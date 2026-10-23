"""Versioned envelope format shared by the local-key and KMS credential ciphers.

Every value is ``env1:<backend>:<key_id>:<wrapped_key>:<nonce>:<ciphertext>``:

- ``backend`` is ``local`` or ``kms``; ``key_id`` is base64url of the wrapping
  key's id (a SHA-256 fingerprint for ``local``, the configured KMS key for
  ``kms``) so the reader picks the right key without trying them all.
- ``wrapped_key`` is the per-value 256-bit data key, wrapped by the backend.
- ``nonce`` / ``ciphertext`` are the AES-256-GCM nonce and output (tag appended)
  of the secret under that data key.

All binary fields are unpadded base64url, so ``:`` never appears inside one.
The header and the row's identity are bound as AES-GCM associated data, so a
value moved to another row, backend, or key id fails authentication. The
``env1:`` prefix cannot be confused with Vault's ``osvault:`` envelope or a
legacy KMS value (plain standard base64, whose alphabet has no ``:``).
See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_VERSION = "env1"
LOCAL_BACKEND = "local"
KMS_BACKEND = "kms"
VAULT_BACKEND = "vault"

#: Prefix of Vault Transit values written by ``VaultSecretCipher``.
VAULT_PREFIX = "osvault:"

#: ``key_id`` reported for KMS values written before the envelope format.
LEGACY_KMS_KEY_ID = "legacy"

DATA_KEY_BYTES = 32
NONCE_BYTES = 12

_ENVELOPE_BACKENDS = frozenset({LOCAL_BACKEND, KMS_BACKEND})
_B64URL_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class EnvelopeKeyError(ValueError):
    """The value names a backend or key id this server has no key for.

    Store-wide misconfiguration (a key removed too early, a backend switched
    without keeping the old key readable), so it propagates instead of reading
    as a per-row "reconnect".
    """


@dataclass(frozen=True)
class CiphertextOrigin:
    """Which backend and key produced a stored value."""

    backend: str
    key_id: str

    def label(self) -> str:
        """Human-readable ``backend:key_id`` used in reports and errors."""
        return f"{self.backend}:{self.key_id}"


@dataclass(frozen=True)
class Envelope:
    """A parsed ``env1`` value."""

    backend: str
    key_id: str
    wrapped_key: bytes
    nonce: bytes
    ciphertext: bytes

    @property
    def header(self) -> str:
        """The authenticated header, ``env1:<backend>:<b64url(key_id)>``."""
        return envelope_header(self.backend, self.key_id)


def b64url_encode(raw: bytes) -> str:
    """Unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Decode unpadded base64url strictly; raise ``ValueError`` otherwise."""
    if not text or any(ch not in _B64URL_ALPHABET for ch in text):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error as exc:
        raise ValueError("not base64url") from exc


def envelope_header(backend: str, key_id: str) -> str:
    """The authenticated header ``env1:<backend>:<b64url(key_id)>``."""
    return f"{ENVELOPE_VERSION}:{backend}:{b64url_encode(key_id.encode('utf-8'))}"


def context_aad(header: str, context: Mapping[str, str]) -> bytes:
    """Associated data binding a value to its header and its row identity.

    Empty context values are dropped (as the KMS and Vault ciphers do) and the
    rest is serialized as sorted-key JSON, so the encoding is order-independent
    and a value containing a delimiter cannot collide two identities.
    """
    items = {key: value for key, value in context.items() if value != ""}
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return f"{header}\n{canonical}".encode()


def seal(
    plaintext: str,
    *,
    backend: str,
    key_id: str,
    wrapped_key: bytes,
    data_key: bytes,
    context: Mapping[str, str],
) -> str:
    """Encrypt *plaintext* under *data_key* and serialize the envelope."""
    header = envelope_header(backend, key_id)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(data_key).encrypt(
        nonce, plaintext.encode("utf-8"), context_aad(header, context)
    )
    return ":".join(
        (header, b64url_encode(wrapped_key), b64url_encode(nonce), b64url_encode(ciphertext))
    )


def open_sealed(envelope: Envelope, data_key: bytes, context: Mapping[str, str]) -> str | None:
    """Decrypt an envelope's payload with its unwrapped *data_key*, or ``None``."""
    try:
        plaintext = AESGCM(data_key).decrypt(
            envelope.nonce, envelope.ciphertext, context_aad(envelope.header, context)
        )
    except (InvalidTag, ValueError):
        return None
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        return None


def is_envelope(value: str) -> bool:
    """Whether *value* claims the ``env1`` format (it may still be malformed)."""
    return value.startswith(f"{ENVELOPE_VERSION}:")


def parse_envelope(value: str) -> Envelope | None:
    """Parse an ``env1`` value, or ``None`` when it is not well-formed."""
    parts = value.split(":")
    if len(parts) != 6 or parts[0] != ENVELOPE_VERSION or parts[1] not in _ENVELOPE_BACKENDS:
        return None
    try:
        key_id = b64url_decode(parts[2]).decode("utf-8")
        wrapped_key = b64url_decode(parts[3])
        nonce = b64url_decode(parts[4])
        ciphertext = b64url_decode(parts[5])
    except (ValueError, UnicodeDecodeError):
        return None
    if len(nonce) != NONCE_BYTES:
        return None
    return Envelope(parts[1], key_id, wrapped_key, nonce, ciphertext)


def describe_ciphertext(value: str) -> CiphertextOrigin | None:
    """Return which backend/key wrote *value*, or ``None`` when unrecognizable.

    Values without a recognized prefix are reported as legacy direct-encrypt
    KMS values, the only format the server wrote before envelopes.
    """
    if is_envelope(value):
        envelope = parse_envelope(value)
        return CiphertextOrigin(envelope.backend, envelope.key_id) if envelope else None
    if value.startswith(VAULT_PREFIX):
        parts = value.split(":", 2)
        if len(parts) != 3:
            return None
        try:
            key_name = base64.urlsafe_b64decode(parts[1].encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        return CiphertextOrigin(VAULT_BACKEND, key_name)
    return CiphertextOrigin(KMS_BACKEND, LEGACY_KMS_KEY_ID)


def ciphertext_backend(value: str) -> str:
    """The backend that must decrypt *value* (by prefix; legacy values are KMS)."""
    if is_envelope(value):
        return value.split(":", 2)[1]
    if value.startswith(VAULT_PREFIX):
        return VAULT_BACKEND
    return KMS_BACKEND


def foreign_backend_error(value_backend: str, reader_backend: str) -> EnvelopeKeyError:
    """Error for a value written by a backend this cipher cannot read."""
    return EnvelopeKeyError(
        f"credential was encrypted by the {value_backend!r} backend but this server only "
        f"reads {reader_backend!r}; keep the old backend's key configured (for a local key, "
        "OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS) until `omnigent credentials rewrap` has run."
    )
