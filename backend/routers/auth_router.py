# routers/auth_router.py

import base64
import hashlib
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, Request, HTTPException, status, Form
from fastapi.responses import JSONResponse, RedirectResponse
from authlib.integrations.starlette_client import OAuth
from authlib.common.security import generate_token
from arango.database import StandardDatabase
from pydantic import BaseModel, ConfigDict

from entities.person import Person
from services.dependencies import get_auth, get_person, AuthContext
from services.auth_service import (
    create_jwt_token,
    is_client_redirect_allowed,
    is_person_allowed,
    verify_token,
    get_jwks,
    hash_password,
    verify_password,
    dummy_verify_password,
    find_mcp_client_by_client_id,
    get_mcp_client_allowed_scopes,
    issue_nonce,
)
from services.person_service import (
    get_or_create_user_by_oidc_identity,
    record_person_event,
)
from core.dependencies import get_arango_db

import bcrypt

from db.arango import (
    get_active_grants_for_principal_resource as db_get_active_grants,
    get_server_credential_by_client_id as db_get_server_credential,
    update_server_credential_last_used as db_update_cred_last_used,
)
from services.bootstrap_types import AUTHORITY_COLLECTION_SLUG
from services.platform_topology import get_id
from services import server_registry

from core import config

logger = logging.getLogger(__name__)
auth_router = APIRouter(prefix="/auth", tags=["Authentication"])
root_router = APIRouter(tags=["Authentication"])
oauth = OAuth()


class PasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identifier: str  # username or email
    password: str


class PasswordRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str
    name: str = ""
    email: str = ""  # optional — for recovery/OTP flows


def _extract_email(user_info: dict) -> str:
    # Common OIDC fields:
    # - Google/Auth0: email
    # - Microsoft Entra: preferred_username (often email/UPN)
    # - Some providers: upn
    email = (
        (user_info.get("email") or "").strip()
        or (user_info.get("preferred_username") or "").strip()
        or (user_info.get("upn") or "").strip()
    )
    if not email:
        emails = user_info.get("emails")
        if isinstance(emails, list) and emails:
            email = str(emails[0]).strip()
    return email


def _extract_name(user_info: dict) -> str:
    name = (user_info.get("name") or "").strip()
    if name:
        return name
    given = (user_info.get("given_name") or "").strip()
    family = (user_info.get("family_name") or "").strip()
    if given or family:
        return (given + " " + family).strip()
    return ((user_info.get("preferred_username") or "").strip() or "User")

def _compute_roles(collection_db: StandardDatabase, user_id: str) -> list[str]:
    """Compute platform role strings for inclusion in JWTs.

    Emits a single ``platform:admin`` role when either condition is true
    (merged from the former operator + admin checks on 2026-04-06):

    - The user is the initial operator recorded in ``platform.operator_id``
      settings (bootstrap fast-path for the setup-wizard-provisioned user).
    - The user holds a write grant on the authority collection (canonical
      platform-admin check).

    Mirrors ``services.dependencies.require_platform_admin``. The frontend
    ``useAdmin`` hook reads this role to gate the Settings page.
    """
    # Bootstrap fast-path: initial operator from setup wizard.
    try:
        from services.platform_settings_service import settings as platform_settings
        stored_operator_id = platform_settings.get("platform.operator_id")
        if stored_operator_id and user_id == stored_operator_id:
            return ["platform:admin"]
    except Exception:
        logger.debug("Bootstrap operator role check failed for user %s", user_id, exc_info=True)

    # Canonical check: write grant on the authority collection.
    try:
        grants = db_get_active_grants(
            collection_db,
            grantee_id=user_id,
            resource_type="collection",
            resource_id=get_id(AUTHORITY_COLLECTION_SLUG),
        )
        if any(g.can_update and g.is_active() for g in grants):
            return ["platform:admin"]
    except Exception:
        logger.debug("Platform admin grant check failed for user %s", user_id, exc_info=True)

    return []


# Provider registry for runtime introspection and safer errors.
REGISTERED_PROVIDERS: dict[str, dict] = {}


