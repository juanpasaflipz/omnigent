"""Tests for the GitHub connection store (encrypted at rest)."""

from __future__ import annotations

from omnigent.connections.github import GithubConnectionStore
from omnigent.server.github_app import GitHubTokenSet


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64
        import json

        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64
        import json

        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


def _store(db_uri: str) -> GithubConnectionStore:
    return GithubConnectionStore(db_uri, SecretBox("enc-key"))


def _tokens(access: str = "ghu_a", refresh: str | None = "ghr_a") -> GitHubTokenSet:
    return GitHubTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=1000,
        refresh_token_expires_at=2000,
        scopes="repo",
    )


def test_upsert_and_get_with_tokens(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice@example.com", github_login="alice", github_user_id=7, tokens=_tokens())

    meta = store.get("alice@example.com")
    assert meta is not None
    assert meta.github_login == "alice"
    assert meta.github_user_id == 7
    # Default view hides tokens.
    assert meta.access_token is None
    assert meta.refresh_token is None

    full = store.get("alice@example.com", with_tokens=True)
    assert full is not None
    assert full.access_token == "ghu_a"
    assert full.refresh_token == "ghr_a"


def test_tokens_are_encrypted_at_rest(db_uri: str) -> None:
    """A store with a different key cannot read the tokens back."""
    _store(db_uri).upsert(
        "bob@example.com", github_login="bob", github_user_id=1, tokens=_tokens()
    )
    other = GithubConnectionStore(db_uri, SecretBox("different-key"))
    conn = other.get("bob@example.com", with_tokens=True)
    assert conn is not None
    # Wrong key → decrypt fails softly to None (needs reconnect), not a crash.
    assert conn.access_token is None


def test_upsert_overwrites_preserving_created_at(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens("ghu_1"))
    first = store.get("alice", with_tokens=True)
    assert first is not None
    store.upsert("alice", github_login="alice2", github_user_id=9, tokens=_tokens("ghu_2"))
    second = store.get("alice", with_tokens=True)
    assert second is not None
    assert second.github_login == "alice2"
    assert second.github_user_id == 9
    assert second.access_token == "ghu_2"
    assert second.created_at == first.created_at


def test_update_tokens(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens("ghu_old"))
    store.update_tokens("alice", _tokens("ghu_fresh", refresh="ghr_fresh"))
    conn = store.get("alice", with_tokens=True)
    assert conn is not None
    assert conn.access_token == "ghu_fresh"
    assert conn.refresh_token == "ghr_fresh"


def test_delete(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens())
    assert store.delete("alice") is True
    assert store.get("alice") is None
    assert store.delete("alice") is False


def test_null_refresh_token(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens(refresh=None))
    conn = store.get("alice", with_tokens=True)
    assert conn is not None
    assert conn.refresh_token is None


_CREDENTIAL_BACKEND_ENV = (
    "OMNIGENT_CREDENTIAL_CIPHER",
    "OMNIGENT_CREDENTIAL_ENC_KEY",
    "OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS",
    "OMNIGENT_CREDENTIAL_KMS_KEY_ID",
    "OMNIGENT_CREDENTIAL_VAULT_KEY",
)


def _github_app_env(monkeypatch) -> None:
    from omnigent.server.github_app import GitHubAppConfig

    for name in _CREDENTIAL_BACKEND_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_ID", "Iv1abc")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_REDIRECT_URI", "https://x/cb")
    assert GitHubAppConfig.from_env() is not None


def test_server_wiring_uses_a_local_key(db_uri: str, monkeypatch) -> None:
    """``OMNIGENT_CREDENTIAL_ENC_KEY`` alone enables Connect GitHub (no KMS/Vault)."""
    import base64
    import os

    import sqlalchemy as sa

    from omnigent.connections.github import build_github_connection_store
    from omnigent.db.utils import get_or_create_engine

    _github_app_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENC_KEY", base64.b64encode(os.urandom(32)).decode())

    store = build_github_connection_store(db_uri)
    assert store is not None
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens("ghu_live"))
    with get_or_create_engine(db_uri).connect() as connection:
        stored = connection.execute(sa.text("SELECT secret_enc FROM connections")).scalar_one()
    assert stored.startswith("env1:local:") and "ghu_live" not in stored
    full = store.get("alice", with_tokens=True)
    assert full is not None and full.access_token == "ghu_live"


def test_server_wiring_without_a_backend_names_the_options(
    db_uri: str, monkeypatch, caplog
) -> None:
    from omnigent.connections.github import build_github_connection_store

    _github_app_env(monkeypatch)
    assert build_github_connection_store(db_uri) is None
    assert "GitHub App is configured but disabled" in caplog.text
    for option in (
        "OMNIGENT_CREDENTIAL_ENC_KEY",
        "openssl rand -base64 32",
        "OMNIGENT_CREDENTIAL_KMS_KEY_ID",
        "OMNIGENT_CREDENTIAL_VAULT_KEY",
    ):
        assert option in caplog.text
