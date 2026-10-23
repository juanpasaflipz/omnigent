"""Encryption for integration secrets stored at rest.

Integration credentials (GitHub App tokens, Databricks and MCP grants) are
encrypted before they touch the database, behind the :class:`SecretCipher`
port. Three backends implement it:

- ``local`` (:mod:`.local_cipher`): AES-256-GCM envelope encryption under a
  master key from ``OMNIGENT_CREDENTIAL_ENC_KEY``. The self-hosting default.
- ``kms`` (:class:`KmsSecretCipher`): the same envelope, with the data key
  generated and wrapped by AWS KMS (``OMNIGENT_CREDENTIAL_KMS_KEY_ID``).
- ``vault`` (:mod:`.vault_cipher`): HashiCorp Vault Transit.

Each secret is bound to its row's identity —
``(workspace_id, user_id, provider, account_id)`` — so a ciphertext only
decrypts for the row it was written for. No configured backend ⇒ the store,
and every integration built on it, is disabled; there is never a plaintext
fallback. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from omnigent.stores.credential_store.envelope import (
    KMS_BACKEND,
    CiphertextOrigin,
    ciphertext_backend,
    foreign_backend_error,
    is_envelope,
    open_sealed,
    parse_envelope,
    seal,
)

_logger = logging.getLogger(__name__)

#: A row's identity, passed as the encryption ``context`` so each
#: ``(workspace, user, provider, account)`` is bound to its own ciphertext.
SecretContext = Mapping[str, str]

#: Names the AWS KMS key (id, ARN, or ``alias/…``) used to encrypt integration
#: secrets. Owned by the credential store, not any one provider, so every
#: "Connect …" integration shares it. Unset ⇒ the store is disabled.
CREDENTIAL_KMS_KEY_ENV_VAR = "OMNIGENT_CREDENTIAL_KMS_KEY_ID"

#: Optional explicit backend selector for the credential store's cipher
#: (``local`` | ``kms`` | ``vault``). Set it to pick a backend per server; its key env var is
#: then required. Unset ⇒ the single configured backend is auto-detected, and
#: configuring more than one without this selector is an error.
CREDENTIAL_CIPHER_ENV_VAR = "OMNIGENT_CREDENTIAL_CIPHER"

#: KMS error codes that mean "this ciphertext can't be read under this identity"
#: — a wrong/rotated key, a mismatched encryption context, or a corrupt blob.
#: These degrade to "reconnect" (``decrypt`` → ``None``); anything else
#: (AccessDenied, disabled key, throttling) is operational and propagates.
_SOFT_FAIL_KMS_CODES = frozenset({"InvalidCiphertextException", "IncorrectKeyException"})


#: Operator-facing list of the ways to enable the credential store.
CREDENTIAL_BACKEND_SETUP_HINT = (
    "set OMNIGENT_CREDENTIAL_ENC_KEY to a local key (generate one with "
    "`openssl rand -base64 32`), or configure AWS KMS (OMNIGENT_CREDENTIAL_KMS_KEY_ID) "
    "or Vault Transit (OMNIGENT_CREDENTIAL_VAULT_KEY)"
)


class SecretTooLargeError(ValueError):
    """The serialized secret exceeds the configured encryption backend limit."""


@runtime_checkable
class SecretCipher(Protocol):
    """Port for encrypting integration secrets at rest.

    The credential store depends on this, not on a concrete backend, so the KMS
    implementation can be swapped (e.g. for GCP/Azure KMS or Vault Transit)
    without a schema change. See ``designs/CREDENTIAL_STORE.md``.

    ``context`` is the row's identity (``workspace``/``user``/``provider``/
    ``account``) and is **required** — every secret is bound to a user, there is
    no unbound/global-key path. Decryption must be given the *same* context; a
    mismatch yields ``None`` (⇒ reconnect), never a wrong plaintext.
    """

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        """Return the ciphertext for *plaintext*, bound to *context*."""
        ...

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Return the plaintext, or ``None`` when the ciphertext/context is unusable."""
        ...


@runtime_checkable
class RewrappableSecretCipher(SecretCipher, Protocol):
    """A :class:`SecretCipher` that reports which backend and key it writes with.

    ``omnigent credentials rewrap`` compares this against each stored value's
    origin (:func:`~.envelope.describe_ciphertext`) to find values to re-encrypt.
    """

    @property
    def write_origin(self) -> CiphertextOrigin | None:
        """Backend and key id of new writes; ``None`` for a read-only cipher."""
        ...


