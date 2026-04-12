"""
core/key_manager.py

Managed key material for backend runtime signing and platform bootstrap.

- JWT signing uses RSA (RS256) and publishes a JWKS.
- Licensing signing uses Ed25519 and publishes a local trust-anchor JSON.
- Platform bootstrap keys: Fernet encryption key,
  platform internal secret, one-time setup token.

Keys are stored in .data/keys/ and generated exclusively by the init container
before any other service starts. The backend reads keys but never writes them.
If a key file is missing the backend will raise a clear error at startup.
"""

import base64
import logging
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from core.config import KEYS_DIR

logger = logging.getLogger(__name__)

_DEFAULT_PRIVATE_KEY_PATH = KEYS_DIR / "jwt_private.pem"
_DEFAULT_PUBLIC_KEY_PATH = KEYS_DIR / "jwt_public.pem"
_DEFAULT_LICENSING_PRIVATE_KEY_PATH = KEYS_DIR / "licensing_private.pem"
_DEFAULT_LICENSING_PUBLIC_KEY_PATH = KEYS_DIR / "licensing_public.pem"
_DEFAULT_LICENSING_TRUST_ANCHORS_PATH = KEYS_DIR / "licensing_trust_anchors.json"
_DEFAULT_ENCRYPTION_KEY_PATH = KEYS_DIR / "encryption.key"
_DEFAULT_PLATFORM_SECRET_PATH = KEYS_DIR / "platform_internal.secret"
_DEFAULT_SETUP_TOKEN_PATH = KEYS_DIR / "setup.token"

_private_key_pem: Optional[str] = None
_public_key_pem: Optional[str] = None
_key_id: Optional[str] = None
_licensing_private_key_path: Optional[Path] = None
_licensing_public_key_path: Optional[Path] = None
_licensing_trust_anchors_path: Optional[Path] = None
_licensing_key_id: Optional[str] = None
_encryption_key: Optional[str] = None
_platform_internal_secret: Optional[str] = None
_setup_token: Optional[str] = None


def _base64url_unpadded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def init_jwt_keys(
    private_key_path: Optional[Path] = None,
    public_key_path: Optional[Path] = None,
    key_id: str = "s1",
) -> None:
    """
    Load JWT signing keys from disk. Called once at application startup.
    Keys must already exist — generated exclusively by the init container.
    """
    global _private_key_pem, _public_key_pem, _key_id

    priv_path = private_key_path or _DEFAULT_PRIVATE_KEY_PATH
    pub_path = public_key_path or _DEFAULT_PUBLIC_KEY_PATH
    _key_id = key_id

    if not priv_path.exists() or not pub_path.exists():
        raise RuntimeError(
            f"JWT key files not found at {priv_path.parent}/. "
            "Ensure the init container has run before starting the backend."
        )
    _private_key_pem = priv_path.read_text()
    _public_key_pem = pub_path.read_text()
    logger.info("JWT keys loaded (kid=%s)", _key_id)


def init_licensing_keys(
    private_key_path: Optional[Path] = None,
    public_key_path: Optional[Path] = None,
    trust_anchors_path: Optional[Path] = None,
    key_id: Optional[str] = None,
) -> None:
    """Load licensing signing keys from disk. Keys must already exist."""
    global _licensing_private_key_path, _licensing_public_key_path
    global _licensing_trust_anchors_path, _licensing_key_id

    priv_path = private_key_path or _DEFAULT_LICENSING_PRIVATE_KEY_PATH
    pub_path = public_key_path or _DEFAULT_LICENSING_PUBLIC_KEY_PATH
    anchors_path = trust_anchors_path or _DEFAULT_LICENSING_TRUST_ANCHORS_PATH
    resolved_key_id = key_id or "lic-s1"

    if not priv_path.exists() or not pub_path.exists():
        raise RuntimeError(
            f"Licensing key files not found at {priv_path.parent}/. "
            "Ensure the init container has run before starting the backend."
        )

    private_key = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    public_key = serialization.load_pem_public_key(pub_path.read_bytes())
    if not isinstance(private_key, Ed25519PrivateKey):
        raise RuntimeError(f"Licensing private key at '{priv_path}' is not Ed25519.")
    if not isinstance(public_key, Ed25519PublicKey):
        raise RuntimeError(f"Licensing public key at '{pub_path}' is not Ed25519.")
    logger.info("Licensing keys loaded (kid=%s)", resolved_key_id)

    # Trust anchors are written exclusively by the init container (read-only at runtime).
    _licensing_private_key_path = priv_path
    _licensing_public_key_path = pub_path
    _licensing_trust_anchors_path = anchors_path
    _licensing_key_id = resolved_key_id