def _register_oidc_provider(
    *,
    name: str,
    label: str,
    server_metadata_url: str,
    client_id: Optional[str],
    client_secret: Optional[str],
    redirect_uri: Optional[str],
    scope: str = "openid email profile",
    issuer: Optional[str] = None,
) -> None:
    # client_secret is optional: omit for public clients (PKCE-only, no secret).
    # This is the correct model for localhost/fixed-domain distributed deployments
    # per RFC 8252 and Google's own guidance for desktop/installed apps.
    if not client_id or not redirect_uri:
        return

    register_kwargs: dict = {
        "server_metadata_url": server_metadata_url,
        "client_id": client_id,
        "client_kwargs": {"scope": scope},
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        register_kwargs["client_secret"] = client_secret
    else:
        # Public client: authenticate at token endpoint without secret (PKCE only).
        register_kwargs["token_endpoint_auth_method"] = "none"

    oauth.register(name=name, **register_kwargs)
    REGISTERED_PROVIDERS[name] = {
        "label": label,
        "type": "oidc",
        "redirect_uri": redirect_uri,
        # issuer stored for RFC 9207 iss parameter validation in auth_callback.
        # Falls back to None for providers where we do not have the issuer at
        # registration time; validation is skipped if None.
        "issuer": issuer,
    }


def reload_oauth_providers() -> None:
    """
    (Re)register all configured OIDC providers from current config values.

    Called from lifespan after Phase 2 (config.load_settings_from_db()) so that
    providers configured via the setup wizard are registered before serving
    requests.  Safe to call multiple times — re-registration is idempotent.
    """
    REGISTERED_PROVIDERS.clear()

    _register_oidc_provider(
        name="google",
        label="Google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=config.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET,
        redirect_uri=config.GOOGLE_OAUTH_REDIRECT_URI,
        issuer="https://accounts.google.com",
    )

    _register_oidc_provider(
        name="entra",
        label="Microsoft",
        server_metadata_url=(
            f"https://login.microsoftonline.com/{config.MICROSOFT_ENTRA_TENANT}"
            "/v2.0/.well-known/openid-configuration"
        ),
        client_id=config.MICROSOFT_ENTRA_CLIENT_ID,
        client_secret=config.MICROSOFT_ENTRA_CLIENT_SECRET,
        redirect_uri=config.MICROSOFT_ENTRA_REDIRECT_URI,
        issuer=(
            f"https://login.microsoftonline.com/{config.MICROSOFT_ENTRA_TENANT}/v2.0"
            if config.MICROSOFT_ENTRA_TENANT else None
        ),
    )

    if config.AUTH0_DOMAIN:
        domain = (
            config.AUTH0_DOMAIN.strip()
            .removeprefix("https://")
            .removeprefix("http://")
            .strip("/")
        )
        _register_oidc_provider(
            name="auth0",
            label="Auth0",
            server_metadata_url=f"https://{domain}/.well-known/openid-configuration",
            client_id=config.AUTH0_CLIENT_ID,
            client_secret=config.AUTH0_CLIENT_SECRET,
            redirect_uri=config.AUTH0_REDIRECT_URI,
            issuer=f"https://{domain}",
        )

    if config.CUSTOM_OIDC_NAME and config.CUSTOM_OIDC_METADATA_URL:
        _register_oidc_provider(
            name=config.CUSTOM_OIDC_NAME,
            label=config.CUSTOM_OIDC_NAME,
            server_metadata_url=config.CUSTOM_OIDC_METADATA_URL,
            client_id=config.CUSTOM_OIDC_CLIENT_ID,
            client_secret=config.CUSTOM_OIDC_CLIENT_SECRET,
            redirect_uri=config.CUSTOM_OIDC_REDIRECT_URI,
            scope=config.CUSTOM_OIDC_SCOPES,
            # Custom OIDC — no known issuer at registration time; validation skipped.
        )

# In-memory storage for authorization codes and PKCE challenges (use Redis in production)
authorization_codes: dict[str, dict] = {}
pkce_challenges: dict[str, dict] = {}

_AUTH_CACHE_TTL = timedelta(minutes=10)
_AUTH_CACHE_MAX_ITEMS = 5000


def _prune_auth_cache(now: datetime) -> None:
    cutoff = now - _AUTH_CACHE_TTL

    for cache in (pkce_challenges, authorization_codes):
        # TTL prune
        expired = [k for k, v in cache.items() if isinstance(v, dict) and v.get("timestamp") and v["timestamp"] < cutoff]
        for k in expired:
            cache.pop(k, None)

        # Size prune: dict preserves insertion order
        while len(cache) > _AUTH_CACHE_MAX_ITEMS:
            try:
                oldest = next(iter(cache))
            except StopIteration:
                break
            cache.pop(oldest, None)

@auth_router.get("/authorize", dependencies=None)
async def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    provider: str = "google",
    scope: Optional[str] = None,
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = "S256",
    setup_operator_token: Optional[str] = None,
    collection_db: StandardDatabase = Depends(get_arango_db),
):
    """
    OAuth2 Authorization endpoint - redirects to OAuth provider for authentication.
    Supports PKCE (RFC 7636). Validates client_id: the built-in platform client is
    validated statically against config; third-party clients are looked up as
    MCP Client artifacts in ArangoDB.
    """
    # Validate required parameters
    if response_type != "code":
        raise HTTPException(
            status_code=400, 
            detail="Unsupported response_type. Only 'code' is supported."
        )
    
    # Validate client_id and redirect_uri.
    # Unknown client_id (not registered as an MCP Client artifact) → built-in platform client.
    registered_uris = find_mcp_client_by_client_id(collection_db, client_id)
    if registered_uris is None:
        # Built-in platform client — validate redirect_uri against static config
        if not is_client_redirect_allowed(redirect_uri):
            raise HTTPException(status_code=403, detail="Invalid redirect_uri")
    else:
        # Third-party MCP client — validate against registered redirect_uris
        if redirect_uri not in registered_uris:
            raise HTTPException(status_code=400, detail="redirect_uri not registered for this client")
        # redirect_uri is trusted once validated against the artifact's registered list
    
    # Validate PKCE parameters
    if code_challenge:
        if code_challenge_method not in ["S256", "plain"]:
            raise HTTPException(
                status_code=400,
                detail="Unsupported code_challenge_method. Only 'S256' and 'plain' are supported."
            )
        if len(code_challenge) < 43 or len(code_challenge) > 128:
            raise HTTPException(
                status_code=400,
                detail="code_challenge must be between 43 and 128 characters"
            )
    
    if provider not in REGISTERED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown or unconfigured provider: {provider}")

    _prune_auth_cache(datetime.now(timezone.utc))

    # Generate a state parameter for the upstream OAuth flow
    oauth_state = generate_token(32)
    
    # Store the original request parameters for later use
    authorization_request = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "provider": provider,
        "scope": scope or "read",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        # RFC 9207: store the expected issuer so auth_callback can validate the
        # iss parameter returned by the upstream provider.  None means skip.
        "expected_iss": REGISTERED_PROVIDERS[provider].get("issuer"),
        "timestamp": datetime.now(timezone.utc)
    }
    # Carry through setup operator token (Google-only first-login promotion)
    if setup_operator_token:
        authorization_request["setup_operator_token"] = setup_operator_token
    
    # Store in temporary storage (use Redis in production)
    pkce_challenges[oauth_state] = authorization_request
    
    oauth_client = oauth.create_client(provider)
    if not oauth_client:
        raise HTTPException(status_code=500, detail=f"Provider not available: {provider}")

    # SEP-2207: request offline_access only if the upstream AS advertises it.
    # Authlib fetches and caches AS metadata on the first request; subsequent
    # calls use the cached value.  Never request offline_access from providers
    # that do not advertise it to avoid unexpected errors.
    extra_kwargs: dict = {}
    try:
        server_metadata = await oauth_client.load_server_metadata()
        supported_scopes = server_metadata.get("scopes_supported") or []
        if "offline_access" in supported_scopes:
            extra_kwargs["scope"] = REGISTERED_PROVIDERS[provider].get(
                "scope", "openid email profile"
            ) + " offline_access"
    except Exception:
        pass  # Metadata unavailable — proceed without offline_access

    # Redirect to upstream OAuth with our state
    return await oauth_client.authorize_redirect(
        request,
        redirect_uri=REGISTERED_PROVIDERS[provider]["redirect_uri"],
        state=oauth_state,
        **extra_kwargs,
    )