class MultiBackendSecretCipher:
    """Writes with one backend and reads values from any configured backend.

    Used while migrating between backends (for example local → KMS with the old
    local key kept in ``OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS``): decrypt routes
    each value by its prefix, so rows not yet rewrapped keep working.

    :param writer: Cipher for every encrypt, and for values in its own format.
    :param writer_backend: The writer's backend name (``local``/``kms``/``vault``).
    :param readers: Other configured backends, by name, used for decrypt only.
    """

    def __init__(
        self,
        writer: SecretCipher,
        writer_backend: str,
        readers: Mapping[str, SecretCipher],
    ) -> None:
        self.writer = writer
        self._backends = {**readers, writer_backend: writer}

    @property
    def write_origin(self) -> CiphertextOrigin | None:
        return getattr(self.writer, "write_origin", None)

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        return self.writer.encrypt(plaintext, context=context)

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        backend = ciphertext_backend(ciphertext)
        cipher = self._backends.get(backend)
        if cipher is None:
            raise foreign_backend_error(backend, ", ".join(sorted(self._backends)))
        return cipher.decrypt(ciphertext, context=context)


def build_kms_secret_cipher() -> KmsSecretCipher | None:
    """Build the AWS KMS cipher from ``OMNIGENT_CREDENTIAL_KMS_KEY_ID``, or ``None``.

    ``None`` when the key is unset — the KMS backend is simply not configured.
    boto3 stays lazy: this only reads env and constructs (no SDK import and no AWS
    call until the first encrypt/decrypt).
    """
    key_id = os.environ.get(CREDENTIAL_KMS_KEY_ENV_VAR, "").strip()
    return KmsSecretCipher(key_id) if key_id else None


def build_secret_cipher() -> SecretCipher | None:
    """Construct the credential store's cipher from deployment config, or ``None``.

    Set ``OMNIGENT_CREDENTIAL_CIPHER`` (``local``, ``kms`` or ``vault``) to choose
    the write backend explicitly; its key env var (``OMNIGENT_CREDENTIAL_ENC_KEY``
    / ``OMNIGENT_CREDENTIAL_KMS_KEY_ID`` / ``OMNIGENT_CREDENTIAL_VAULT_KEY``) is then
    required. When the selector is unset, the single configured backend is
    auto-detected; configuring more than one without a selector is an error, and
    configuring none disables the store (``None``).

    Other backends whose key material is also configured (including local keys
    listed only in ``OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS``) are added as
    decrypt-only readers, so values written before a backend switch stay readable
    until ``omnigent credentials rewrap`` re-encrypts them.

    :raises ValueError: On an unknown selector, a selected backend without its
        key, an ambiguous configuration, or a malformed local key.
    """
    # hvac / boto3 stay optional: each builder only reads env and constructs, so
    # importing and calling them here pulls no SDK and makes no network call.
    from omnigent.stores.credential_store.local_cipher import (
        build_local_secret_cipher,
        build_local_secret_reader,
    )
    from omnigent.stores.credential_store.vault_cipher import build_vault_secret_cipher

    builders: dict[str, Callable[[], SecretCipher | None]] = {
        "kms": build_kms_secret_cipher,
        "local": build_local_secret_cipher,
        "vault": build_vault_secret_cipher,
    }
    reader_builders: dict[str, Callable[[], SecretCipher | None]] = {
        **builders,
        "local": build_local_secret_reader,
    }

    selected = os.environ.get(CREDENTIAL_CIPHER_ENV_VAR, "").strip().lower()
    if selected:
        builder = builders.get(selected)
        if builder is None:
            raise ValueError(
                f"{CREDENTIAL_CIPHER_ENV_VAR}={selected!r} is not a known credential "
                f"backend; expected one of {sorted(builders)}."
            )
        writer = builder()
        if writer is None:
            raise ValueError(
                f"{CREDENTIAL_CIPHER_ENV_VAR}={selected!r} but its key is unset — "
                "set the backend's key env var."
            )
    else:
        configured = {name: c for name, build in builders.items() if (c := build()) is not None}
        if len(configured) > 1:
            raise ValueError(
                f"multiple credential backends are configured ({sorted(configured)}); "
                f"set {CREDENTIAL_CIPHER_ENV_VAR} to choose one."
            )
        if not configured:
            return None
        ((selected, writer),) = configured.items()

    readers = {
        name: reader
        for name, build in reader_builders.items()
        if name != selected and (reader := build()) is not None
    }
    return MultiBackendSecretCipher(writer, selected, readers) if readers else writer


def build_integration_cipher(integration: str) -> SecretCipher | None:
    """:func:`build_secret_cipher` for a configured integration, logging if none.

    :param integration: Name for the log line, e.g. ``"GitHub App"``.
    :returns: The cipher, or ``None`` (after logging how to enable one).
    """
    cipher = build_secret_cipher()
    if cipher is None:
        _logger.error(
            "%s is configured but disabled: no credential store backend; %s.",
            integration,
            CREDENTIAL_BACKEND_SETUP_HINT,
        )
    return cipher


