"""
agience-server-seraph � MCP Server
====================================
Security, Governance & Trust: access control, audit trails, identity verification,
policy compliance, cryptographic signing, and Authorizer runtime (OAuth + Crypto).

Seraph is a kernel-tier server � Core cannot connect to other servers
without Seraph resolving auth.  This creates an intentional dependency:
Core's mcp_service has an explicit recursion guard that skips auth
resolution when calling Seraph itself.

Seraph has two distinct roles:
  1. Credential application: resolves secrets to auth headers/tokens.
     Decryption is delegated to Core's ``/secrets/{id}/decrypt`` endpoint �
     Seraph does NOT hold the DATA_ENCRYPTION_KEY.
  2. Authorizer: knows OAuth providers (Google auth), handles OAuth flows.

Tools
-----
  provide_access_token      � Exchange stored refresh token for a fresh access token
  complete_authorizer_oauth � Complete OAuth code exchange and store refresh token
  resolve_llm_credentials   � Resolve LLM Connection credentials to a decrypted API key
  audit_access              � Query the access audit log
  check_permissions         � Check access grants
  grant_access              � Grant collection access
  revoke_access             � Revoke access
  rotate_api_key            � Rotate a scoped API key
  verify_token              � Verify JWT or API key
  list_audit_events         � List security events
  sign_card                 � Cryptographic signature
  enforce_policy            � Evaluate policies
  list_policies             � List governance policies
  check_compliance          � Check compliance

Auth
----
  PLATFORM_INTERNAL_SECRET  ⬩ Shared deployment secret for client_credentials token exchange
  AGIENCE_API_URI           ⬩ Base URI of the agience-core backend

Transport
---------
  MCP_TRANSPORT=streamable-http (default for Agience)
  MCP_HOST=0.0.0.0
  MCP_PORT=8089
"""

from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import os
import pathlib
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("agience-server-seraph")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

AGIENCE_API_URI: str = os.getenv("AGIENCE_API_URI", "http://localhost:8081").rstrip("/")
PLATFORM_INTERNAL_SECRET: str | None = os.getenv("PLATFORM_INTERNAL_SECRET")
SERAPH_CLIENT_ID: str = "agience-server-seraph"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8089"))


# ---------------------------------------------------------------------------
# Shared authentication infrastructure (AgieceServerAuth)
# ---------------------------------------------------------------------------

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent.parent / "_shared"))
from agience_server_auth import AgieceServerAuth as _AgieceServerAuth

_auth = _AgieceServerAuth(SERAPH_CLIENT_ID, AGIENCE_API_URI)


def create_seraph_app():
    """Return the Seraph MCP ASGI app with verified middleware and startup hooks."""
    return _auth.create_app(mcp, _exchange_token)


# ---------------------------------------------------------------------------
# Platform auth — client_credentials token exchange (server identity)
# ---------------------------------------------------------------------------

_token_state: dict = {"access_token": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()


async def _exchange_token() -> str | None:
    """Exchange kernel credentials for a platform JWT; refreshes 60 s before expiry."""
    if not PLATFORM_INTERNAL_SECRET:
        return None

    import time

    async with _token_lock:
        if _token_state["access_token"] and time.time() < _token_state["expires_at"] - 60:
            return _token_state["access_token"]

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{AGIENCE_API_URI}/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": SERAPH_CLIENT_ID,
                    "client_secret": PLATFORM_INTERNAL_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            body = resp.json()

        token = body["access_token"]
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        _token_state["access_token"] = token
        _token_state["expires_at"] = float(payload.get("exp", time.time() + 43200))
        return token


async def _headers() -> dict[str, str]:
    """Return authorization headers using the server's own platform token."""
    h = {"Content-Type": "application/json"}
    token = await _exchange_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _user_headers() -> dict[str, str]:
    """Return headers with the verified delegation JWT, or fall back to server token."""
    return await _auth.user_headers(_exchange_token)


# ---------------------------------------------------------------------------
# Secret resolution helpers
# ---------------------------------------------------------------------------


async def _fetch_and_decrypt_secret(
    secret_id: str | None = None,
    authorizer_id: str | None = None,
    secret_type: str | None = None,
    provider: str | None = None,
) -> str | None:
    """Fetch a secret from Core via the delegation JWT and decrypt it locally.

    Core validates the delegation JWT, looks up the secret under the user
    identity, re-encrypts it as a JWE for this server's public RSA key, and
    returns the envelope.  Seraph decrypts with its private key so plaintext
    never transits unencrypted.
    """
    body: dict = {}
    if secret_id:
        body["secret_id"] = secret_id
    if authorizer_id:
        body["authorizer_id"] = authorizer_id
    if secret_type:
        body["type"] = secret_type
    if provider:
        body["provider"] = provider

    user_hdrs = await _user_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/secrets/fetch",
            headers=user_hdrs,
            json=body,
        )
    if resp.status_code != 200:
        log.error("Failed to fetch secret: %s %s", resp.status_code, resp.text[:200])
        return None
    try:
        return _auth.decrypt_jwe(resp.json()["jwe"])
    except Exception as e:
        log.error("JWE decryption failed: %s", e)
        return None


