"""Local-key :class:`SecretCipher`: AES-256-GCM envelope encryption under a
master key from the environment, for self-hosted servers without a cloud KMS.

Each value gets a fresh random data key that encrypts the secret; the master
key only wraps that data key. Both layers bind the envelope header and the
row identity as associated data (see :mod:`.envelope`).

``OMNIGENT_CREDENTIAL_ENC_KEY`` is the current master key (32 random bytes,
base64). ``OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS`` is an optional
comma-separated list of retired keys accepted for decrypt only, so a key can be
rotated and old values rewrapped with ``omnigent credentials rewrap``.

A local key protects stored secrets against a leaked database or backup, not
against someone who can read the server's environment.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
from collections.abc import Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from omnigent.stores.credential_store.envelope import (
    DATA_KEY_BYTES,
    LOCAL_BACKEND,
    NONCE_BYTES,
    CiphertextOrigin,
    EnvelopeKeyError,
    ciphertext_backend,
    context_aad,
    envelope_header,
    foreign_backend_error,
    open_sealed,
    parse_envelope,
    seal,
)
from omnigent.stores.credential_store.secret_cipher import SecretContext

_logger = logging.getLogger(__name__)

#: Current local master key (base64 of 32 random bytes).
CREDENTIAL_ENC_KEY_ENV_VAR = "OMNIGENT_CREDENTIAL_ENC_KEY"
#: Retired local master keys, comma-separated, accepted for decrypt only.
CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR = "OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS"

_MASTER_KEY_BYTES = 32
_KEY_GENERATION_HINT = "Generate one with: openssl rand -base64 32"
# AES-GCM appends a 16-byte tag; a wrapped data key is nonce + key + tag.
_WRAPPED_KEY_BYTES = NONCE_BYTES + DATA_KEY_BYTES + 16


def decode_master_key(value: str, *, source: str) -> bytes:
    """Decode a base64 master key (standard or urlsafe, padding optional).

    :param value: The encoded key.
    :param source: Env var name for the error message (the key never is).
    :raises ValueError: Unless it decodes to exactly 32 bytes.
    """
    text = value.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raw = b""
    if len(raw) != _MASTER_KEY_BYTES:
        raise ValueError(
            f"{source} must be {_MASTER_KEY_BYTES} random bytes encoded as base64. "
            f"{_KEY_GENERATION_HINT}"
        )
    return raw


def key_fingerprint(key: bytes) -> str:
    """Short public id of a master key: first 8 hex chars of its SHA-256."""
    return hashlib.sha256(key).hexdigest()[:8]


class LocalSecretCipher:
    """:class:`SecretCipher` using a local AES-256-GCM master key.

    :param current_key: Master key for new writes; ``None`` makes the cipher
        read-only (used to read old values during a move to another backend).
    :param previous_keys: Retired master keys accepted for decrypt only.
    """

    backend = LOCAL_BACKEND

    def __init__(self, current_key: bytes | None, previous_keys: Sequence[bytes] = ()) -> None:
        keys: dict[str, bytes] = {}
        for key in [*([current_key] if current_key is not None else []), *previous_keys]:
            if len(key) != _MASTER_KEY_BYTES:
                raise ValueError(f"local credential keys must be {_MASTER_KEY_BYTES} bytes")
            key_id = key_fingerprint(key)
            if keys.get(key_id, key) != key:
                raise ValueError(
                    f"two local credential keys share the id {key_id}; generate a new key"
                )
            keys[key_id] = key
        if not keys:
            raise ValueError("LocalSecretCipher needs at least one key")
        self._keys = keys
        self._current_id = key_fingerprint(current_key) if current_key is not None else None

    @property
    def write_origin(self) -> CiphertextOrigin | None:
        """Backend and key id new writes use, or ``None`` when read-only."""
        if self._current_id is None:
            return None
        return CiphertextOrigin(LOCAL_BACKEND, self._current_id)

    @property
    def key_ids(self) -> frozenset[str]:
        """Ids of every key this cipher can decrypt with."""
        return frozenset(self._keys)

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        if self._current_id is None:
            raise EnvelopeKeyError(
                f"{CREDENTIAL_ENC_KEY_ENV_VAR} is unset; the local key is configured for "
                "decrypt only"
            )
        master = AESGCM(self._keys[self._current_id])
        data_key = AESGCM.generate_key(bit_length=256)
        wrap_nonce = os.urandom(NONCE_BYTES)
        header_aad = context_aad(envelope_header(LOCAL_BACKEND, self._current_id), context)
        wrapped_key = wrap_nonce + master.encrypt(wrap_nonce, data_key, header_aad)
        return seal(
            plaintext,
            backend=LOCAL_BACKEND,
            key_id=self._current_id,
            wrapped_key=wrapped_key,
            data_key=data_key,
            context=context,
        )

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Return the plaintext, ``None`` for a corrupt/mismatched value.

        :raises EnvelopeKeyError: When the value comes from another backend or
            names a key id that is not configured (store-wide misconfiguration).
        """
        backend = ciphertext_backend(ciphertext)
        if backend != LOCAL_BACKEND:
            raise foreign_backend_error(backend, LOCAL_BACKEND)
        envelope = parse_envelope(ciphertext)
        if envelope is None:
            _logger.warning(
                "local credential cipher: stored value is not a valid envelope (corrupt); "
                "the affected integration will read as disconnected until reconnected"
            )
            return None
        key = self._keys.get(envelope.key_id)
        if key is None:
            raise EnvelopeKeyError(
                f"credential was encrypted with local key id {envelope.key_id!r}, which is "
                f"not configured; add that key to {CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR}"
            )
        wrapped = envelope.wrapped_key
        if len(wrapped) != _WRAPPED_KEY_BYTES:
            return _per_row_failure()
        try:
            data_key = AESGCM(key).decrypt(
                wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], context_aad(envelope.header, context)
            )
        except InvalidTag:
            return _per_row_failure()
        plaintext = open_sealed(envelope, data_key, context)
        return plaintext if plaintext is not None else _per_row_failure()


def _per_row_failure() -> None:
    _logger.warning(
        "local credential cipher: could not decrypt (tampered value or wrong row); "
        "the affected integration will read as disconnected until reconnected"
    )


def _read_keys() -> tuple[bytes | None, list[bytes]]:
    current_text = os.environ.get(CREDENTIAL_ENC_KEY_ENV_VAR, "").strip()
    current = (
        decode_master_key(current_text, source=CREDENTIAL_ENC_KEY_ENV_VAR)
        if current_text
        else None
    )
    previous = [
        decode_master_key(item, source=CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR)
        for item in os.environ.get(CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR, "").split(",")
        if item.strip()
    ]
    return current, previous


def build_local_secret_cipher() -> LocalSecretCipher | None:
    """The local cipher when ``OMNIGENT_CREDENTIAL_ENC_KEY`` is set, else ``None``.

    :raises ValueError: When a configured key is malformed.
    """
    current, previous = _read_keys()
    return LocalSecretCipher(current, previous) if current is not None else None


def build_local_secret_reader() -> LocalSecretCipher | None:
    """A cipher that can read local values when any local key is configured.

    Unlike :func:`build_local_secret_cipher`, previous keys alone are enough;
    the result is then read-only. Used while migrating to another backend.
    """
    current, previous = _read_keys()
    if current is None and not previous:
        return None
    return LocalSecretCipher(current, previous)