def get_licensing_private_key_path() -> Path:
    if _licensing_private_key_path is None:
        return _DEFAULT_LICENSING_PRIVATE_KEY_PATH
    return _licensing_private_key_path


def get_licensing_public_key_path() -> Path:
    if _licensing_public_key_path is None:
        return _DEFAULT_LICENSING_PUBLIC_KEY_PATH
    return _licensing_public_key_path


def get_licensing_trust_anchors_path() -> Path:
    if _licensing_trust_anchors_path is None:
        return _DEFAULT_LICENSING_TRUST_ANCHORS_PATH
    return _licensing_trust_anchors_path


def get_licensing_key_id() -> str:
    if _licensing_key_id:
        return _licensing_key_id
    return "lic-s1"


def get_private_key_pem() -> str:
    if not _private_key_pem:
        raise RuntimeError("JWT keys not initialized -- call init_jwt_keys() at startup")
    return _private_key_pem


def get_public_key_pem() -> str:
    if not _public_key_pem:
        raise RuntimeError("JWT keys not initialized -- call init_jwt_keys() at startup")
    return _public_key_pem


def get_key_id() -> str:
    if not _key_id:
        raise RuntimeError("JWT keys not initialized -- call init_jwt_keys() at startup")
    return _key_id


def get_jwk_public() -> dict:
    """
    Return the current public key as a JWK dict (standard OIDC format).
    Served at /.well-known/jwks.json.
    """
    if not _public_key_pem:
        raise RuntimeError("JWT keys not initialized -- call init_jwt_keys() at startup")

    public_key = serialization.load_pem_public_key(_public_key_pem.encode())

    if not isinstance(public_key, RSAPublicKey):
        raise NotImplementedError("Only RSA keys are supported in v1")

    pub_numbers = public_key.public_numbers()

    def _int_to_base64url(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": _key_id,
        "n": _int_to_base64url(pub_numbers.n),
        "e": _int_to_base64url(pub_numbers.e),
    }


# ---------------------------------------------------------------------------
#  Platform bootstrap keys
# ---------------------------------------------------------------------------

def init_encryption_key(path: Optional[Path] = None) -> None:
    """Load Fernet encryption key from disk. Key must already exist."""
    global _encryption_key

    key_path = path or _DEFAULT_ENCRYPTION_KEY_PATH

    if not key_path.exists():
        raise RuntimeError(
            f"Encryption key not found at {key_path}. "
            "Ensure the init container has run before starting the backend."
        )
    _encryption_key = key_path.read_text().strip()
    logger.info("Encryption key loaded from %s", key_path)


def get_encryption_key() -> str:
    if not _encryption_key:
        raise RuntimeError("Encryption key not initialized -- call init_encryption_key() at startup")
    return _encryption_key


def init_platform_secret(path: Optional[Path] = None) -> None:
    """Load platform internal secret for kernel/MCP server auth. File must already exist."""
    global _platform_internal_secret

    secret_path = path or _DEFAULT_PLATFORM_SECRET_PATH

    if not secret_path.exists():
        raise RuntimeError(
            f"Platform internal secret not found at {secret_path}. "
            "Ensure the init container has run before starting the backend."
        )
    _platform_internal_secret = secret_path.read_text().strip()
    logger.info("Platform internal secret loaded from %s", secret_path)


def get_platform_internal_secret() -> str:
    if _platform_internal_secret is None:
        raise RuntimeError("Platform secret not initialized -- call init_platform_secret() at startup")
    return _platform_internal_secret


# ---------------------------------------------------------------------------
#  Inbound nonce secret
# ---------------------------------------------------------------------------

_DEFAULT_NONCE_SECRET_PATH = KEYS_DIR / "inbound_nonce.secret"
_nonce_secret: Optional[str] = None


def init_nonce_secret(path: Optional[Path] = None) -> None:
    """Load inbound nonce HMAC secret from a key file generated by the init container."""
    global _nonce_secret

    secret_path = path or _DEFAULT_NONCE_SECRET_PATH

    if not secret_path.exists():
        raise RuntimeError(
            f"Inbound nonce secret not found at {secret_path}. "
            "Ensure the init container has run before starting the backend."
        )
    _nonce_secret = secret_path.read_text().strip()
    logger.info("Inbound nonce secret loaded from %s", secret_path)


