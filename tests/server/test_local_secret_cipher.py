"""Tests for the local-key AES-256-GCM envelope cipher and its key configuration."""

from __future__ import annotations

import base64
import os

import pytest

from omnigent.stores.credential_store.envelope import (
    EnvelopeKeyError,
    b64url_decode,
    b64url_encode,
    describe_ciphertext,
)
from omnigent.stores.credential_store.local_cipher import (
    CREDENTIAL_ENC_KEY_ENV_VAR,
    CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR,
    LocalSecretCipher,
    build_local_secret_cipher,
    decode_master_key,
    key_fingerprint,
)

CTX_ALICE = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
CTX_BOB = {"workspace_id": "0", "user_id": "bob", "provider": "github", "account_id": ""}


def _key() -> bytes:
    return os.urandom(32)


def test_roundtrip_and_format() -> None:
    key = _key()
    cipher = LocalSecretCipher(key)
    ciphertext = cipher.encrypt('{"access_token": "ghu_secret"}', context=CTX_ALICE)
    assert ciphertext.startswith("env1:local:")
    assert "ghu_secret" not in ciphertext
    origin = describe_ciphertext(ciphertext)
    assert origin is not None and origin.label() == f"local:{key_fingerprint(key)}"
    assert cipher.decrypt(ciphertext, context=CTX_ALICE) == '{"access_token": "ghu_secret"}'


@pytest.mark.parametrize("plaintext", ["", "é" * 200_000], ids=["empty", "large"])
def test_empty_and_large_plaintext_roundtrip(plaintext: str) -> None:
    cipher = LocalSecretCipher(_key())
    assert cipher.decrypt(cipher.encrypt(plaintext, context=CTX_ALICE), context=CTX_ALICE) == (
        plaintext
    )


def test_fresh_nonce_and_data_key_per_encryption() -> None:
    cipher = LocalSecretCipher(_key())
    first = cipher.encrypt("same", context=CTX_ALICE).split(":")
    second = cipher.encrypt("same", context=CTX_ALICE).split(":")
    assert first[:3] == second[:3]  # same version, backend, key id
    assert first[3] != second[3]  # wrapped data key
    assert first[4] != second[4]  # nonce
    assert first[5] != second[5]  # ciphertext


def test_context_mismatch_fails() -> None:
    cipher = LocalSecretCipher(_key())
    ciphertext = cipher.encrypt("ghu_alice", context=CTX_ALICE)
    assert cipher.decrypt(ciphertext, context=CTX_BOB) is None
    assert cipher.decrypt(ciphertext, context={**CTX_ALICE, "account_id": "org1"}) is None


@pytest.mark.parametrize("field", [2, 3, 4, 5], ids=["key_id", "wrapped_key", "nonce", "payload"])
def test_tampering_any_field_fails(field: int) -> None:
    key = _key()
    cipher = LocalSecretCipher(key)
    parts = cipher.encrypt("ghu_secret", context=CTX_ALICE).split(":")
    raw = bytearray(b64url_decode(parts[field]))
    raw[0] ^= 0x01
    parts[field] = b64url_encode(bytes(raw))
    tampered = ":".join(parts)
    if field == 2:
        # A different key id names a key that is not configured: store-wide error.
        with pytest.raises(EnvelopeKeyError):
            cipher.decrypt(tampered, context=CTX_ALICE)
    else:
        assert cipher.decrypt(tampered, context=CTX_ALICE) is None


def test_malformed_envelope_fails() -> None:
    cipher = LocalSecretCipher(_key())
    assert cipher.decrypt("env1:local:abc", context=CTX_ALICE) is None


def test_wrong_key_fails_with_the_key_id() -> None:
    ciphertext = LocalSecretCipher(_key()).encrypt("ghu_secret", context=CTX_ALICE)
    origin = describe_ciphertext(ciphertext)
    assert origin is not None
    with pytest.raises(EnvelopeKeyError, match=origin.key_id):
        LocalSecretCipher(_key()).decrypt(ciphertext, context=CTX_ALICE)