async def _list_secrets_metadata(
    secret_id: str | None = None,
    authorizer_id: str | None = None,
    secret_type: str | None = None,
) -> dict | None:
    """Fetch secret metadata (no plaintext) from Core using the delegation JWT."""
    params: dict[str, str] = {}
    if secret_id:
        params["id"] = secret_id
    if authorizer_id:
        params["authorizer_id"] = authorizer_id
    if secret_type:
        params["type"] = secret_type

    headers = await _user_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{AGIENCE_API_URI}/secrets", params=params, headers=headers)
    if resp.status_code != 200:
        log.error("Failed to list secrets: %s %s", resp.status_code, resp.text[:200])
        return None
    secrets = resp.json()
    return secrets[0] if secrets else None


async def _store_or_rotate_bearer_token(
    authorizer_artifact_id: str,
    provider: str,
    access_token: str,
    expires_in: int | None,
) -> None:
    """Delete any existing bearer_token for this authorizer and store the new one.

    Called both after a fresh OAuth2 code exchange and after a refresh-token
    exchange so that ``_resolve_auth_headers`` always finds a cached token.
    """
    from datetime import datetime, timezone, timedelta

    expires_at = ""
    if expires_in:
        try:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        except (ValueError, TypeError):
            pass

    headers = await _user_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        # Delete any previously cached bearer token for this authorizer
        existing = await client.get(
            f"{AGIENCE_API_URI}/secrets",
            params={"authorizer_id": authorizer_artifact_id, "type": "bearer_token"},
            headers=headers,
        )
        if existing.status_code == 200:
            for sec in existing.json():
                await client.delete(f"{AGIENCE_API_URI}/secrets/{sec['id']}", headers=headers)

        # Store the fresh token
        await client.post(
            f"{AGIENCE_API_URI}/secrets",
            headers=headers,
            json={
                "type": "bearer_token",
                "provider": provider,
                "label": f"Access token for {authorizer_artifact_id}",
                "value": access_token,
                "authorizer_id": authorizer_artifact_id,
                "expires_at": expires_at,
            },
        )


mcp = FastMCP(
    "agience-server-seraph",
    instructions=(
        "You are Seraph, the security, governance, and trust guardian of the Agience platform. "
        "You enforce access policies, maintain audit trails, verify identities, ensure policy "
        "compliance, and protect the integrity of knowledge through cryptographic signing. "
        "Treat every access control decision as consequential � revoke first, escalate later."
    ),
)
from artifact_helpers import register_types_manifest
register_types_manifest(mcp, "seraph", __file__)

# ---------------------------------------------------------------------------
# Authorizer tools
# ---------------------------------------------------------------------------

