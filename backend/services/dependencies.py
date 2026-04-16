# /services/dependencies.py
#
# Unified auth dependency layer.
#
# Public API:
#   AuthContext        — dataclass returned by get_auth()
#   get_auth()         — single FastAPI dependency for all endpoints
#   get_person()       — load Person entity for the authenticated user
#   resolve_auth()     — plain-function core (usable outside FastAPI DI)
#   require_platform_admin() — post-auth guard
#   get_end_user_claims() — user-only JWT guard (rejects API-key JWTs)
#   check_access()        — verify principal has permission on an artifact
#   _check_grant_permission() — grant permission helper

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import HTTPException, Depends, Security, Request
from fastapi.security import (
    OAuth2AuthorizationCodeBearer,
)
from arango.database import StandardDatabase

from core.dependencies import get_arango_db
from core import config
from services.person_service import get_user_by_id  # now expects StandardDatabase
from services.auth_service import verify_token, verify_api_key
from db.arango import (
    get_active_grants_for_principal_resource as db_get_active_grants,
    get_active_grants_for_grantee as db_get_active_grants_for_grantee,
    get_active_grants_by_key as db_get_active_grants_by_key,
)
from services.bootstrap_types import AUTHORITY_COLLECTION_SLUG
from services.platform_topology import get_id
from entities.person import Person
from entities.api_key import APIKey as APIKeyEntity
from entities.grant import Grant as GrantEntity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AuthContext
# ---------------------------------------------------------------------------

@dataclass
class AuthContext:
    """Unified auth context returned by ``get_auth()``.

    Replaces all legacy auth dependency return types (tuples, Person,
    old AuthContext) with a single consistent shape.  Field names follow
    the Unified Artifact API spec.
    """

    principal_id: str = ""                              # user_id | api_key_id | server_client_id
    principal_type: str = "user"                        # "user" | "api_key" | "server" | "mcp_client" | "grant_key"
    user_id: Optional[str] = None                       # present for user, mcp_client, api_key, delegation
    grants: List[GrantEntity] = field(default_factory=list)  # loaded server-side
    api_key_id: Optional[str] = None                    # if auth was via API key
    api_key_entity: Optional[APIKeyEntity] = None       # full entity — needed by collection service
    server_id: Optional[str] = None                     # if auth was via server token
    actor: Optional[str] = None                         # delegation: acting server
    authority: Optional[str] = None                     # issuer / authority identity
    host_id: Optional[str] = None                       # host identity (platform instance)
    bearer_grant: Optional[GrantEntity] = None           # convenience: grant resolved from Bearer grant key
    target_artifact_id: Optional[str] = None             # artifact scoping from prefixed Bearer token ({id}:agc_xxx)


# ---------------------------------------------------------------------------
# Schemes & helpers
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl="/auth/authorize",
    tokenUrl="/auth/token"
)


def is_api_key_jwt_payload(payload: Optional[dict]) -> bool:
    """Return True when JWT claims represent an API-key JWT token."""
    if not payload:
        return False
    return bool(payload.get("api_key_id"))


def _validate_aud_for_principal(payload: dict) -> None:
    """Post-decode audience validation for multi-type token paths."""
    principal_type = payload.get("principal_type", "user")
    aud = payload.get("aud")
    if principal_type == "server":
        if aud != "agience":
            raise HTTPException(status_code=401, detail="Invalid token audience for server credential")
    elif principal_type == "mcp_client":
        if not aud:
            raise HTTPException(status_code=401, detail="Missing aud in mcp_client token")
    elif principal_type == "delegation":
        # Delegation JWTs have aud=server_client_id (the server they were issued
        # TO).  When a persona server calls Core on behalf of a user, Core
        # accepts these because the JWT is Core-signed and carries sub=user_id
        # + act.sub=server_client_id.  Only require aud to be present.
        if not aud:
            raise HTTPException(status_code=401, detail="Missing aud in delegation token")
    else:
        if aud != config.AUTHORITY_ISSUER:
            raise HTTPException(status_code=401, detail="Invalid token audience")


