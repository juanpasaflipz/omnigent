"""Tests for the AWS KMS-backed at-rest secret cipher.

These use an in-memory fake KMS client (no AWS, no moto) to exercise
:class:`KmsSecretCipher`'s envelope encryption, legacy direct-encrypt reads,
encryption-context binding, and soft-fail behaviour. Real KMS semantics are
covered by the demo end-to-end verification.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest
from botocore.exceptions import ClientError

from omnigent.stores.credential_store.secret_cipher import (
    CREDENTIAL_KMS_KEY_ENV_VAR,
    KmsSecretCipher,
    build_secret_cipher,
)

CTX_ALICE = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
CTX_BOB = {"workspace_id": "0", "user_id": "bob", "provider": "github", "account_id": ""}


class _FakeKmsClient:
    """In-memory stand-in for boto3's KMS client.

    ``encrypt`` and ``generate_data_key`` record ``(key_id, encryption_context,
    plaintext)`` under an opaque blob; ``decrypt`` returns the plaintext only when
    the *same* encryption context is presented, otherwise raises the
    ``ClientError`` KMS raises for a context/key mismatch.
    """

    def __init__(self) -> None:
        self.store: dict[bytes, tuple[str, dict[str, str], bytes]] = {}
        self._n = 0
        self.decrypted_blobs: list[bytes] = []

    def generate_data_key(
        self, *, KeyId: str, KeySpec: str, EncryptionContext: dict[str, str]
    ) -> Any:
        assert KeySpec == "AES_256"
        self._n += 1
        blob = f"dk-{self._n}".encode()
        data_key = os.urandom(32)
        self.store[blob] = (KeyId, dict(EncryptionContext), data_key)
        return {"CiphertextBlob": blob, "Plaintext": data_key, "KeyId": KeyId}

    def encrypt(self, *, KeyId: str, Plaintext: bytes, EncryptionContext: dict[str, str]) -> Any:
        self._n += 1
        blob = f"ct-{self._n}".encode()
        self.store[blob] = (KeyId, dict(EncryptionContext), Plaintext)
        return {"CiphertextBlob": blob, "KeyId": KeyId}

    def decrypt(
        self, *, CiphertextBlob: bytes, EncryptionContext: dict[str, str], KeyId: str | None = None
    ) -> Any:
        self.decrypted_blobs.append(CiphertextBlob)
        entry = self.store.get(CiphertextBlob)
        if entry is None or entry[1] != dict(EncryptionContext):
            raise ClientError(
                {"Error": {"Code": "InvalidCiphertextException", "Message": "mismatch"}},
                "Decrypt",
            )
        return {"Plaintext": entry[2], "KeyId": entry[0]}


def _cipher() -> KmsSecretCipher:
    return KmsSecretCipher("alias/omnigent-credentials", client=_FakeKmsClient())


def test_encrypt_decrypt_roundtrip() -> None:
    box = _cipher()
    ct = box.encrypt("ghu_secret_token", context=CTX_ALICE)
    assert ct != "ghu_secret_token"
    assert box.decrypt(ct, context=CTX_ALICE) == "ghu_secret_token"


def test_wrong_context_returns_none() -> None:
    """A secret encrypted for one identity can't be read under another's."""
    box = _cipher()
    ct = box.encrypt("ghu_alice", context=CTX_ALICE)
    assert box.decrypt(ct, context=CTX_BOB) is None


def test_context_is_required() -> None:
    # There is no unbound/global-key path: the identity is mandatory.
    box = _cipher()
    with pytest.raises(TypeError):
        box.encrypt("x")  # type: ignore[call-arg]


def test_envelope_sends_only_the_wrapped_data_key_to_kms() -> None:
    client = _FakeKmsClient()
    box = KmsSecretCipher("alias/omnigent-credentials", client=client)
    secret = "ghu_" + "x" * 10_000  # beyond the 4096-byte direct-encrypt limit
    ct = box.encrypt(secret, context=CTX_ALICE)
    assert ct.startswith("env1:kms:")
    assert secret not in ct
    assert box.encrypt(secret, context=CTX_ALICE) != ct  # fresh data key and nonce
    assert box.decrypt(ct, context=CTX_ALICE) == secret
    assert client.decrypted_blobs == [b"dk-1"]  # the wrapped data key, never the secret


def test_envelope_tampered_payload_returns_none() -> None:
    box = _cipher()
    ct = box.encrypt("ghu_secret", context=CTX_ALICE)
    head, payload = ct.rsplit(":", 1)
    flipped = "A" if payload[0] != "A" else "B"
    assert box.decrypt(f"{head}:{flipped}{payload[1:]}", context=CTX_ALICE) is None


def test_legacy_direct_encrypt_ciphertext_still_decrypts() -> None:
    # Values written before envelopes: a plain base64 kms:Encrypt blob.
    client = _FakeKmsClient()
    legacy_blob = client.encrypt(
        KeyId="alias/omnigent-credentials",
        Plaintext=b'{"access_token": "ghu_old"}',
        EncryptionContext={"workspace_id": "0", "user_id": "alice", "provider": "github"},
    )["CiphertextBlob"]
    legacy = base64.b64encode(legacy_blob).decode("ascii")
    box = KmsSecretCipher("alias/omnigent-credentials", client=client)
    assert box.decrypt(legacy, context=CTX_ALICE) == '{"access_token": "ghu_old"}'
    assert box.decrypt(legacy, context=CTX_BOB) is None


def test_local_envelope_is_rejected_as_a_foreign_backend() -> None:
    from omnigent.stores.credential_store.envelope import EnvelopeKeyError
    from omnigent.stores.credential_store.local_cipher import LocalSecretCipher

    local_value = LocalSecretCipher(os.urandom(32)).encrypt("x", context=CTX_ALICE)
    with pytest.raises(EnvelopeKeyError, match="'local'"):
        _cipher().decrypt(local_value, context=CTX_ALICE)


def test_empty_context_values_dropped() -> None:
    # KMS rejects empty encryption-context values; account_id="" is dropped
    # consistently, and a named account still produces a distinct context.
    client = _FakeKmsClient()
    box = KmsSecretCipher("k", client=client)
    ct = box.encrypt("x", context=CTX_ALICE)
    (_key, stored_ctx, _pt) = client.store[next(iter(client.store))]
    assert "account_id" not in stored_ctx
    assert stored_ctx == {"workspace_id": "0", "user_id": "alice", "provider": "github"}
    # A named account yields a different, still-decryptable context.
    named = {**CTX_ALICE, "account_id": "org1"}
    assert box.decrypt(box.encrypt("y", context=named), context=named) == "y"
    assert box.decrypt(ct, context=named) is None


def test_corrupt_ciphertext_returns_none() -> None:
    box = _cipher()
    assert box.decrypt("not-valid-base64!!", context=CTX_ALICE) is None


def test_unknown_blob_returns_none() -> None:
    # Valid base64 but not a real KMS blob (InvalidCiphertextException) → reconnect.
    box = _cipher()
    assert box.decrypt("aGVsbG8=", context=CTX_ALICE) is None


def test_operational_error_propagates() -> None:
    # AccessDenied / disabled key are operational, not per-row: they must NOT be
    # masked as "reconnect" (which wouldn't help); they propagate.
    class _DenyingClient(_FakeKmsClient):
        def decrypt(self, **kwargs: Any) -> Any:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Decrypt"
            )

    box = KmsSecretCipher("k", client=_DenyingClient())
    ct = box.encrypt("x", context=CTX_ALICE)
    with pytest.raises(ClientError):
        box.decrypt(ct, context=CTX_ALICE)


def test_build_secret_cipher_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CREDENTIAL_KMS_KEY_ENV_VAR, raising=False)
    assert build_secret_cipher() is None


def test_build_secret_cipher_disabled_with_whitespace_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_KMS_KEY_ENV_VAR, "   \t\n")
    assert build_secret_cipher() is None


def test_build_secret_cipher_from_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_KMS_KEY_ENV_VAR, "arn:aws:kms:us-east-1:1:key/abc")
    cipher = build_secret_cipher()
    assert isinstance(cipher, KmsSecretCipher)