@mcp.tool(description="Exchange an Authorizer's stored refresh token for a fresh access token")
async def provide_access_token(
    authorizer_config: str,
    authorizer_artifact_id: str,
) -> str:
    """Decrypt the client secret, fetch the stored refresh token, exchange for access token.

    Args:
        authorizer_config: JSON string of the Authorizer artifact's content
            (contains client_id, client_secret_id, token_endpoint, scopes, sender_address).
        authorizer_artifact_id: Artifact ID of the Authorizer artifact.

    User identity is carried at the transport layer via the delegation JWT set
    by Core before calling this tool.  No bearer token should be passed as an
    argument.
    """
    try:
        config = _json.loads(authorizer_config) if isinstance(authorizer_config, str) else authorizer_config
    except Exception:
        return _json.dumps({"error": "Invalid authorizer_config JSON"})

    # Bearer-only authorizers: return stored access token directly (no refresh flow)
    token_response_type = config.get("token_response_type", "standard")
    requires_client_credentials = config.get("requires_client_credentials", True)

    if token_response_type == "bearer_only" or not requires_client_credentials:
        bt_secret = await _list_secrets_metadata(
            authorizer_id=authorizer_artifact_id,
            secret_type="bearer_token",
        )
        if not bt_secret:
            return _json.dumps({
                "error": "token_expired",
                "reauth_required": True,
                "message": "No bearer token found. User must re-authenticate.",
            })

        access_token = await _fetch_and_decrypt_secret(secret_id=bt_secret["id"])
        if not access_token:
            return _json.dumps({"error": "Failed to decrypt bearer token"})

        # Check expiry
        expires_at = bt_secret.get("expires_at", "")
        if expires_at:
            from datetime import datetime, timezone
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp_dt < datetime.now(timezone.utc):
                    return _json.dumps({
                        "error": "token_expired",
                        "reauth_required": True,
                        "message": "Bearer token has expired. User must re-authenticate.",
                    })
            except Exception:
                pass  # can't parse expiry, return token anyway

        return _json.dumps({"access_token": access_token})

    # Standard OAuth2 refresh flow
    client_id = config.get("client_id")
    client_secret_id = config.get("client_secret_id")
    token_endpoint = config.get("token_endpoint")
    scopes = config.get("scopes", "")
    sender_address = config.get("sender_address", "")

    if not all([client_id, client_secret_id, token_endpoint]):
        return _json.dumps({"error": "authorizer_config missing required fields (client_id, client_secret_id, token_endpoint)"})

    # 1. Decrypt client_secret via JWE
    client_secret = await _fetch_and_decrypt_secret(secret_id=client_secret_id)
    if not client_secret:
        return _json.dumps({"error": "Client secret not found or failed to decrypt"})

    # 2. Fetch the refresh token (stored with authorizer_id reference)
    rt_secret = await _list_secrets_metadata(
        authorizer_id=authorizer_artifact_id,
        secret_type="oauth_refresh_token",
    )
    if not rt_secret:
        return _json.dumps({"error": "No refresh token found for this authorizer. Connect the account first."})
    refresh_token = await _fetch_and_decrypt_secret(secret_id=rt_secret["id"])
    if not refresh_token:
        return _json.dumps({"error": "Failed to decrypt refresh token"})

    # 3. Exchange refresh token for access token
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "scope": scopes,
            },
        )

    if resp.status_code != 200:
        return _json.dumps({"error": f"Token exchange failed: {resp.status_code} {resp.text[:200]}"})

    token_data = resp.json()
    access_token = token_data.get("access_token")
    provider = config.get("provider", "google")

    # If a new refresh token was returned, rotate the stored one
    new_refresh = token_data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        headers = await _user_headers()
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(f"{AGIENCE_API_URI}/secrets/{rt_secret['id']}", headers=headers)
            await client.post(
                f"{AGIENCE_API_URI}/secrets",
                headers=headers,
                json={
                    "type": "oauth_refresh_token",
                    "provider": provider,
                    "label": f"Refresh token for {sender_address or authorizer_artifact_id}",
                    "value": new_refresh,
                    "authorizer_id": authorizer_artifact_id,
                },
            )

    # Cache the new access token
    if access_token:
        await _store_or_rotate_bearer_token(
            authorizer_artifact_id=authorizer_artifact_id,
            provider=provider,
            access_token=access_token,
            expires_in=token_data.get("expires_in"),
        )

    return _json.dumps({"access_token": access_token, "sender_address": sender_address})