@auth_router.get("/callback")
async def auth_callback(request: Request):
    """
    OAuth2 callback endpoint - handles upstream OAuth response and issues authorization code
    """
    try:
        # Get the upstream state from the callback
        oauth_state = request.query_params.get("state")
        if not oauth_state or oauth_state not in pkce_challenges:
            raise HTTPException(status_code=400, detail="Invalid or expired state parameter")

        auth_request = pkce_challenges[oauth_state]
        provider = auth_request.get("provider") or "google"

        # RFC 9207 §2: if the upstream provider returned an iss parameter, compare
        # it against the expected issuer stored when the authorization was initiated.
        # Reject mismatches to prevent mix-up attacks.
        callback_iss = request.query_params.get("iss")
        expected_iss = auth_request.get("expected_iss")
        if expected_iss and callback_iss and callback_iss != expected_iss:
            logger.warning(
                "RFC 9207 iss mismatch for provider %r: expected %r, got %r",
                provider, expected_iss, callback_iss,
            )
            raise HTTPException(status_code=400, detail="Authorization server mismatch (iss parameter)")

        oauth_client = oauth.create_client(provider)
        if not oauth_client:
            raise HTTPException(status_code=500, detail=f"Provider not available: {provider}")

        token = await oauth_client.authorize_access_token(request)
        user_info = token.get("userinfo")
        if not user_info:
            try:
                user_info = await oauth_client.userinfo(token=token)
            except Exception:
                user_info = None

        if not user_info:
            raise HTTPException(status_code=400, detail="Failed to fetch user info")

        email = _extract_email(user_info)
        name = _extract_name(user_info)
        if not email:
            raise HTTPException(status_code=400, detail="Provider did not return an email address")

        # deny early if not allowlisted
        google_id = user_info.get("sub") if provider == "google" else None
        if not is_person_allowed(google_id, email):
            error_params = {"error": "access_denied", "error_description": "User not allowed"}
            if auth_request.get("state"):
                error_params["state"] = auth_request["state"]
            redirect_url = f'{auth_request["redirect_uri"]}?{urlencode(error_params)}'
            if redirect_url:
                return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
            raise HTTPException(status_code=403, detail="User not allowed")
        
        _prune_auth_cache(datetime.now(timezone.utc))
        
        # Generate authorization code
        auth_code = generate_token(32)
        
        # Store authorization code with user info and PKCE challenge
        authorization_codes[auth_code] = {
            "user_info": {
                "provider": provider,
                "sub": user_info["sub"],
                "email": email.lower(),
                "name": name,
                "picture": user_info.get("picture"),
            },
            "client_id": auth_request["client_id"],
            "redirect_uri": auth_request["redirect_uri"],
            "scope": auth_request["scope"],
            "code_challenge": auth_request.get("code_challenge"),
            "code_challenge_method": auth_request.get("code_challenge_method"),
            "timestamp": datetime.now(timezone.utc)
        }
        # Pass through setup operator token if present (Google-only first-login promotion)
        if auth_request.get("setup_operator_token"):
            authorization_codes[auth_code]["setup_operator_token"] = auth_request["setup_operator_token"]

        _prune_auth_cache(datetime.now(timezone.utc))
        
        # Build redirect URL with authorization code
        params = {"code": auth_code}
        if auth_request["state"]:
            params["state"] = auth_request["state"]
        
        redirect_url = f"{auth_request['redirect_uri']}?{urlencode(params)}"
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        # Try to redirect with error if possible
        oauth_state = request.query_params.get("state")
        if oauth_state and oauth_state in pkce_challenges:
            auth_request = pkce_challenges[oauth_state]
            error_params = {"error": "server_error", "error_description": "Authentication failed"}
            if auth_request["state"]:
                error_params["state"] = auth_request["state"]
            error_url = f"{auth_request['redirect_uri']}?{urlencode(error_params)}"
            return RedirectResponse(url=error_url, status_code=status.HTTP_302_FOUND)
        
        return JSONResponse(
            status_code=400, 
            content={"error": "server_error", "error_description": "Authentication failed"}
        )