def _check_grant_permission(grants: List[GrantEntity], action: str, resource_type: str = None, resource_id: str = None) -> bool:
    """Check if any allow-effect grant permits the requested action.

    Deny-effect grants are excluded — callers that need deny semantics
    should use check_access() instead.
    """
    perm_attr = f"can_{action}"
    for grant in grants:
        if getattr(grant, "effect", "allow") == "deny":
            continue
        if not getattr(grant, perm_attr, False):
            continue
        if resource_type and getattr(grant, "resource_type", None) != resource_type:
            continue
        if resource_id and getattr(grant, "resource_id", None) != resource_id:
            continue
        return True
    return False


def _get_end_user_token_payload(token: str) -> dict:
    """Decode user-only JWT, rejecting API-key JWTs."""
    payload = verify_token(token, expected_audience=config.AUTHORITY_ISSUER)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=401, detail="Invalid or malformed token")
    if is_api_key_jwt_payload(payload):
        raise HTTPException(status_code=403, detail="API key token not valid for this endpoint")
    return payload


async def get_end_user_claims(
    token: str = Security(oauth2_scheme)
) -> dict:
    return _get_end_user_token_payload(token)


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------

def resolve_auth(
    token: str,
    arango_db: StandardDatabase,
    request: Optional[Request] = None,
) -> AuthContext:
    """Core auth resolution — usable from both FastAPI deps and ASGI middleware.

    Token dispatch:
    1. Parse optional artifact-id prefix (``{artifact_id}:agc_xxx``).
    2. ``agc_`` prefix → API key path.
    3. JWT (``ey`` prefix) → decode + dispatch by ``principal_type``.
    4. Otherwise → grant key in Bearer slot.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    raw_token = token.strip()
    target_artifact_id: Optional[str] = None

    # --- prefix parsing: {artifact_id}:agc_xxx ---
    if ":" in raw_token and not raw_token.startswith("ey"):
        parts = raw_token.split(":", 1)
        if len(parts) == 2 and parts[1].startswith("agc_"):
            target_artifact_id = parts[0]
            raw_token = parts[1]

    # --- API key path ---
    if raw_token.startswith("agc_"):
        api_key_entity = verify_api_key(arango_db, raw_token)
        if not api_key_entity:
            raise HTTPException(status_code=401, detail="Invalid API key")

        grants: List[GrantEntity] = []
        if getattr(api_key_entity, "id", None):
            grants = db_get_active_grants_for_grantee(arango_db, api_key_entity.id, "api_key")

        return AuthContext(
            principal_id=str(getattr(api_key_entity, "id", "")),
            principal_type="api_key",
            user_id=str(api_key_entity.user_id) if api_key_entity.user_id else None,
            grants=grants,
            api_key_id=str(getattr(api_key_entity, "id", None)) if getattr(api_key_entity, "id", None) else None,
            api_key_entity=api_key_entity,
            target_artifact_id=target_artifact_id,
        )

    # --- JWT path ---
    payload = verify_token(raw_token)
    if payload and "sub" in payload:
        _validate_aud_for_principal(payload)

        if is_api_key_jwt_payload(payload):
            raise HTTPException(status_code=403, detail="API-key JWT not accepted; use direct API key")

        jwt_principal_type = payload.get("principal_type", "user")

        if jwt_principal_type == "server":
            client_id = str(payload.get("client_id")) if payload.get("client_id") else None
            return AuthContext(
                principal_id=client_id or str(payload.get("sub", "")),
                principal_type="server",
                user_id=None,
                server_id=str(payload.get("server_id")) if payload.get("server_id") else None,
                authority=str(payload.get("authority", "")) or None,
                host_id=str(payload.get("host_id", "")) or None,
            )

        if jwt_principal_type == "mcp_client":
            return AuthContext(
                principal_id=str(payload.get("aud", "")),
                principal_type="mcp_client",
                user_id=str(payload.get("sub")) if payload.get("sub") else None,
            )

        if jwt_principal_type == "delegation":
            # All four identity-chain entities are required:
            # User (sub), Server (act.sub), Authority (iss), Host (host_id)
            d_sub = payload.get("sub")
            d_act_sub = (payload.get("act") or {}).get("sub")
            d_host = payload.get("host_id")
            if not d_sub:
                raise HTTPException(status_code=401, detail="Delegation token missing sub (user)")
            if not d_act_sub:
                raise HTTPException(status_code=401, detail="Delegation token missing act.sub (server)")
            if not d_host:
                raise HTTPException(status_code=401, detail="Delegation token missing host_id")
            return AuthContext(
                principal_id=str(d_sub),
                principal_type="user",
                user_id=str(d_sub),
                actor=str(d_act_sub),
                authority=str(payload.get("iss", "")) or None,
                host_id=str(d_host),
            )

        # Default: user JWT
        user_id = str(payload.get("sub")) if payload.get("sub") else None
        return AuthContext(
            principal_id=user_id or "",
            principal_type="user",
            user_id=user_id,
        )

    # --- Grant key in Bearer slot ---
    key_grants = db_get_active_grants_by_key(arango_db, raw_token)
    if key_grants:
        grant = key_grants[0]
        return AuthContext(
            principal_id=getattr(grant, "id", "") or "",
            principal_type="grant_key",
            user_id=None,
            grants=[grant],
            bearer_grant=grant,
        )

    raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_auth(
    token: str = Security(oauth2_scheme),
    arango_db: StandardDatabase = Depends(get_arango_db),
    request: Request = None,
) -> AuthContext:
    """Single auth dependency for all endpoints."""
    auth = resolve_auth(
        token=token or "",
        arango_db=arango_db,
        request=request,
    )
    if request is not None and auth.user_id:
        request.state.user_id = auth.user_id
    return auth


async def get_person(
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
) -> Person:
    """Load the Person entity for the authenticated user.

    Use as a second dependency alongside ``get_auth`` when a router needs
    Person fields (email, name, preferences, etc.) — not just ``user_id``.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")
    person = get_user_by_id(db=arango_db, id=auth.user_id)
    if not person:
        raise HTTPException(status_code=404, detail="User not found")
    return person