@mcp.tool(description="Complete the OAuth authorization code exchange for an Authorizer artifact")
async def complete_authorizer_oauth(
    authorizer_config: str,
    authorizer_artifact_id: str,
    authorization_code: str,
    code_verifier: str,
    redirect_uri: str,
) -> str:
    """Exchange an OAuth authorization code for tokens and store the refresh token.

    Args:
        authorizer_config: JSON content of the Authorizer artifact.
        authorizer_artifact_id: Artifact ID of the Authorizer.
        authorization_code: The OAuth authorization code from the callback.
        code_verifier: The PKCE code verifier.
        redirect_uri: The redirect URI used in the original authorization request.

    User identity is carried at the transport layer via the delegation JWT.
    """
    try:
        config = _json.loads(authorizer_config) if isinstance(authorizer_config, str) else authorizer_config
    except Exception:
        return _json.dumps({"error": "Invalid authorizer_config JSON"})

    client_id = config.get("client_id")
    client_secret_id = config.get("client_secret_id")
    token_endpoint = config.get("token_endpoint")
    provider = config.get("provider", "google")

    if not all([client_id, client_secret_id, token_endpoint]):
        return _json.dumps({"error": "authorizer_config missing required fields"})

    # Decrypt client_secret via JWE
    client_secret = await _fetch_and_decrypt_secret(secret_id=client_secret_id)
    if not client_secret:
        return _json.dumps({"error": "Client secret not found or failed to decrypt"})

    # Exchange authorization code for tokens
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )

    if resp.status_code != 200:
        return _json.dumps({"error": f"Token exchange failed: {resp.status_code} {resp.text[:200]}"})

    token_data = resp.json()
    refresh_token = token_data.get("refresh_token")

    if not refresh_token:
        return _json.dumps({"error": "No refresh_token in token response. Ensure offline access is requested."})

    # Store refresh token as a secret with authorizer_id delegation
    async with httpx.AsyncClient(timeout=10) as client:
        store_resp = await client.post(
            f"{AGIENCE_API_URI}/secrets",
            headers=await _user_headers(),
            json={
                "type": "oauth_refresh_token",
                "provider": provider,
                "label": f"OAuth refresh for {authorizer_artifact_id}",
                "value": refresh_token,
                "authorizer_id": authorizer_artifact_id,
            },
        )

    if store_resp.status_code >= 400:
        return _json.dumps({"error": f"Failed to store refresh token: {store_resp.text[:200]}"})

    # Cache the initial access token so the first tool call works without an
    # extra round-trip to provide_access_token.
    initial_access_token = token_data.get("access_token")
    if initial_access_token:
        await _store_or_rotate_bearer_token(
            authorizer_artifact_id=authorizer_artifact_id,
            provider=provider,
            access_token=initial_access_token,
            expires_in=token_data.get("expires_in"),
        )

    return _json.dumps({"status": "connected", "authorizer_artifact_id": authorizer_artifact_id})


@mcp.tool(description="Complete a bearer-token-only authorization code exchange (no refresh token)")
async def complete_authorizer_bearer(
    authorizer_config: str,
    authorizer_artifact_id: str,
    authorization_code: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
) -> str:
    """Exchange an authorization code for a bearer token (no refresh token flow).

    For providers that return only an access_token with no refresh token
    (e.g. JarvisGPT). The access token is stored as a secret with an
    expiry timestamp; when it expires the user must re-authenticate.

    Args:
        authorizer_config: JSON content of the Authorizer artifact.
        authorizer_artifact_id: Artifact ID of the Authorizer.
        authorization_code: The authorization code from the callback.
        redirect_uri: The redirect URI used in the original request.
        code_verifier: Optional PKCE code verifier.

    User identity is carried at the transport layer via the delegation JWT.
    """
    try:
        config = _json.loads(authorizer_config) if isinstance(authorizer_config, str) else authorizer_config
    except Exception:
        return _json.dumps({"error": "Invalid authorizer_config JSON"})

    token_endpoint = config.get("token_endpoint")
    if not token_endpoint:
        return _json.dumps({"error": "authorizer_config missing token_endpoint"})

    client_id = config.get("client_id")
    provider = config.get("provider", "external")

    # Build token exchange payload
    token_data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": redirect_uri,
    }
    if client_id:
        token_data["client_id"] = client_id
    if code_verifier:
        token_data["code_verifier"] = code_verifier

    # Optionally decrypt client secret via Core
    client_secret_id = config.get("client_secret_id")
    if client_secret_id:
        decrypted_cs = await _fetch_and_decrypt_secret(secret_id=client_secret_id)
        if decrypted_cs:
            token_data["client_secret"] = decrypted_cs

    # Exchange authorization code for access token
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(token_endpoint, data=token_data)

    if resp.status_code != 200:
        return _json.dumps({"error": f"Token exchange failed: {resp.status_code} {resp.text[:200]}"})

    result = resp.json()
    access_token = result.get("access_token")
    if not access_token:
        return _json.dumps({"error": "No access_token in token response"})

    await _store_or_rotate_bearer_token(
        authorizer_artifact_id=authorizer_artifact_id,
        provider=provider,
        access_token=access_token,
        expires_in=result.get("expires_in"),
    )

    return _json.dumps({
        "status": "connected",
        "authorizer_artifact_id": authorizer_artifact_id,
    })


