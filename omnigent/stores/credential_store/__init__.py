"""Provider-agnostic per-user credential store.

The SQLAlchemy backend (:class:`CredentialStore`) behind the
:class:`SecretCipher` port (local key, AWS KMS, or Vault Transit backends).
Provider façades live in :mod:`omnigent.connections`. See ``designs/CREDENTIAL_STORE.md``.
"""

from omnigent.stores.credential_store.local_cipher import (
    CREDENTIAL_ENC_KEY_ENV_VAR,
    CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR,
    LocalSecretCipher,
)
from omnigent.stores.credential_store.secret_cipher import (
    CREDENTIAL_BACKEND_SETUP_HINT,
    CREDENTIAL_KMS_KEY_ENV_VAR,
    KmsSecretCipher,
    MultiBackendSecretCipher,
    SecretCipher,
    SecretContext,
    build_secret_cipher,
)
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore

__all__ = [
    "CREDENTIAL_BACKEND_SETUP_HINT",
    "CREDENTIAL_ENC_KEYS_PREVIOUS_ENV_VAR",
    "CREDENTIAL_ENC_KEY_ENV_VAR",
    "CREDENTIAL_KMS_KEY_ENV_VAR",
    "CredentialStore",
    "KmsSecretCipher",
    "LocalSecretCipher",
    "MultiBackendSecretCipher",
    "SecretCipher",
    "SecretContext",
    "build_secret_cipher",
]