# ---------------------------------------------------------------------------
# Post-auth guards
# ---------------------------------------------------------------------------

def require_platform_admin(
    auth: AuthContext, arango_db: StandardDatabase
) -> str:
    """Post-auth guard: require platform admin.

    Merged successor to ``require_admin`` + ``require_operator`` (2026-04-06).
    A platform admin is any user with a write grant on the authority
    collection. During the post-setup / pre-Phase-4 bootstrap window,
    the initial operator recorded in ``platform.operator_id`` settings
    is treated as a platform admin even before the authority collection
    has issued them a grant — this avoids a chicken-and-egg between the
    setup wizard and the grant system.

    Returns the user_id on success, raises HTTP 403 otherwise.
    """
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="Platform admin access required")

    # Bootstrap fast-path: initial operator from setup wizard.
    from services.platform_settings_service import settings as platform_settings
    stored_operator_id = platform_settings.get("platform.operator_id")
    if stored_operator_id and auth.user_id == stored_operator_id:
        return auth.user_id

    # Canonical check: write grant on the authority collection.
    try:
        grants = db_get_active_grants(
            arango_db,
            grantee_id=auth.user_id,
            resource_type="collection",
            resource_id=get_id(AUTHORITY_COLLECTION_SLUG),
        )
        if any(g.can_update and g.is_active() for g in grants):
            return auth.user_id
    except Exception:
        logger.debug("Arango grant check failed in require_platform_admin", exc_info=True)

    raise HTTPException(status_code=403, detail="Platform admin access required")


# ---------------------------------------------------------------------------
# Access check
# ---------------------------------------------------------------------------

# Map action names to CRUDIASO grant flag attributes.
# Actions whose grants cascade from a parent collection to direct child artifacts.
# Self-only actions (update, invoke, admin) require a direct grant on the artifact.
_CASCADING_ACTIONS: frozenset = frozenset({"read", "create", "add", "delete", "share"})

_ACTION_FLAG_MAP = {
    "create": "can_create",
    "read": "can_read",
    "update": "can_update",
    "delete": "can_delete",
    "invoke": "can_invoke",
    "add": "can_add",
    "share": "can_share",
    "admin": "can_admin",
}


def _synthetic_owner_grant(resource_id: str, user_id: str) -> GrantEntity:
    """Return a synthetic full-CRUDIASO grant for the artifact owner."""
    return GrantEntity(
        resource_type="artifact",
        resource_id=resource_id,
        grantee_type="user",
        grantee_id=user_id,
        granted_by=user_id,
        can_create=True,
        can_read=True,
        can_update=True,
        can_delete=True,
        can_invoke=True,
        can_add=True,
        can_share=True,
        can_admin=True,
        state="active",
    )