@auth_router.post("/token")
async def token_endpoint(
    background_tasks: BackgroundTasks,
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
    collection_db: StandardDatabase = Depends(get_arango_db),
):
    """
    OAuth2 Token endpoint.
    Supports: authorization_code, refresh_token, client_credentials.
    """
    if grant_type == "authorization_code":
        # Validate required form fields for this flow
        if not all([code, redirect_uri, client_id, code_verifier]):
            raise HTTPException(
                status_code=400,
                detail="Missing one or more required parameters: code, redirect_uri, client_id, code_verifier"
            )
        # Type narrowing - these are guaranteed to be str after the check above
        assert code is not None
        assert redirect_uri is not None
        assert client_id is not None
        assert code_verifier is not None
        return await handle_authorization_code_grant(
            collection_db=collection_db,
            background_tasks=background_tasks,
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_verifier=code_verifier            
        )

    elif grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(status_code=400, detail="Missing required parameter: refresh_token")
        return await handle_refresh_token_grant(
            background_tasks=background_tasks,
            refresh_token=refresh_token
        )

    elif grant_type == "client_credentials":
        if not client_id or not client_secret:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "client_id and client_secret are required"},
            )
        return await handle_client_credentials_grant(
            collection_db=collection_db,
            client_id=client_id,
            client_secret=client_secret,
        )

    raise HTTPException(
        status_code=400,
        detail="Unsupported grant_type. Only 'authorization_code', 'refresh_token', and 'client_credentials' are supported."
    )