def test_previous_key_decrypts_but_new_writes_use_current() -> None:
    old_key, new_key = _key(), _key()
    old_value = LocalSecretCipher(old_key).encrypt("ghu_old", context=CTX_ALICE)
    rotated = LocalSecretCipher(new_key, [old_key])
    assert rotated.decrypt(old_value, context=CTX_ALICE) == "ghu_old"
    new_value = rotated.encrypt("ghu_new", context=CTX_ALICE)
    assert describe_ciphertext(new_value) == rotated.write_origin
    assert rotated.write_origin is not None
    assert rotated.write_origin.key_id == key_fingerprint(new_key)
    # The old key alone cannot read values written under the new one.
    with pytest.raises(EnvelopeKeyError):
        LocalSecretCipher(old_key).decrypt(new_value, context=CTX_ALICE)


def test_read_only_cipher_refuses_to_encrypt() -> None:
    old_key = _key()
    reader = LocalSecretCipher(None, [old_key])
    assert reader.write_origin is None
    with pytest.raises(EnvelopeKeyError, match=CREDENTIAL_ENC_KEY_ENV_VAR):
        reader.encrypt("x", context=CTX_ALICE)


def test_vault_and_legacy_values_are_foreign() -> None:
    cipher = LocalSecretCipher(_key())
    with pytest.raises(EnvelopeKeyError, match="'vault'"):
        cipher.decrypt("osvault:a2V5:vault:v1:abc", context=CTX_ALICE)
    with pytest.raises(EnvelopeKeyError, match="'kms'"):
        cipher.decrypt(base64.b64encode(b"kms-blob").decode(), context=CTX_ALICE)


# ── key configuration ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "encode",
    [
        lambda raw: base64.b64encode(raw).decode(),
        lambda raw: base64.b64encode(raw).decode().rstrip("="),
        lambda raw: base64.urlsafe_b64encode(raw).decode(),
        lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("="),
        lambda raw: f"  {base64.b64encode(raw).decode()}\n",
    ],
    ids=["standard", "standard-unpadded", "urlsafe", "urlsafe-unpadded", "whitespace"],
)
def test_decode_master_key_accepts_base64_variants(encode) -> None:
    # 0xfb/0xff bytes make standard and urlsafe encodings differ.
    raw = bytes([0xFB, 0xFF] * 16)
    assert decode_master_key(encode(raw), source="X") == raw


@pytest.mark.parametrize(
    "value",
    [
        "not base64 at all!",
        base64.b64encode(b"x" * 16).decode(),
        base64.b64encode(b"x" * 33).decode(),
        "deadbeef" * 8,  # 64 hex chars: `openssl rand -hex 32`, the wrong flag
    ],
    ids=["garbage", "short", "long", "hex"],
)
def test_decode_master_key_rejects_bad_values_with_hint(value: str) -> None:
    with pytest.raises(ValueError, match=r"openssl rand -base64 32") as excinfo:
        decode_master_key(value, source=CREDENTIAL_ENC_KEY_ENV_VAR)
    assert CREDENTIAL_ENC_KEY_ENV_VAR in str(excinfo.value)
    assert value.strip() not in str(excinfo.value)


def test_builder_reads_current_and_previous_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    current, previous = _key(), _key()
    monkeypatch.setenv(CREDENTIAL_ENC_KEY_ENV_VAR, base64.b64encode(current).decode())
    monkeypatch.setenv(
        CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR, f" {base64.b64encode(previous).decode()} ,"
    )
    cipher = build_local_secret_cipher()
    assert cipher is not None
    assert cipher.key_ids == {key_fingerprint(current), key_fingerprint(previous)}
    assert cipher.write_origin is not None
    assert cipher.write_origin.key_id == key_fingerprint(current)


def test_builder_rejects_a_malformed_previous_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_ENC_KEY_ENV_VAR, base64.b64encode(_key()).decode())
    monkeypatch.setenv(CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR, "short")
    with pytest.raises(ValueError, match=CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR):
        build_local_secret_cipher()


def test_builder_disabled_without_current_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CREDENTIAL_ENC_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv(CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR, base64.b64encode(_key()).decode())
    assert build_local_secret_cipher() is None