def get_nonce_secret() -> str:
    if not _nonce_secret:
        raise RuntimeError("Inbound nonce secret not initialized -- call init_nonce_secret() at startup")
    return _nonce_secret


def init_setup_token(path: Optional[Path] = None) -> None:
    """
    Load the one-time setup wizard token from disk.

    If the file exists the token is loaded (setup not yet completed).
    If missing, setup has already been completed — _setup_token stays None.
    After setup completion, delete_setup_token() removes the file.
    """
    global _setup_token

    token_path = path or _DEFAULT_SETUP_TOKEN_PATH

    if token_path.exists():
        _setup_token = token_path.read_text().strip()
        logger.info("Setup token loaded from %s", token_path)
    else:
        logger.info("Setup token not present — setup already completed")


def get_setup_token() -> Optional[str]:
    """Return the setup token, or None if setup is already complete (file deleted)."""
    return _setup_token


def delete_setup_token(path: Optional[Path] = None) -> None:
    """Delete the setup token file after setup completion."""
    global _setup_token

    token_path = path or _DEFAULT_SETUP_TOKEN_PATH
    if token_path.exists():
        try:
            token_path.unlink()
            logger.info("Setup token deleted from %s", token_path)
        except PermissionError:
            # Windows may mark the file read-only; strip that attribute first.
            import stat
            token_path.chmod(stat.S_IWRITE)
            token_path.unlink()
            logger.info("Setup token deleted from %s", token_path)
        except OSError as e:
            # Keys directory is mounted read-only (Docker production and dev).
            # The file cannot be removed from within the container. Token is
            # cleared from memory below; DB-backed platform_settings.needs_setup()
            # is authoritative on restart, so the stale file is harmless.
            logger.warning(
                "Setup token file at %s could not be deleted (%s). "
                "Token invalidated in memory; DB state is authoritative on restart.",
                token_path, e,
            )
    _setup_token = None


# ---------------------------------------------------------------------------
#  Infrastructure credentials (ArangoDB, OpenSearch, MinIO)
# ---------------------------------------------------------------------------

_DEFAULT_ARANGO_PASS_PATH = KEYS_DIR / "arango.pass"
_DEFAULT_OPENSEARCH_PASS_PATH = KEYS_DIR / "opensearch.pass"
_DEFAULT_MINIO_PASS_PATH = KEYS_DIR / "minio.pass"

_arango_password: Optional[str] = None
_opensearch_password: Optional[str] = None
_minio_pass: Optional[str] = None


def init_arango_password(path: Optional[Path] = None) -> None:
    """Load ArangoDB root password from key file. File must already exist."""
    global _arango_password
    pass_path = path or _DEFAULT_ARANGO_PASS_PATH
    if not pass_path.exists():
        raise RuntimeError(
            f"ArangoDB password file not found at {pass_path}. "
            "Ensure the init container has run before starting the backend."
        )
    _arango_password = pass_path.read_text().strip()
    logger.info("ArangoDB password loaded from %s", pass_path)


def get_arango_password() -> str:
    if _arango_password is None:
        raise RuntimeError("ArangoDB password not initialized -- call init_arango_password() at startup")
    return _arango_password


def init_opensearch_password(path: Optional[Path] = None) -> None:
    """Load OpenSearch admin password from key file. File must already exist."""
    global _opensearch_password
    pass_path = path or _DEFAULT_OPENSEARCH_PASS_PATH
    if not pass_path.exists():
        raise RuntimeError(
            f"OpenSearch password file not found at {pass_path}. "
            "Ensure the init container has run before starting the backend."
        )
    _opensearch_password = pass_path.read_text().strip()
    logger.info("OpenSearch password loaded from %s", pass_path)


def get_opensearch_password() -> str:
    if _opensearch_password is None:
        raise RuntimeError("OpenSearch password not initialized -- call init_opensearch_password() at startup")
    return _opensearch_password


def init_minio_password(path: Optional[Path] = None) -> None:
    """Load MinIO root password from key file. File must already exist."""
    global _minio_pass
    p_path = path or _DEFAULT_MINIO_PASS_PATH
    if not p_path.exists():
        raise RuntimeError(
            f"MinIO password file not found at {p_path}. "
            "Ensure the init container has run before starting the backend."
        )
    _minio_pass = p_path.read_text().strip()
    logger.info("MinIO password loaded from %s", p_path)


def get_minio_pass() -> str:
    if _minio_pass is None:
        raise RuntimeError("MinIO password not initialized -- call init_minio_password() at startup")
    return _minio_pass
