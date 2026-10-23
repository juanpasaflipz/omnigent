"""Re-encrypt stored credentials under the current backend and key.

``connections.secret_enc`` is the only column holding cipher output; every
integration (GitHub, Databricks, MCP grants and pending OAuth grants) stores its
secret there through :class:`~.sqlalchemy_store.CredentialStore`. This walks all
rows across workspaces and rewrites any value whose envelope is not on the
cipher's current backend and key. Each row is its own transaction guarded by a
compare-and-swap on the old value, so a crash leaves a consistent table, a rerun
only touches what is left, and a concurrent token refresh is never overwritten.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConnection
from omnigent.db.utils import (
    NamedManagedSessionMaker,
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.stores.credential_store.envelope import describe_ciphertext
from omnigent.stores.credential_store.secret_cipher import RewrappableSecretCipher, SecretCipher
from omnigent.stores.credential_store.sqlalchemy_store import _check_encrypted_size, _enc_context

_logger = logging.getLogger(__name__)

_RowKey = tuple[int, str, str, str]


@dataclass
class RewrapFailure:
    """A row that could not be rewrapped (left untouched)."""

    workspace_id: int
    user_id: str
    provider: str
    account_id: str
    reason: str


@dataclass
class RewrapReport:
    """Outcome counts of one rewrap pass.

    ``by_provider`` and ``by_source_key`` count rows that were (or, in a dry
    run, would be) rewrapped; ``source_key`` is ``backend:key_id``.
    """

    target: str
    dry_run: bool
    scanned: int = 0
    current: int = 0
    rewrapped: int = 0
    skipped_concurrent: int = 0
    by_provider: Counter[str] = field(default_factory=Counter)
    by_source_key: Counter[str] = field(default_factory=Counter)
    failures: list[RewrapFailure] = field(default_factory=list)


def rewrap_credentials(
    storage_location: str, cipher: SecretCipher, *, dry_run: bool = False
) -> RewrapReport:
    """Re-encrypt every stored credential not already on *cipher*'s write key.

    :param storage_location: SQLAlchemy database URI.
    :param cipher: The configured cipher; it must be able to read every source
        backend/key (e.g. via ``OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS``).
    :param dry_run: Count what would change without writing.
    :raises ValueError: When *cipher* is read-only or cannot report its key.
    """
    target = cipher.write_origin if isinstance(cipher, RewrappableSecretCipher) else None
    if target is None:
        raise ValueError("the configured credential cipher is read-only; set a current key")
    engine = get_or_create_engine(storage_location)
    session = make_named_managed_session_maker(
        engine, query_name_prefix="omnigent.credential_store"
    )
    session_immediate = make_named_managed_session_maker(
        engine, query_name_prefix="omnigent.credential_store", immediate=True
    )
    report = RewrapReport(target=target.label(), dry_run=dry_run)

    with session("rewrap_list_secrets") as scan:
        rows = scan.execute(
            select(
                SqlConnection.workspace_id,
                SqlConnection.user_id,
                SqlConnection.provider,
                SqlConnection.account_id,
                SqlConnection.secret_enc,
            ).order_by(
                SqlConnection.workspace_id,
                SqlConnection.user_id,
                SqlConnection.provider,
                SqlConnection.account_id,
            )
        ).all()

    for workspace_id, user_id, provider, account_id, old_value in rows:
        report.scanned += 1
        row_key: _RowKey = (workspace_id, user_id, provider, account_id)

        def fail(reason: str, row_key: _RowKey = row_key) -> None:
            report.failures.append(RewrapFailure(*row_key, reason=reason))

        origin = describe_ciphertext(old_value)
        if origin == target:
            report.current += 1
            continue
        source = origin.label() if origin is not None else "unrecognized"
        context = _enc_context(workspace_id, user_id, provider, account_id)
        try:
            plaintext = cipher.decrypt(old_value, context=context)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pass
            fail(f"{source}: {type(exc).__name__}: {exc}")
            continue
        if plaintext is None:
            fail(f"{source}: could not be decrypted (corrupt or wrong row)")
            continue
        new_value = cipher.encrypt(plaintext, context=context)
        try:
            _check_encrypted_size(new_value, engine.dialect.name)
        except ValueError as exc:
            fail(f"{source}: {exc}")
            continue
        if not dry_run and not _swap(session_immediate, row_key, old_value, new_value):
            # Refreshed or removed since the scan; its new value is already current.
            report.skipped_concurrent += 1
            continue
        report.rewrapped += 1
        report.by_provider[provider] += 1
        report.by_source_key[source] += 1

    for failure in report.failures:
        _logger.warning(
            "credentials rewrap: skipped %s connection (workspace %s): %s",
            failure.provider,
            failure.workspace_id,
            failure.reason,
        )
    return report


def _swap(
    session_maker: NamedManagedSessionMaker,
    row_key: _RowKey,
    old_value: str,
    new_value: str,
) -> bool:
    """Replace one row's value only if it still holds *old_value*."""
    workspace_id, user_id, provider, account_id = row_key

    def write(session: Session) -> bool:
        result = cast(
            CursorResult[tuple[object]],
            session.execute(
                update(SqlConnection)
                .where(
                    SqlConnection.workspace_id == workspace_id,
                    SqlConnection.user_id == user_id,
                    SqlConnection.provider == provider,
                    SqlConnection.account_id == account_id,
                    SqlConnection.secret_enc == old_value,
                )
                .values(secret_enc=new_value)
            ),
        )
        return result.rowcount > 0

    return run_write_transaction(session_maker, "rewrap_secret", write)