# ---------------------------------------------------------------------------
# LLM credential resolution
# ---------------------------------------------------------------------------

# Provider-specific env var names for platform-default fallback
_LLM_PROVIDER_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "google": "GOOGLE_AI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}


@mcp.tool(
    description=(
        "Resolve an LLM Connection artifact's credentials to a decrypted API key. "
        "Resolution chain: (1) exact secret_id, (2) user's default for provider, "
        "(3) platform env var. Called by Verso at LLM invocation time."
    )
)
async def resolve_llm_credentials(
    credentials_ref: str,
) -> str:
    """Resolve LLM credentials from a connection artifact's credentials_ref.

    Args:
        credentials_ref: JSON string of the credentials_ref object from the LLM Connection
            artifact context (contains secret_id, secret_type, provider, resolution).

    User identity is carried at the transport layer via the delegation JWT.
    """
    try:
        cref = _json.loads(credentials_ref) if isinstance(credentials_ref, str) else credentials_ref
    except Exception:
        return _json.dumps({"error": "Invalid credentials_ref JSON"})

    secret_id = cref.get("secret_id")
    secret_type = cref.get("secret_type", "llm_key")
    provider = cref.get("provider", "openai")
    resolution = cref.get("resolution", "platform_default")

    api_key = None
    source = None

    # 1. If exact secret_id is provided, decrypt via JWE
    if secret_id and resolution in ("user_secret", "workspace_secret"):
        api_key = await _fetch_and_decrypt_secret(secret_id=secret_id)
        if api_key:
            source = "user_secret"

    # 2. If no key yet, try user's default secret for this provider
    if not api_key and resolution != "platform_default":
        secret = await _list_secrets_metadata(
            secret_type=secret_type,
        )
        if secret:
            # Match provider if multiple secrets exist
            if secret.get("provider") == provider or not secret.get("provider"):
                api_key = await _fetch_and_decrypt_secret(secret_id=secret["id"])
                if api_key:
                    source = "user_default"

    # 3. Fallback to platform env var
    if not api_key:
        env_key = _LLM_PROVIDER_ENV_KEYS.get(provider)
        env_val = os.getenv(env_key) if env_key else None
        if env_val:
            api_key = env_val
            source = "platform_default"

    if not api_key:
        return _json.dumps({
            "error": f"No API key found for provider '{provider}'. "
                     "Add your own key via Settings or contact your administrator.",
            "resolution_source": None,
        })

    return _json.dumps({
        "api_key": api_key,
        "resolution_source": source,
    })


# ---------------------------------------------------------------------------
# AWS credential decryption
# ---------------------------------------------------------------------------

@mcp.tool(description="Decrypt and return AWS credentials for a credential artifact")
async def provide_aws_credentials(
    credential_artifact_id: str,
    workspace_id: str,
) -> str:
    """Fetch an AWS credentials artifact, decrypt the secret access key, and return both credentials.

    The credential artifact is a plain application/json artifact whose context
    stores aws_access_key_id and a reference (secret_id) to the encrypted
    aws_secret_access_key held as a Seraph-managed secret.

    Args:
        credential_artifact_id: Artifact ID of the application/json credential artifact.
        workspace_id: Workspace containing the credential artifact.
    """
    # 1. Fetch the credential artifact to get context fields.
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{credential_artifact_id}",
            headers=await _headers(),
        )
    if resp.status_code != 200:
        return _json.dumps({"error": f"Failed to fetch credential artifact: {resp.status_code}"})

    artifact = resp.json()
    ctx = artifact.get("context", {})
    if isinstance(ctx, str):
        try:
            ctx = _json.loads(ctx)
        except Exception:
            return _json.dumps({"error": "Invalid credential artifact context"})

    aws_access_key_id = ctx.get("aws_access_key_id")
    secret_id = ctx.get("secret_id")
    aws_region = ctx.get("aws_region", "us-east-1")

    if not aws_access_key_id or not secret_id:
        return _json.dumps({"error": "Credential artifact missing aws_access_key_id or secret_id"})

    # 2. Decrypt the secret access key using the stored delegation context or
    # the server's own platform credentials.
    aws_secret_access_key = await _fetch_and_decrypt_secret(secret_id=secret_id)
    if not aws_secret_access_key:
        return _json.dumps({"error": "AWS secret access key not found or could not be decrypted"})

    # 3. Return credentials (transient � not stored or logged).
    return _json.dumps({
        "aws_access_key_id": aws_access_key_id,
        "aws_secret_access_key": aws_secret_access_key,
        "aws_region": aws_region,
    })