async def handle_authorization_code_grant(
    collection_db: StandardDatabase,
    background_tasks: BackgroundTasks,
    code: str, 
    redirect_uri: str, 
    client_id: str, 
    code_verifier: Optional[str]
):
    """Handle authorization_code grant type with PKCE verification"""
    if not code:
        raise HTTPException(status_code=400, detail="Missing required parameter: code")
    
    # Retrieve authorization code data
    if code not in authorization_codes:
        raise HTTPException(status_code=400, detail="Invalid or expired authorization code")
    
    auth_data = authorization_codes[code]

    _prune_auth_cache(datetime.now(timezone.utc))
    
    # Validate client_id and redirect_uri
    if client_id != auth_data["client_id"]:
        raise HTTPException(status_code=400, detail="Invalid client_id")
    
    if redirect_uri != auth_data["redirect_uri"]:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    
    # Check if authorization code is expired (10 minutes)
    if datetime.now(timezone.utc) - auth_data["timestamp"] > _AUTH_CACHE_TTL:
        del authorization_codes[code]
        raise HTTPException(status_code=400, detail="Authorization code expired")
    
    # PKCE verification
    if auth_data.get("code_challenge"):
        if not code_verifier:
            raise HTTPException(status_code=400, detail="Missing code_verifier for PKCE")
        
        # Verify code challenge
        if auth_data["code_challenge_method"] == "S256":
            # SHA256 hash of code_verifier, base64url encoded
            verifier_hash = hashlib.sha256(code_verifier.encode()).digest()
            verifier_challenge = base64.urlsafe_b64encode(verifier_hash).decode().rstrip('=')
        else:  # plain
            verifier_challenge = code_verifier
        
        if not secrets.compare_digest(verifier_challenge, auth_data["code_challenge"]):
            raise HTTPException(status_code=400, detail="Invalid code_verifier")
    
    # Clean up used authorization code
    del authorization_codes[code]
    
    info = auth_data["user_info"]
    try:
        provider = info.get("provider") or "google"
        user = get_or_create_user_by_oidc_identity(
            db=collection_db,
            oidc_provider=provider,
            oidc_subject=info["sub"],
            email=info.get("email", ""),
            name=info.get("name", ""),
            picture=info.get("picture"),
        )    
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not user:
        raise HTTPException(status_code=500, detail="Failed to create or retrieve user")
    logger.info(f"User authenticated: {user.email} ({user.id})")

    # Setup operator promotion: if the Google-only wizard flow passed a setup_operator_token,
    # validate it against the one-time token stored in platform settings.  Only runs when
    # platform.operator_id is not yet set (setup completed without a local account).
    try:
        _setup_op_token = auth_data.get("setup_operator_token")
        if _setup_op_token:
            from services.platform_settings_service import settings as _platform_settings
            _stored_token = _platform_settings.get("platform.setup_operator_token")
            if (
                _stored_token
                and not _platform_settings.get("platform.operator_id")
                and _platform_settings.get("platform.setup_complete") == "true"
                and secrets.compare_digest(_stored_token, _setup_op_token)
            ):
                _platform_settings.set_many(
                    collection_db,
                    [
                        {"key": "platform.operator_id", "value": str(user.id), "category": "platform", "is_secret": False},
                        # Consume the token — one-time use only
                        {"key": "platform.setup_operator_token", "value": "", "category": "platform", "is_secret": True},
                    ],
                    updated_by=str(user.id),
                )
                logger.info("Setup token validated: promoted %s (%s) to platform operator", user.email, user.id)
            elif _setup_op_token and not _stored_token:
                logger.warning("Setup operator token presented but none is stored — already consumed or invalid")
    except Exception:
        logger.warning("Operator promotion check failed for user %s", user.id, exc_info=True)

    issuing_client_id = auth_data["client_id"]

    # Determine token type by DB lookup: registered MCP Client artifacts get a scoped
    # token; anything else (the built-in platform frontend) gets a full user JWT.
    # redirect_uri was already validated in /authorize, so the client is trusted here.
    is_mcp_client = find_mcp_client_by_client_id(collection_db, issuing_client_id) is not None

    if not is_mcp_client:
        # ── Platform client: full user JWT ──────────────────────────────────────────
        user_data = {
            "sub": str(user.id),
            "email": user.email,
            "name": user.name,
            "picture": user.picture,
            "roles": _compute_roles(collection_db, str(user.id)),
            # client_id traces which OAuth client initiated this flow.
            "client_id": issuing_client_id,
            # aud: the resource server this token is intended for (RFC 7519 §4.1.3).
            "aud": config.AUTHORITY_ISSUER,
        }
    else:
        # ── Third-party MCP OAuth client: scoped token (R4) ──────────────────────────
        # No PII (roles, email, name, picture).  Scopes are the intersection of
        # what the client requested and what the artifact allows.
        requested_scopes = set((auth_data.get("scope") or "read").split())
        allowed_scopes = set(get_mcp_client_allowed_scopes(collection_db, issuing_client_id))
        granted_scopes = sorted(requested_scopes & allowed_scopes) or ["read"]
        user_data = {
            "sub": str(user.id),
            "aud": issuing_client_id,
            "principal_type": "mcp_client",
            "scopes": granted_scopes,
        }

    access_token = create_jwt_token(user_data)

    # Create refresh token (longer expiry) - don't pre-add exp field
    refresh_payload = {
        **user_data,
        "token_type": "refresh"
    }
    refresh_token = create_jwt_token(refresh_payload, expires_hours=24*30)  # 30 days

    background_tasks.add_task(record_person_event, payload=user_data, event_type="auth_grant")
    
    return JSONResponse(content={
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600 * 12,  # 12 hours
        "refresh_token": refresh_token,
        "scope": auth_data["scope"]
    })


@auth_router.get("/providers", dependencies=None)
async def list_providers():
    """List authentication mechanisms available to the frontend."""
    providers = []
    for name, meta in REGISTERED_PROVIDERS.items():
        providers.append({
            "name": name,
            "label": meta.get("label", name),
            "type": meta.get("type", "oidc"),
        })
    providers.sort(key=lambda p: p.get("label", p["name"]))
    from services.email_service import is_configured as _email_configured
    return {
        "providers": providers,
        "password": config.PASSWORD_AUTH_ENABLED,
        "otp": _email_configured(),
    }


