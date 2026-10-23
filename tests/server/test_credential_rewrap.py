"""Tests for re-encrypting stored credentials after a key rotation."""

from __future__ import annotations

import base64
import os

import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.credential_store.envelope import describe_ciphertext
from omnigent.stores.credential_store.local_cipher import (
    CREDENTIAL_ENC_KEY_ENV_VAR,
    CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR,
    LocalSecretCipher,
    key_fingerprint,
)
from omnigent.stores.credential_store.rewrap import rewrap_credentials
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore


def _stored(db_uri: str) -> dict[tuple[int, str, str], str]:
    with get_or_create_engine(db_uri).connect() as connection:
        rows = connection.execute(
            sa.text("SELECT workspace_id, user_id, provider, secret_enc FROM connections")
        ).all()
    return {(row[0], row[1], row[2]): row[3] for row in rows}


def _corrupt(db_uri: str, user_id: str, provider: str) -> None:
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE connections SET secret_enc = 'env1:local:bogus' "
                "WHERE user_id = :user AND provider = :provider"
            ),
            {"user": user_id, "provider": provider},
        )


@pytest.fixture
def rotated(db_uri: str) -> tuple[bytes, bytes]:
    """Three rows across stores and workspaces under an old key, one already current."""
    old_key, new_key = os.urandom(32), os.urandom(32)
    old_store = CredentialStore(db_uri, LocalSecretCipher(old_key))
    old_store.upsert("alice", "github", secret={"access_token": "gh-a"}, metadata={})
    old_store.upsert("alice", "mcp:tracker", secret={"access_token": "mcp-a"}, metadata={})
    with workspace_scope(7):
        old_store.upsert("bob", "databricks", secret={"refresh_token": "dbx-b"}, metadata={})
    new_store = CredentialStore(db_uri, LocalSecretCipher(new_key))
    new_store.upsert("carol", "github", secret={"access_token": "gh-c"}, metadata={})
    return old_key, new_key


def test_rewrap_moves_previous_key_values_to_the_current_key(
    db_uri: str, rotated: tuple[bytes, bytes]
) -> None:
    old_key, new_key = rotated
    cipher = LocalSecretCipher(new_key, [old_key])
    report = rewrap_credentials(db_uri, cipher)

    assert (report.scanned, report.current, report.rewrapped) == (4, 1, 3)
    assert report.failures == []
    assert report.by_provider == {"github": 1, "mcp:tracker": 1, "databricks": 1}
    assert report.by_source_key == {f"local:{key_fingerprint(old_key)}": 3}
    for value in _stored(db_uri).values():
        assert describe_ciphertext(value) == cipher.write_origin

    # The old key is no longer needed to read anything.
    current_only = CredentialStore(db_uri, LocalSecretCipher(new_key))
    alice = current_only.get("alice", "github", with_secret=True)
    assert alice is not None and alice.secret == {"access_token": "gh-a"}
    with workspace_scope(7):
        bob = current_only.get("bob", "databricks", with_secret=True)
    assert bob is not None and bob.secret == {"refresh_token": "dbx-b"}

    # Rerun is a no-op.
    again = rewrap_credentials(db_uri, cipher)
    assert (again.current, again.rewrapped, again.failures) == (4, 0, [])


def test_dry_run_changes_nothing(db_uri: str, rotated: tuple[bytes, bytes]) -> None:
    old_key, new_key = rotated
    before = _stored(db_uri)
    report = rewrap_credentials(db_uri, LocalSecretCipher(new_key, [old_key]), dry_run=True)
    assert report.dry_run and report.rewrapped == 3
    assert _stored(db_uri) == before


def test_corrupted_row_is_reported_and_skipped(db_uri: str, rotated: tuple[bytes, bytes]) -> None:
    old_key, new_key = rotated
    _corrupt(db_uri, "alice", "mcp:tracker")
    report = rewrap_credentials(db_uri, LocalSecretCipher(new_key, [old_key]))
    assert report.rewrapped == 2
    [failure] = report.failures
    assert (failure.user_id, failure.provider) == ("alice", "mcp:tracker")
    assert "ghu" not in failure.reason and "gh-a" not in failure.reason
    assert _stored(db_uri)[(0, "alice", "mcp:tracker")] == "env1:local:bogus"


def test_missing_previous_key_is_reported_per_row(
    db_uri: str, rotated: tuple[bytes, bytes]
) -> None:
    old_key, new_key = rotated
    report = rewrap_credentials(db_uri, LocalSecretCipher(new_key))
    assert report.rewrapped == 0
    assert len(report.failures) == 3
    assert all(key_fingerprint(old_key) in failure.reason for failure in report.failures)


def test_cli_rewrap_reports_counts(
    db_uri: str, rotated: tuple[bytes, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.cli import cli

    old_key, new_key = rotated
    for name in ("OMNIGENT_CREDENTIAL_CIPHER", "OMNIGENT_CREDENTIAL_KMS_KEY_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OMNIGENT_CREDENTIAL_VAULT_KEY", raising=False)
    monkeypatch.setenv(CREDENTIAL_ENC_KEY_ENV_VAR, base64.b64encode(new_key).decode())
    monkeypatch.setenv(CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR, base64.b64encode(old_key).decode())

    dry = CliRunner().invoke(cli, ["credentials", "rewrap", "--database-uri", db_uri, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would rewrap 3" in dry.output

    result = CliRunner().invoke(cli, ["credentials", "rewrap", "--database-uri", db_uri])
    assert result.exit_code == 0, result.output
    assert f"Target key: local:{key_fingerprint(new_key)}" in result.output
    assert "rewrapped 3" in result.output
    assert "store github: 1" in result.output
    assert f"from local:{key_fingerprint(old_key)}: 3" in result.output
    assert base64.b64encode(new_key).decode() not in result.output


def test_cli_rewrap_without_a_backend_names_the_options(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.cli import cli

    for name in (
        "OMNIGENT_CREDENTIAL_CIPHER",
        "OMNIGENT_CREDENTIAL_KMS_KEY_ID",
        "OMNIGENT_CREDENTIAL_VAULT_KEY",
        CREDENTIAL_ENC_KEY_ENV_VAR,
        CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(cli, ["credentials", "rewrap", "--database-uri", db_uri])
    assert result.exit_code != 0
    assert CREDENTIAL_ENC_KEY_ENV_VAR in result.output