# ---------------------------------------------------------------------------
# Security & governance tool stubs
# ---------------------------------------------------------------------------

@mcp.tool(description="Query the access audit log for a resource, collection, or user")
async def audit_access(
    resource_id: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Return audit log entries filtered by resource or user."""
    return "TODO: audit_access not yet implemented."


@mcp.tool(description="Check what a person or API key can access")
async def check_permissions(
    subject: str,
) -> str:
    """Return the access grants held by a person ID or API key prefix."""
    return "TODO: check_permissions not yet implemented."


@mcp.tool(description="Grant access to a collection for a person or team")
async def grant_access(
    collection_id: str,
    grantee: str,
    role: str = "viewer",
) -> str:
    """Grant the specified role on a collection to a grantee (person ID or email)."""
    return "TODO: grant_access not yet implemented."


@mcp.tool(description="Revoke access to a collection")
async def revoke_access(
    collection_id: str,
    grantee: str,
) -> str:
    """Remove all grants on a collection for the specified grantee."""
    return "TODO: revoke_access not yet implemented."


@mcp.tool(description="Rotate a scoped API key and return the new key details")
async def rotate_api_key(
    key_id: str,
) -> str:
    """Rotate (invalidate and reissue) a scoped API key by ID."""
    return "TODO: rotate_api_key not yet implemented."


@mcp.tool(description="Verify a JWT or API key and return its decoded claims")
async def verify_token(
    token: str,
) -> str:
    """Verify the supplied token against the platform JWKS and return its claims."""
    return "TODO: verify_token not yet implemented."


@mcp.tool(description="List recent security events (logins, grants, revocations, key usage)")
async def list_audit_events(
    limit: int = 20,
    event_type: Optional[str] = None,
) -> str:
    """Return recent platform security events, optionally filtered by event type."""
    return "TODO: list_audit_events not yet implemented."


@mcp.tool(description="Create a tamper-evident cryptographic signature card for a card")
async def sign_card(
    artifact_id: str,
    workspace_id: str,
) -> str:
    """Compute a hash of the card content and create a signature card referencing it."""
    return "TODO: sign_card not yet implemented."


@mcp.tool(description="Evaluate a request or card against active system policies")
async def enforce_policy(
    resource_id: str,
    action: str,
    context: Optional[str] = None,
) -> str:
    """Check whether a proposed action on a resource is allowed by current policies."""
    return "TODO: enforce_policy not yet implemented."


@mcp.tool(description="List active governance policies")
async def list_policies(
    scope: Optional[str] = None,
) -> str:
    """Return active policies, optionally filtered by scope (e.g. 'collection', 'workspace')."""
    return "TODO: list_policies not yet implemented."


@mcp.tool(description="Check compliance of a resource or workflow against governance rules")
async def check_compliance(
    resource_id: str,
    workspace_id: Optional[str] = None,
) -> str:
    """Assess whether a resource or workflow meets all applicable compliance requirements."""
    return "TODO: check_compliance not yet implemented."


# ---------------------------------------------------------------------------
# UI Resources
# ---------------------------------------------------------------------------

@mcp.resource("ui://seraph/vnd.agience.key.html")
async def key_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.key+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.key+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://seraph/vnd.agience.authority.html")
async def authority_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.authority+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.authority+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://seraph/vnd.agience.authorizer.html")
async def authorizer_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.authorizer+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.authorizer+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://seraph/vnd.agience.signature.html")
async def signature_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.signature+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.signature+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Standard server interface (used by _host and standalone)
# ---------------------------------------------------------------------------

def create_server_app():
    """Return the Seraph ASGI app with verified middleware and startup hooks."""
    return create_seraph_app()


async def server_startup() -> None:
    """Run Seraph startup tasks: Core JWKS fetch + server key registration."""
    await _auth.startup(_exchange_token)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting agience-server-seraph � transport=%s port=%s", MCP_TRANSPORT, MCP_PORT)
    if MCP_TRANSPORT == "streamable-http":
        import uvicorn
        uvicorn.run(create_server_app(), host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()