@auth_router.post("/password/login")
async def password_login(
    background_tasks: BackgroundTasks,
    payload: PasswordLoginRequest,
    collection_db: StandardDatabase = Depends(get_arango_db),
):
    if not config.PASSWORD_AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Password auth is disabled")

    from services.person_service import get_user_by_email, get_user_by_username

    identifier = (payload.identifier or "").strip()
    password = payload.password or ""
    if not identifier or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    # Lookup by email if identifier looks like one, otherwise by username
    if "@" in identifier:
        user = get_user_by_email(collection_db, identifier.lower())
    else:
        user = get_user_by_username(collection_db, identifier)

    if not user or not getattr(user, "password_hash", None):
        dummy_verify_password(password)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    user_data = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "roles": _compute_roles(collection_db, str(user.id)),
        "client_id": config.PLATFORM_CLIENT_ID,
        "aud": config.AUTHORITY_ISSUER,
    }
    access_token = create_jwt_token(user_data)
    refresh_payload = {**user_data, "token_type": "refresh"}
    refresh_token = create_jwt_token(refresh_payload, expires_hours=24 * 30)

    background_tasks.add_task(record_person_event, payload=user_data, event_type="password_login")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600 * 12,
        "refresh_token": refresh_token,
        "scope": "read write",
    }


@auth_router.post("/password/register")
async def password_register(
    background_tasks: BackgroundTasks,
    payload: PasswordRegisterRequest,
    collection_db: StandardDatabase = Depends(get_arango_db),
):
    if not config.PASSWORD_AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Password auth is disabled")

    username = (payload.username or "").strip()
    email = (payload.email or "").strip().lower()
    password = payload.password or ""
    name = (payload.name or "").strip()

    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if email and "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    if len(password) < config.PASSWORD_MIN_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {config.PASSWORD_MIN_LENGTH} characters")

    from services.person_service import create_user_with_password

    try:
        pwd_hash = hash_password(password)
        user = create_user_with_password(
            db=collection_db,
            username=username,
            name=name,
            password_hash=pwd_hash,
            email=email,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        logger.info("Password registration failed for username=%r (%s)", username, e)
        raise HTTPException(status_code=400, detail="Registration failed")

    user_data = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "roles": _compute_roles(collection_db, str(user.id)),
        "client_id": config.PLATFORM_CLIENT_ID,
        "aud": config.AUTHORITY_ISSUER,
    }
    access_token = create_jwt_token(user_data)
    refresh_payload = {**user_data, "token_type": "refresh"}
    refresh_token = create_jwt_token(refresh_payload, expires_hours=24 * 30)

    background_tasks.add_task(record_person_event, payload=user_data, event_type="password_register")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600 * 12,
        "refresh_token": refresh_token,
        "scope": "read write",
    }

async def handle_refresh_token_grant(background_tasks: BackgroundTasks, refresh_token: str):
    """Handle refresh_token grant type"""
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Missing required parameter: refresh_token")

    # Decode without audience enforcement — refresh tokens can carry different aud values
    # (AUTHORITY_ISSUER for platform users, a client_id for MCP OAuth clients).
    # We validate aud presence post-decode and carry it forward into the new access token.
    payload = verify_token(refresh_token)
    if not payload or payload.get("token_type") != "refresh":
        raise HTTPException(status_code=400, detail="Invalid refresh token")

    aud = payload.get("aud")
    if not aud:
        # Reject tokens issued before audience enforcement was added.
        raise HTTPException(status_code=400, detail="Refresh token has no audience — please log in again")

    # Carry claims forward; strip token_type and exp so create_jwt_token sets a fresh expiry.
    user_data = {k: v for k, v in payload.items() if k not in ["exp", "token_type", "iat"]}
    access_token = create_jwt_token(user_data)

    background_tasks.add_task(record_person_event, payload=user_data, event_type="refresh_grant")

    return JSONResponse(content={
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600 * 12,  # 12 hours
        "scope": " ".join(user_data.get("scopes", [])) or "read write"
    })