def _kms_context(context: SecretContext) -> dict[str, str]:
    """Encryption context for a KMS call.

    KMS rejects empty-string encryption-context values, so drop them — done
    consistently on encrypt and decrypt, the binding is unchanged, and the
    remaining fields (workspace/user/provider, plus ``account_id`` only when a
    named account is used) still uniquely identify the row.
    """
    return {k: v for k, v in context.items() if v != ""}


class KmsSecretCipher:
    """:class:`SecretCipher` backed by AWS KMS envelope encryption.

    Each write calls ``kms:GenerateDataKey`` under ``key_id`` with the row
    identity as the encryption context, encrypts the secret locally with the
    returned AES-256 data key (AES-GCM, identity bound as associated data), and
    stores only the KMS-wrapped data key beside it in the ``env1:kms:`` envelope
    (see :mod:`.envelope`). Decrypt sends just the wrapped data key to
    ``kms:Decrypt``. Values written by the earlier direct ``kms:Encrypt`` code
    (plain base64 blobs) still decrypt. The KMS client is created lazily so
    importing this module never requires boto3 or AWS credentials.

    :param key_id: KMS key id, ARN, or ``alias/…``.
    :param client: Optional pre-built boto3 KMS client (tests inject a fake).
    """

    backend = KMS_BACKEND

    def __init__(self, key_id: str, *, client: Any | None = None) -> None:
        self._key_id = key_id
        self._client = client

    @property
    def _kms(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    "The AWS KMS credential backend needs boto3. Install it with "
                    "`pip install 'omnigent[kms]'`."
                ) from exc

            self._client = boto3.client("kms")
        return self._client

    @property
    def write_origin(self) -> CiphertextOrigin:
        """Backend and key id new writes use."""
        return CiphertextOrigin(KMS_BACKEND, self._key_id)

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        response = self._kms.generate_data_key(
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=_kms_context(context),
        )
        return seal(
            plaintext,
            backend=KMS_BACKEND,
            key_id=self._key_id,
            wrapped_key=response["CiphertextBlob"],
            data_key=response["Plaintext"],
            context=context,
        )

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Return the plaintext for *ciphertext*, or ``None`` if unreadable.

        Returns ``None`` (rather than raising) when the ciphertext was written
        under a different key/context or is corrupt, so a rotated key degrades
        to "reconnect" instead of a 500. Operational failures (access denied,
        disabled key, throttling) propagate — telling the user to reconnect
        wouldn't help and would hide a misconfiguration. A value from another
        backend raises :class:`EnvelopeKeyError`.
        """
        backend = ciphertext_backend(ciphertext)
        if backend != KMS_BACKEND:
            raise foreign_backend_error(backend, KMS_BACKEND)
        if not is_envelope(ciphertext):
            return self._decrypt_legacy(ciphertext, context)
        envelope = parse_envelope(ciphertext)
        if envelope is None:
            _logger.warning(
                "kms credential cipher: stored value is not a valid envelope (corrupt) — "
                "the affected integration will read as disconnected until reconnected"
            )
            return None
        data_key = self._kms_decrypt(envelope.wrapped_key, context, key_id=envelope.key_id)
        if data_key is None:
            return None
        plaintext = open_sealed(envelope, data_key, context)
        if plaintext is None:
            _logger.warning(
                "kms credential cipher: payload failed authentication (tampered or wrong "
                "row) — the affected integration will read as disconnected until reconnected"
            )
        return plaintext

    def _decrypt_legacy(self, ciphertext: str, context: SecretContext) -> str | None:
        """Read a value written by direct ``kms:Encrypt`` (plain base64 blob)."""
        try:
            blob = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError):
            _logger.warning(
                "secretbox: stored ciphertext is not valid base64 (corrupt) — "
                "the affected integration will read as disconnected until reconnected"
            )
            return None
        plaintext = self._kms_decrypt(blob, context, key_id=self._key_id)
        return plaintext.decode("utf-8") if plaintext is not None else None

    def _kms_decrypt(self, blob: bytes, context: SecretContext, *, key_id: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            response = self._kms.decrypt(
                CiphertextBlob=blob,
                EncryptionContext=_kms_context(context),
                KeyId=key_id,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _SOFT_FAIL_KMS_CODES:
                _logger.warning(
                    "secretbox: KMS could not decrypt (code=%s: wrong key/context or "
                    "corrupt) — the affected integration will read as disconnected "
                    "until reconnected",
                    code,
                )
                return None
            raise
        return bytes(response["Plaintext"])
