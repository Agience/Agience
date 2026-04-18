# services/auth_service.py

import fnmatch
import hashlib
import hmac
import base64
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
from jose import jwt, JWTError
from arango.database import StandardDatabase

from core import config
from core.key_manager import get_private_key_pem, get_public_key_pem, get_key_id, get_jwk_public
from entities.api_key import APIKey as APIKeyEntity
from db.arango import (
    get_api_key_by_hash,
    update_api_key_last_used,
    find_artifact_by_context_field,
)

JWT_ALGORITHM = "RS256"
ACCESS_TOKEN_EXPIRE_HOURS = 12

# Password hashing (stdlib only)
_PWD_ALG = "pbkdf2_sha256"


def _pbkdf2_sha256(password: str, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256.

    Stored format:
      pbkdf2_sha256$<iters>$<salt_b64url>$<hash_b64url>
    """
    if not isinstance(password, str) or not password:
        raise ValueError("Password is required")

    iters = int(config.PASSWORD_PBKDF2_ITERS or 200_000)
    salt = secrets.token_bytes(16)
    dk = _pbkdf2_sha256(password, salt, iters)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    dk_b64 = base64.urlsafe_b64encode(dk).decode("ascii").rstrip("=")
    return f"{_PWD_ALG}${iters}${salt_b64}${dk_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not password or not stored_hash:
        return False
    try:
        alg, iters_s, salt_b64, dk_b64 = stored_hash.split("$", 3)
        if alg != _PWD_ALG:
            return False
        iters = int(iters_s)

        # restore base64 padding
        def _pad(s: str) -> str:
            return s + "=" * ((4 - (len(s) % 4)) % 4)

        salt = base64.urlsafe_b64decode(_pad(salt_b64).encode("ascii"))
        expected = base64.urlsafe_b64decode(_pad(dk_b64).encode("ascii"))
        actual = _pbkdf2_sha256(password, salt, iters)
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def dummy_verify_password(password: str) -> None:
    """Spend roughly the same CPU as a real password verify.

    Used to reduce trivial user-enumeration timing differences.
    """
    try:
        iters = int(config.PASSWORD_PBKDF2_ITERS or 200_000)
        salt = b"\x00" * 16
        expected = b"\x00" * 32
        actual = _pbkdf2_sha256(password or "", salt, iters)
        secrets.compare_digest(actual, expected)
    except Exception:
        return

def verify_token(token: str, expected_audience: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Verify and decode a JWT signed with the authority's RSA private key.

    Args:
        token: The raw Bearer token string.
        expected_audience: When provided, the ``aud`` claim must exactly equal
            this value (RFC 7519 §4.1.3).  Pass ``AUTHORITY_ISSUER`` for
            user-facing endpoints and ``"agience"`` for server-credential paths.
            Omit (``None``) only when the caller validates ``aud`` manually
            after decoding (e.g., machine-auth paths that accept multiple token
            types).
    """
    try:
        decode_kwargs: dict = {
            "algorithms": [JWT_ALGORITHM],
            "issuer": config.AUTHORITY_ISSUER,
            "options": {"verify_iss": True},
        }
        if expected_audience is not None:
            decode_kwargs["audience"] = expected_audience
        else:
            # No audience check requested by caller; caller is responsible for
            # post-decode aud validation when accepting multiple token types.
            decode_kwargs["options"]["verify_aud"] = False

        payload = jwt.decode(token, get_public_key_pem(), **decode_kwargs)
        exp = payload.get("exp")
        if exp and datetime.now(timezone.utc).timestamp() > exp:
            return None
        return payload
    except JWTError:
        return None

def get_jwks() -> dict:
    """Return the JSON Web Key Set for this authority's signing key.
    Served at /.well-known/jwks.json in standard OIDC format.
    """
    return {"keys": [get_jwk_public()]}


def is_client_redirect_allowed(uri: str) -> bool:
    """Check if the redirect URI is allowed for the built-in platform client.

    Third-party clients (registered MCP Client artifacts) validate redirect URIs
    against their own registered_uris list — this function is not called for them.
    """
    # Always allow explicit configured URIs.
    allowed_bases = [config.FRONTEND_URI, config.BACKEND_URI]

    # RFC 8252 §7.3: loopback redirects on any port are always permitted for
    # native/desktop OAuth clients (VS Code, Postman, etc.).
    allowed_hosts = {"127.0.0.1", "localhost"}

    # Well-known tool redirect pages that are always safe.
    always_allowed = {
        "https://vscode.dev",        # VS Code web redirect
        "https://oauth.pstmn.io",    # Postman OAuth testing
    }

    # In a local deployment also allow the full localhost port range.
    backend_is_local = (config.BACKEND_URI or "").startswith("http://localhost") or (config.BACKEND_URI or "").startswith("http://127.0.0.1")
    frontend_is_local = (config.FRONTEND_URI or "").startswith("http://localhost") or (config.FRONTEND_URI or "").startswith("http://127.0.0.1")
    if backend_is_local or frontend_is_local:
        allowed_bases.extend([f"http://localhost:{port}" for port in range(3000, 9000)])

    try:
        parsed = urlparse(uri)
        if parsed.scheme not in ("http", "https"):
            return False
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        if base_url in allowed_bases or base_url in always_allowed:
            return True
        # Allow loopback on any port (http only — https loopback is unusual)
        if parsed.scheme == "http" and parsed.hostname in allowed_hosts:
            return True
        return False
    except Exception:
        return False

def issue_delegation_token(server_client_id: str, user_id: str, ttl_seconds: int = 300) -> str:
    """Issue a short-lived delegation JWT: server acting on behalf of a user.

    RFC 8693-style: ``sub`` identifies the user whose resources are being accessed;
    ``act.sub`` identifies the server performing the operation.  ``principal_type``
    is set to ``"delegation"`` so endpoints can distinguish this from plain user or
    server tokens.

    The token carries all four identity-chain entities:
      - User (``sub``)
      - Server (``act.sub`` / ``aud``)
      - Authority (``iss``)
      - Host (``host_id`` — resolved from platform topology)

    Signed with Core's RS256 key — servers verify it against Core's published JWKS.
    Only Core can issue delegation tokens.
    """
    from services.platform_topology import get_id_optional
    from services.bootstrap_types import HOST_ARTIFACT_SLUG

    now = datetime.now(timezone.utc)
    payload = {
        "iss": config.AUTHORITY_ISSUER,
        "sub": user_id,
        # aud is the specific server this token is issued TO.  The server
        # verifies this claim before accepting or presenting the token.
        "aud": server_client_id,
        "act": {"sub": server_client_id},
        "principal_type": "delegation",
        "host_id": get_id_optional(HOST_ARTIFACT_SLUG) or "",
        "iat": now.timestamp(),
        "exp": (now + timedelta(seconds=ttl_seconds)).timestamp(),
    }
    return jwt.encode(
        payload,
        get_private_key_pem(),
        algorithm=JWT_ALGORITHM,
        headers={"kid": get_key_id()},
    )


def create_jwt_token(user_data: dict, expires_hours: int = ACCESS_TOKEN_EXPIRE_HOURS) -> str:
    """Create a JWT signed with the authority's RSA private key (RS256)."""
    payload = user_data.copy()

    if "exp" not in payload:
        exp = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        payload["exp"] = exp.timestamp()

    if "iat" not in payload:
        payload["iat"] = datetime.now(timezone.utc).timestamp()

    if "iss" not in payload:
        payload["iss"] = config.AUTHORITY_ISSUER

    return jwt.encode(
        payload,
        get_private_key_pem(),
        algorithm=JWT_ALGORITHM,
        headers={"kid": get_key_id()},
    )

def generate_pkce_challenge() -> tuple[str, str]:
    """
    Generate PKCE code verifier and code challenge
    Returns: (code_verifier, code_challenge)
    """
    # Generate code verifier (43-128 characters, URL-safe)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
    
    # Generate code challenge (SHA256 hash of verifier, base64url encoded)
    verifier_hash = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(verifier_hash).decode().rstrip('=')
    
    return code_verifier, code_challenge

def verify_pkce_challenge(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    """
    Verify PKCE code challenge against code verifier
    """
    if method == "S256":
        verifier_hash = hashlib.sha256(code_verifier.encode()).digest()
        computed_challenge = base64.urlsafe_b64encode(verifier_hash).decode().rstrip('=')
    elif method == "plain":
        computed_challenge = code_verifier
    else:
        return False
    
    return secrets.compare_digest(computed_challenge, code_challenge)

def create_authorization_response(
    authorization_code: str,
    state: Optional[str] = None,
    redirect_uri: Optional[str] = None
) -> str:
    """Create OAuth2 authorization response URL"""
    from urllib.parse import urlencode
    
    params = {"code": authorization_code}
    if state:
        params["state"] = state
    
    return f"{redirect_uri}?{urlencode(params)}"

def create_error_response(
    error: str,
    error_description: Optional[str] = None,
    state: Optional[str] = None,
    redirect_uri: Optional[str] = None
) -> str:
    """Create OAuth2 error response URL"""
    from urllib.parse import urlencode
    
    params = {"error": error}
    if error_description:
        params["error_description"] = error_description
    if state:
        params["state"] = state
    
    return f"{redirect_uri}?{urlencode(params)}"

def _match_exact_or_glob(value: str, exact: set[str], patterns: list[str]) -> bool:
    if value in exact:
        return True
    for pat in patterns:
        if fnmatch.fnmatchcase(value, pat):
            return True
    return False

def is_person_allowed(google_id: str | None, email: str | None) -> bool:
    """Check if a person is allowed access based on current allow-lists.

    Access control lists are read from config at call time so that values
    loaded from the DB during Phase 2 are always used (not import-time snapshots).
    """
    allowed_emails = config.ALLOWED_EMAILS or []
    allowed_domains = config.ALLOWED_DOMAINS or []
    allowed_google_ids = config.ALLOWED_GOOGLE_IDS or []

    # Default-allow when nothing is configured
    if not (allowed_emails or allowed_domains or allowed_google_ids):
        return True

    # Global wildcard — any list containing "*" opens access to everyone
    if "*" in allowed_emails or "*" in allowed_domains or "*" in allowed_google_ids:
        return True

    allowed_gids = set(allowed_google_ids)
    gid_ok = bool(google_id) and (google_id in allowed_gids)

    email_l = (email or "").lower()
    domain = email_l.split("@")[-1] if "@" in email_l else ""

    exact_emails = {e.lower() for e in allowed_emails if "*" not in e}
    email_patterns = [e.lower() for e in allowed_emails if "*" in e]
    exact_domains = {d.lower() for d in allowed_domains if "*" not in d}
    domain_patterns = [d.lower() for d in allowed_domains if "*" in d]

    email_ok = bool(email_l) and _match_exact_or_glob(email_l, exact_emails, email_patterns)
    domain_ok = bool(domain) and _match_exact_or_glob(domain, exact_domains, domain_patterns)

    return gid_ok or email_ok or domain_ok


# OAuth2 error constants
class OAuth2Error:
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED_CLIENT = "unauthorized_client"
    ACCESS_DENIED = "access_denied"
    UNSUPPORTED_RESPONSE_TYPE = "unsupported_response_type"
    INVALID_SCOPE = "invalid_scope"
    SERVER_ERROR = "server_error"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INVALID_CLIENT = "invalid_client"
    INVALID_GRANT = "invalid_grant"
    UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"


# ---------- API Key Functions ----------

def generate_api_key() -> str:
    """
    Generate a secure random API key.
    Format: agc_<32 hex chars> (128 bits of entropy)
    
    Returns:
        The raw API key (only shown once - must be stored by user)
    """
    random_bytes = secrets.token_bytes(16)
    key_hex = random_bytes.hex()
    return f"agc_{key_hex}"


def hash_api_key(api_key: str) -> str:
    """
    Hash an API key for storage.
    Uses SHA-256 for security.
    
    Args:
        api_key: The raw API key
    
    Returns:
        The hashed key (hex string)
    """
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(db: StandardDatabase, api_key: str) -> Optional[APIKeyEntity]:
    """
    Verify an API key and return the entity if valid.
    Also updates the last_used_at timestamp.
    
    Args:
        db: ArangoDB database connection
        api_key: The raw API key from Authorization header
    
    Returns:
        The APIKey entity if valid, None otherwise
    """
    # Hash the provided key
    key_hash = hash_api_key(api_key)
    
    # Look up the key
    api_key_entity = get_api_key_by_hash(db, key_hash)
    
    if not api_key_entity:
        return None

    # Check active status
    if not getattr(api_key_entity, "is_active", True):
        return None

    # Check expiration
    expires_at = getattr(api_key_entity, "expires_at", None)
    if expires_at:
        try:
            if isinstance(expires_at, str):
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                exp_dt = expires_at
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                return None
        except (ValueError, TypeError):
            pass
    
    # Update last used timestamp (fire and forget)
    now = datetime.now(timezone.utc).isoformat()
    update_api_key_last_used(db, api_key_entity.id, now)

    return api_key_entity


# ---------------------------------------------------------------------------
# Inbound nonce — stateless HMAC challenge tokens for public-facing API keys.
#
# Nonce payload:  {ts}:{artifact_id}:{key_id}
# Signature:      HMAC-SHA256(INBOUND_NONCE_SECRET, payload)
# Wire format:    base64url({payload}:{sig})
#
# The nonce is bound to a specific (artifact_id, key_id) pair so it cannot
# be replayed against a different endpoint even within the TTL window.
# ---------------------------------------------------------------------------

NONCE_TTL_SECONDS: int = 1800  # 30 minutes


def issue_nonce(key_id: str, artifact_id: str, secret: str) -> Tuple[str, datetime]:
    """Issue a stateless HMAC-signed nonce bound to (artifact_id, key_id).

    Returns (nonce_token, expires_at).  Nothing is persisted.
    """
    if not secret:
        raise ValueError("INBOUND_NONCE_SECRET is not configured")
    ts = str(int(time.time()))
    payload = f"{ts}:{artifact_id}:{key_id}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}:{sig}".encode("utf-8")).decode("utf-8")
    expires_at = datetime.fromtimestamp(int(ts) + NONCE_TTL_SECONDS, tz=timezone.utc)
    return token, expires_at


def verify_nonce(
    token: str,
    key_id: str,
    artifact_id: str,
    secret: str,
    ttl_seconds: int = NONCE_TTL_SECONDS,
) -> bool:
    """Verify a nonce token issued by ``issue_nonce``.

    Returns True if the token is structurally valid, the HMAC matches, the
    binding (artifact_id, key_id) is correct, and the token has not expired.
    Returns False instead of raising so callers decide the HTTP status.
    """
    if not secret or not token:
        return False
    try:
        padding = "=" * (4 - len(token) % 4)
        decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8")
        parts = decoded.split(":", 3)
        if len(parts) != 4:
            return False
        ts_str, nonce_artifact_id, nonce_key_id, sig = parts
        ts = int(ts_str)
    except Exception:
        return False

    # Binding check
    if nonce_artifact_id != artifact_id or nonce_key_id != key_id:
        return False

    # Expiry check
    if int(time.time()) - ts > ttl_seconds:
        return False

    # HMAC check (constant-time)
    expected_payload = f"{ts_str}:{artifact_id}:{key_id}"
    expected_sig = hmac.new(
        secret.encode("utf-8"), expected_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return secrets.compare_digest(sig, expected_sig)


def find_mcp_client_by_client_id(
    db: StandardDatabase,
    client_id: str,
) -> Optional[list[str]]:
    """Look up a registered MCP Client artifact by its client_id context field.

    Returns the artifact's registered redirect_uris list if found, or None if the
    client_id does not match any known artifact. The caller is responsible for
    checking whether a specific redirect_uri is in the returned list.
    """
    import json as _json

    artifact = find_artifact_by_context_field(
        db,
        "client_id",
        client_id,
        content_type="application/vnd.agience.mcp-client+json",
    )
    if not artifact:
        return None
    try:
        ctx = _json.loads(artifact.context) if isinstance(artifact.context, str) else (artifact.context or {})
    except Exception:
        ctx = {}
    return ctx.get("redirect_uris") or []


# Safe default scopes granted to third-party MCP OAuth clients that have not
# declared explicit allowed_oauth_scopes in their artifact context.
_MCP_CLIENT_DEFAULT_SCOPES = ["read"]


def get_mcp_client_allowed_scopes(db: StandardDatabase, client_id: str) -> list[str]:
    """Return the OAuth scopes an MCP Client artifact is permitted to receive.

    Reads ``allowed_oauth_scopes`` from the artifact context.  Falls back to
    ``_MCP_CLIENT_DEFAULT_SCOPES`` if the field is absent or the artifact is
    not found.
    """
    import json as _json

    artifact = find_artifact_by_context_field(
        db,
        "client_id",
        client_id,
        content_type="application/vnd.agience.mcp-client+json",
    )
    if not artifact:
        return list(_MCP_CLIENT_DEFAULT_SCOPES)
    try:
        ctx = _json.loads(artifact.context) if isinstance(artifact.context, str) else (artifact.context or {})
    except Exception:
        ctx = {}
    declared = ctx.get("allowed_oauth_scopes")
    if declared and isinstance(declared, list):
        return [str(s) for s in declared if s]
    return list(_MCP_CLIENT_DEFAULT_SCOPES)