async def handle_client_credentials_grant(
    collection_db: StandardDatabase,
    client_id: str,
    client_secret: str,
):
    """Handle client_credentials grant type (RFC 6749 §4.4).

    Servers exchange client_id + client_secret for a short-lived JWT
    carrying the full identity chain. No refresh token is issued.

    Kernel servers (server_registry.all_client_ids()) authenticate with the shared
    config.PLATFORM_INTERNAL_SECRET — no provisioned ServerCredential required.
    Third-party servers use the provisioned ServerCredential flow.
    """
    # --- Kernel servers: shared internal secret, no DB lookup ---
    if client_id in server_registry.all_client_ids():
        if not config.PLATFORM_INTERNAL_SECRET:
            return JSONResponse(
                status_code=503,
                content={"error": "server_error", "error_description": "Platform internal secret is not configured"},
            )
        if not secrets.compare_digest(client_secret, config.PLATFORM_INTERNAL_SECRET):
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_client", "error_description": "Invalid client_secret"},
            )
        from services.platform_topology import get_id_optional
        from services.bootstrap_types import HOST_ARTIFACT_SLUG

        server_claims = {
            "sub": f"server/{client_id}",
            "aud": "agience",
            "principal_type": "server",
            "authority": config.AUTHORITY_ISSUER,
            "host_id": get_id_optional(HOST_ARTIFACT_SLUG) or "",
            "server_id": client_id,
            "client_id": client_id,
            "scopes": ["tool:*:invoke", "resource:*:read", "resource:*:list", "resource:*:search"],
            "resource_filters": {"workspaces": "*", "collections": "*"},
        }
        access_token = create_jwt_token(server_claims, expires_hours=1)
        return JSONResponse(content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 3600,
        })

    # --- Third-party servers: provisioned ServerCredential ---
    credential = db_get_server_credential(collection_db, client_id)
    if not credential or not credential.is_active:
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_client", "error_description": "Unknown or inactive client_id"},
        )

    if not bcrypt.checkpw(client_secret.encode(), credential.secret_hash.encode()):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_client", "error_description": "Invalid client_secret"},
        )

    # Update last-used timestamp
    now = datetime.now(timezone.utc).isoformat()
    db_update_cred_last_used(collection_db, credential.id, now)

    # Build server JWT claims per the identity-chain spec
    server_claims = {
        "sub": f"server/{credential.client_id}",
        "aud": "agience",
        "principal_type": "server",
        "authority": credential.authority,
        "host_id": credential.host_id,
        "server_id": credential.server_id,
        "client_id": credential.client_id,
        "scopes": credential.scopes,
        "resource_filters": credential.resource_filters,
    }

    expires_in = 3600  # 1 hour
    access_token = create_jwt_token(server_claims, expires_hours=1)

    return JSONResponse(content={
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": expires_in,
    })


@auth_router.get("/userinfo")
async def user_info_endpoint(
    auth: AuthContext = Depends(get_auth),
    person: Person = Depends(get_person),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """
    OAuth2 /userinfo endpoint - returns user object from the database.
    """
    data = person.to_dict()
    data["roles"] = _compute_roles(arango_db, str(person.id))
    data["platform_user_id"] = config.AGIENCE_PLATFORM_USER_ID
    return data


@auth_router.get("/me/preferences")
async def get_preferences(
    auth: AuthContext = Depends(get_auth),
    person: Person = Depends(get_person),
):
    """
    Get user preferences
    """
    return person.preferences or {}


@auth_router.get("/nonce", dependencies=None)
async def issue_challenge_nonce(
    request: Request,
    auth: AuthContext = Depends(get_auth),
):
    """Issue a stateless HMAC-signed nonce for a key with ``requires_nonce=True``.

    The nonce is bound to the (artifact_id, key_id) pair extracted from the
    presented key so it cannot be replayed against a different artifact or key.

    Returns {"nonce": "...", "expires_at": "ISO-8601"}
    """
    if auth.principal_type != "api_key" or not auth.api_key_entity:
        raise HTTPException(status_code=403, detail="Inbound API key required")
    if not getattr(auth.api_key_entity, "requires_nonce", False):
        raise HTTPException(status_code=403, detail="Key is not configured for inbound access")

    artifact_id = auth.target_artifact_id or ""
    key_id = auth.api_key_id or ""
    if not artifact_id or not key_id:
        raise HTTPException(status_code=400, detail="Key is not artifact-scoped")

    secret = config.INBOUND_NONCE_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="Nonce service not configured")

    token, expires_at = issue_nonce(key_id=key_id, artifact_id=artifact_id, secret=secret)
    return {"nonce": token, "expires_at": expires_at.isoformat()}