def check_access(
    auth: AuthContext,
    artifact_id: str,
    action: str,
    arango_db: StandardDatabase,
) -> GrantEntity:
    """Verify *auth* has permission to perform *action* on *artifact_id*.

    Unified artifact store: *artifact_id* is either a Collection doc key
    (workspace or otherwise) or an Artifact doc key.

    Resolution flow:
      1. Resolve id → collection_id and determine resource_type
      2. Owner fast-path on the collection (full CRUDIASO)
      3. Direct grants on the target — deny checked before allow
      4. If target is an artifact and action cascades: check parent collection grants
         (read, create, add, delete, search cascade; update, invoke, own do not)
    """
    flag_attr = _ACTION_FLAG_MAP.get(action)
    if not flag_attr:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # --- Resolve target: collection or artifact ---
    # Container-as-artifact: all containers are in the artifacts table.
    # A container doc has no collection_id; a regular artifact has one.
    art_doc = None
    collection_id: Optional[str] = None
    try:
        doc = arango_db.collection("artifacts").get(artifact_id)
    except Exception:
        doc = None

    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    if not doc.get("collection_id"):
        # No collection_id → this IS a container (workspace or collection)
        collection_id = artifact_id
        collection_doc = doc
        resource_type = "collection"
    else:
        # Regular artifact — look up its parent container
        art_doc = doc
        collection_id = doc.get("collection_id")
        try:
            collection_doc = arango_db.collection("artifacts").get(collection_id)
        except Exception:
            collection_doc = None
        if not collection_doc:
            raise HTTPException(status_code=404, detail="Not found")
        resource_type = "artifact"

    # --- Owner fast-path on the collection ---
    owner_id = collection_doc.get("created_by")
    if owner_id and auth.user_id and owner_id == auth.user_id:
        return _synthetic_owner_grant(artifact_id, auth.user_id)

    if not auth.user_id:
        raise HTTPException(status_code=404, detail="Not found")

    # --- Direct grants on the target (deny before allow) ---
    direct_grants = db_get_active_grants(
        arango_db,
        grantee_id=auth.user_id,
        resource_type=resource_type,
        resource_id=artifact_id,
    )
    for g in direct_grants:
        if getattr(g, "effect", "allow") == "deny" and getattr(g, flag_attr, False):
            raise HTTPException(status_code=404, detail="Not found")
    for g in direct_grants:
        if getattr(g, "effect", "allow") != "deny" and getattr(g, flag_attr, False):
            return g

    # --- Parent collection grants (cascading actions only) ---
    if art_doc and action in _CASCADING_ACTIONS:
        parent_grants = db_get_active_grants(
            arango_db,
            grantee_id=auth.user_id,
            resource_type="collection",
            resource_id=collection_id,
        )
        for g in parent_grants:
            if getattr(g, "effect", "allow") == "deny" and getattr(g, flag_attr, False):
                raise HTTPException(status_code=404, detail="Not found")
        for g in parent_grants:
            if getattr(g, "effect", "allow") != "deny" and getattr(g, flag_attr, False):
                return g

    raise HTTPException(status_code=404, detail="Not found")


def check_inbound_nonce(request: Request, auth: AuthContext) -> None:
    """Enforce nonce validation for keys with ``requires_nonce=True``.

    Must be called explicitly from any endpoint that should be bot-protected.
    No-ops for principals whose key does not have ``requires_nonce=True``, so
    the same endpoint can serve both authenticated users and nonce-gated callers.

    Raises 403 if the nonce is absent or invalid.
    """
    if auth.principal_type != "api_key":
        return
    key_entity = auth.api_key_entity
    if not key_entity or not getattr(key_entity, "requires_nonce", False):
        return

    from services.auth_service import verify_nonce as _verify_nonce
    from core import config

    nonce = request.headers.get("X-Agience-Challenge", "")
    if not nonce:
        raise HTTPException(status_code=403, detail="Nonce required for inbound access")

    artifact_id = auth.target_artifact_id or ""
    key_id = auth.api_key_id or ""

    if not _verify_nonce(
        token=nonce,
        key_id=key_id,
        artifact_id=artifact_id,
        secret=config.INBOUND_NONCE_SECRET,
    ):
        raise HTTPException(status_code=403, detail="Invalid or expired nonce")