@auth_router.patch("/me/preferences")
async def update_preferences(
    preferences: dict,
    auth: AuthContext = Depends(get_auth),
    person: Person = Depends(get_person),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """
    Update user preferences (merges with existing)
    """
    from services.person_service import update_person_preferences

    updated_person = update_person_preferences(arango_db, person.id, preferences)
    return updated_person.preferences or {}


class LinkProviderRequest(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str


@auth_router.post("/me/link-provider")
async def link_provider(
    body: LinkProviderRequest,
    auth: AuthContext = Depends(get_auth),
    person: Person = Depends(get_person),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """
    Link an OIDC provider identity to the currently authenticated user.

    Reuses the authorization_codes dict populated by auth_callback — no extra
    round-trip to the upstream provider is needed.  The authorization code
    carries the provider identity (sub, email, etc.) that was verified during
    the PKCE dance.  PKCE code_verifier is re-checked here to prevent replays.
    """
    from services.person_service import link_oidc_identity as _link_oidc_identity

    if body.code not in authorization_codes:
        raise HTTPException(status_code=400, detail="Invalid or expired authorization code")

    auth_data = authorization_codes[body.code]

    # Validate redirect_uri (mirrors handle_authorization_code_grant)
    if body.redirect_uri != auth_data["redirect_uri"]:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    # Expiry check
    if datetime.now(timezone.utc) - auth_data["timestamp"] > _AUTH_CACHE_TTL:
        authorization_codes.pop(body.code, None)
        raise HTTPException(status_code=400, detail="Authorization code expired")

    # PKCE verification
    if auth_data.get("code_challenge"):
        if not body.code_verifier:
            raise HTTPException(status_code=400, detail="Missing code_verifier")
        if auth_data["code_challenge_method"] == "S256":
            verifier_hash = hashlib.sha256(body.code_verifier.encode()).digest()
            computed = base64.urlsafe_b64encode(verifier_hash).decode().rstrip("=")
        else:
            computed = body.code_verifier
        if not secrets.compare_digest(computed, auth_data["code_challenge"]):
            raise HTTPException(status_code=400, detail="Invalid code_verifier")

    # Consume the authorization code (one-time use)
    authorization_codes.pop(body.code, None)

    info = auth_data["user_info"]
    oidc_provider = info.get("provider") or "google"
    oidc_subject = info["sub"]

    try:
        updated = _link_oidc_identity(arango_db, str(person.id), oidc_provider, oidc_subject)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    logger.info("Linked %s identity to user %s (%s)", oidc_provider, person.email, person.id)
    return updated.to_dict()


@auth_router.delete("/me/link-provider/{provider}")
async def unlink_provider(
    provider: str,
    auth: AuthContext = Depends(get_auth),
    person: Person = Depends(get_person),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """
    Unlink an OIDC provider identity from the current user.
    Only allowed when the account has a password (fallback login exists).
    """
    from services.person_service import unlink_oidc_identity as _unlink_oidc_identity

    try:
        updated = _unlink_oidc_identity(arango_db, str(person.id))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    logger.info("Unlinked %s identity from user %s (%s)", provider, person.email, person.id)
    return updated.to_dict()


class CompleteAuthorizerOAuthRequest(BaseModel):
    """Payload for completing an upstream-OAuth flow against an Authorizer artifact."""
    workspace_id: str
    authorizer_artifact_id: str
    authorization_code: str
    code_verifier: str
    redirect_uri: str


@auth_router.post("/authorizer/complete-oauth")
async def complete_authorizer_oauth_endpoint(
    body: CompleteAuthorizerOAuthRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Complete an upstream-OAuth flow for an Authorizer artifact.

    Called from the frontend OAuth callback page after the user authorizes
    with an upstream provider (e.g. Google). Exchanges the authorization
    code for tokens via Seraph, which stores the refresh token.

    This used to run through POST /agents/invoke with
    ``operator="complete_authorizer_oauth"``. The /agents/invoke endpoint
    is being retired; this dedicated endpoint replaces that hop.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    # Fetch the authorizer artifact to read its config.
    from services import workspace_service, mcp_service, server_registry
    artifact = workspace_service.get_workspace_artifact(
        arango_db, auth.user_id, body.workspace_id, body.authorizer_artifact_id,
    )
    authorizer_config = getattr(artifact, "content", None) or "{}"
    if not authorizer_config or authorizer_config == "{}":
        try:
            import json as _json
            ctx_raw = getattr(artifact, "context", None) or "{}"
            ctx = _json.loads(ctx_raw) if isinstance(ctx_raw, str) else ctx_raw
            ck = ctx.get("content_key") if isinstance(ctx, dict) else None
            if ck:
                from services.content_service import get_text_direct
                authorizer_config = get_text_direct(ck) or "{}"
        except Exception:
            authorizer_config = "{}"

    # Short-lived JWT for Seraph to call GET /secrets on the user's behalf.
    from services.auth_service import create_jwt_token
    user_bearer_token = create_jwt_token({"sub": auth.user_id}, expires_hours=1)

    try:
        result = mcp_service.invoke_tool(
            db=arango_db,
            user_id=auth.user_id,
            workspace_id=None,
            server_artifact_id=server_registry.resolve_name_to_id("seraph"),
            tool_name="complete_authorizer_oauth",
            arguments={
                "authorizer_config": authorizer_config,
                "authorizer_artifact_id": body.authorizer_artifact_id,
                "authorization_code": body.authorization_code,
                "code_verifier": body.code_verifier,
                "redirect_uri": body.redirect_uri,
                "user_bearer_token": user_bearer_token,
            },
        )
    except Exception as exc:
        logger.error("OAuth completion failed (Seraph may be unavailable): %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"OAuth completion service unavailable: {exc}",
        )

    return result


@root_router.get("/.well-known/openid_configuration")
async def openid_configuration(request: Request):
    """OpenID Connect Discovery endpoint"""
    base_url = str(request.base_url).rstrip('/')
    
    return JSONResponse(content={
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/auth/authorize",
        "token_endpoint": f"{base_url}/auth/token",
        "userinfo_endpoint": f"{base_url}/auth/userinfo",
        "jwks_uri": f"{base_url}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "scopes_supported": ["read", "write", "openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["none"],  # Public clients
        "subject_types_supported": ["public"]
    })


@root_router.get("/.well-known/openid-configuration")
async def openid_configuration_standard(request: Request):
    # Alias for the standard discovery path.
    return await openid_configuration(request)

@root_router.get("/.well-known/jwks.json")
async def jwks_endpoint():
    """JSON Web Key Set endpoint"""
    
    jwks = get_jwks()
    return JSONResponse(content=jwks)
