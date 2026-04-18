# routers/grants_router.py
#
# Grant management endpoints — invite claim, CRUD, accept.

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from arango.database import StandardDatabase
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from core.dependencies import get_arango_db
from services.dependencies import get_auth, AuthContext
from db.arango import (
    create_grant,
    get_grant_by_id,
    update_grant,
)
from entities.grant import Grant as GrantEntity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/grants", tags=["Grants"])


# =============================================================================
# Request / Response Models
# =============================================================================

class ClaimInviteRequest(BaseModel):
    token: str


class CreateGrantRequest(BaseModel):
    resource_id: str
    # CRUDEASIO
    can_create: bool = False
    can_read: bool = True
    can_update: bool = False
    can_delete: bool = False
    can_invoke: bool = False
    can_add: bool = False
    can_share: bool = False
    can_admin: bool = False
    # Invite targeting (optional — makes this an invite grant)
    grantee_type: str = "user"              # "user" | "invite"
    grantee_id: Optional[str] = None        # user_id for direct grant; omit for invite
    target_entity: Optional[str] = None     # email, domain, etc. (invite only)
    target_entity_type: Optional[str] = None
    max_claims: Optional[int] = None
    requires_identity: bool = False
    name: Optional[str] = None
    notes: Optional[str] = None
    expires_at: Optional[str] = None
    state: str = "active"
    # Named role preset shortcut. When set, overrides individual CRUDEASIO
    # flags (which become defaults). Grant-service is the source of truth
    # for what each role maps to.
    role: Optional[str] = None
    # Personal message included in the invite email (invite grants only).
    message: Optional[str] = None




# =============================================================================
# Helpers
# =============================================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _grant_response(grant: GrantEntity) -> dict:
    """Serialize a grant entity for API responses."""
    return grant.to_dict()


def _require_admin(
    auth: AuthContext,
    resource_id: str,
    arango_db: StandardDatabase,
) -> None:
    """Raise 403 unless the caller can manage grants on the resource.

    Used for: listing all grants, revoking arbitrary grants, creating
    direct user→user grants. Delegates to ``grant_service.can_admin``.
    """
    from services import grant_service
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")
    if not grant_service.can_admin(arango_db, auth.user_id, resource_id):
        raise HTTPException(
            status_code=403,
            detail="Only the resource creator or an admin can manage grants",
        )


def _require_share_or_admin(
    auth: AuthContext,
    resource_id: str,
    arango_db: StandardDatabase,
) -> None:
    """Raise 403 unless the caller can create invites on the resource.

    Invite creation is a lower bar than full grant management ---
    collaborators with ``can_share`` (S in CRUDEASIO) can invite new
    people without needing ``can_admin``. Delegates to
    ``grant_service.can_share``.
    """
    from services import grant_service
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")
    if not grant_service.can_share(arango_db, auth.user_id, resource_id):
        raise HTTPException(
            status_code=403,
            detail="You need share or admin permission on this resource",
        )


# =============================================================================
# Endpoints
# =============================================================================

# ---------- GET /grants/invite-context — Pre-auth invite context ----------

@router.get("/invite-context")
async def get_invite_context_endpoint(
    token: str = Query(..., description="Raw invite claim token"),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Return non-PII metadata about an invite.

    Safe to call pre-auth. The claim page uses this to decide whether to
    prompt for sign-in. Returns 404 if the token doesn't resolve to an
    active invite; otherwise returns ``{valid, has_target, target_type}``
    with no inviter identity or resource name.
    """
    from services import grant_service
    ctx = grant_service.get_invite_context(arango_db, token)
    if not ctx:
        raise HTTPException(status_code=404, detail="Invite not found or expired")
    return ctx


# ---------- GET /grants/invite-details — Post-auth invite details ----------

@router.get("/invite-details")
async def get_invite_details_endpoint(
    token: str = Query(..., description="Raw invite claim token"),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Return full invite details after verifying caller identity.

    Requires authentication. Returns inviter + resource info only when the
    caller matches the invite's target identity (if set). A target mismatch
    returns ``{valid, identity_mismatch: True}`` without leaking PII.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")
    from services import grant_service
    details = grant_service.get_invite_details(arango_db, token, auth.user_id)
    if not details:
        raise HTTPException(status_code=404, detail="Invite not found or expired")
    return details


# ---------- GET /grants/mine-sent — Invites I've sent ----------

@router.get("/mine-sent")
async def list_invites_sent_endpoint(
    include_revoked: bool = Query(
        False, description="Include revoked / exhausted invites in the result.",
    ),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """List invite grants the caller has created.

    Used by the UI to show a "pending invites" list with revoke affordances.
    Only returns invites where the caller is ``granted_by``; the actual
    claim token is never exposed (only the hash lives in the grant).
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    from services import grant_service
    grants = grant_service.list_invites_sent(
        arango_db, auth.user_id, include_revoked=include_revoked,
    )
    return [_grant_response(g) for g in grants]


# ---------- POST /grants/claim — Claim an invite ----------

@router.post("/claim", status_code=status.HTTP_201_CREATED)
async def claim_invite_endpoint(
    body: ClaimInviteRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Claim an invite grant by presenting the raw invite token.

    Delegates to :func:`grant_service.claim_invite` and maps service
    exceptions to HTTP status codes. The service enforces the identity
    match, so forwarded links cannot be claimed by the wrong recipient.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    from services import grant_service
    from services.grant_service import (
        InviteNotFound,
        InviteExhausted,
        InviteIdentityMismatch,
    )
    try:
        created = grant_service.claim_invite(arango_db, auth.user_id, body.token)
    except InviteIdentityMismatch as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except InviteExhausted as exc:
        raise HTTPException(status_code=410, detail=str(exc))
    except InviteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return _grant_response(created)


# ---------- GET /grants — List grants for a resource ----------

@router.get("")
async def list_grants_endpoint(
    resource_id: str = Query(..., description="Resource ID to list grants for"),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """List all grants on a resource. Only the resource owner (or can_admin) can list."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    _require_admin(auth, resource_id, arango_db)

    from db.arango import get_grants_for_collection
    grants = get_grants_for_collection(arango_db, resource_id)

    return [_grant_response(g) for g in grants]


# ---------- POST /grants — Create a grant or invite ----------

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_grant_endpoint(
    body: CreateGrantRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Create a new grant or invite.

    Permission model:
    - Creating an **invite grant** (``grantee_type == "invite"``) only
      needs ``can_share`` or ``can_admin`` (or creator). This lets
      collaborators invite others without full grant management.
    - Creating a **direct user→user grant** still needs ``can_admin``
      (or creator), since it bypasses the claim flow and target-identity
      verification.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    # Invite grants go through the service: it handles token generation,
    # role-preset resolution, email delivery, and event emission in one
    # place so the MCP share tool and this endpoint stay in lockstep.
    if body.grantee_type == GrantEntity.GRANTEE_INVITE:
        _require_share_or_admin(auth, body.resource_id, arango_db)
        from services import grant_service

        # Resolve the target email. Prefer explicit target_entity when the
        # client set target_entity_type=email; otherwise require body.role
        # for a named preset.
        target_email = None
        if (body.target_entity_type or "").lower() == "email":
            target_email = body.target_entity

        # If no role was given, synthesize one from the CRUDEASIO bits on
        # the body by matching against known presets. Otherwise fall back
        # to "viewer". Keeps legacy callers working.
        role = body.role or _role_from_bits(body) or "viewer"

        try:
            created, raw_token = grant_service.create_invite(
                arango_db,
                user_id=auth.user_id,
                resource_id=body.resource_id,
                role=role,
                target_email=target_email,
                max_claims=body.max_claims if body.max_claims is not None else 1,
                name=body.name,
                notes=body.notes,
                expires_at=body.expires_at,
                message=body.message,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        response = _grant_response(created)
        response["claim_token"] = raw_token
        response["claim_url"] = grant_service.build_claim_url(raw_token)
        return response

    # Direct user->user grant: still requires can_admin on the resource.
    _require_admin(auth, body.resource_id, arango_db)

    now = _now_iso()
    grantee_id = body.grantee_id or ""

    grant = GrantEntity(
        id=str(uuid.uuid4()),
        resource_id=body.resource_id,
        grantee_type=body.grantee_type,
        grantee_id=grantee_id,
        granted_by=auth.user_id,
        can_create=body.can_create,
        can_read=body.can_read,
        can_update=body.can_update,
        can_delete=body.can_delete,
        can_invoke=body.can_invoke,
        can_add=body.can_add,
        can_share=body.can_share,
        can_admin=body.can_admin,
        requires_identity=body.requires_identity,
        target_entity=body.target_entity,
        target_entity_type=body.target_entity_type,
        max_claims=body.max_claims,
        state=body.state,
        name=body.name,
        notes=body.notes,
        granted_at=now,
        expires_at=body.expires_at,
        created_time=now,
        modified_time=now,
    )

    created = create_grant(arango_db, grant)
    return _grant_response(created)


def _role_from_bits(body: CreateGrantRequest) -> Optional[str]:
    """Best-effort reverse-map from CRUDEASIO bits on the request to a role.

    Exact-match against ``Grant.ROLE_PRESETS`` so legacy callers that send
    individual flags (rather than a ``role`` string) still hit the same
    preset and its associated email copy.
    """
    actual = {
        "can_create": body.can_create,
        "can_read": body.can_read,
        "can_update": body.can_update,
        "can_delete": body.can_delete,
        "can_invoke": body.can_invoke,
        "can_add": body.can_add,
        "can_share": body.can_share,
        "can_admin": body.can_admin,
    }
    enabled = {k for k, v in actual.items() if v}
    for role_name, preset in GrantEntity.ROLE_PRESETS.items():
        preset_enabled = {k for k, v in preset.items() if v}
        if enabled == preset_enabled:
            return role_name
    return None


# ---------- GET /grants/{grant_id} — Read a grant ----------

@router.get("/{grant_id}")
async def read_grant(
    grant_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Read a single grant by ID."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    grant = get_grant_by_id(arango_db, grant_id)
    if not grant:
        raise HTTPException(status_code=404, detail="Grant not found")

    # Visible to: the grantee, the granter, or someone with can_admin on the resource.
    is_grantee = grant.grantee_id == auth.user_id
    is_granter = grant.granted_by == auth.user_id
    if not is_grantee and not is_granter:
        # Check can_admin on the resource.
        try:
            _require_admin(auth, grant.resource_id, arango_db)
        except HTTPException:
            raise HTTPException(status_code=404, detail="Grant not found")

    return _grant_response(grant)


# ---------- PATCH /grants/{grant_id} — Removed (immutable grants) ----------
# Grants are immutable after creation. To change permissions, revoke the old
# grant and create a new one.  This aligns with the FLARE append-only ledger
# model and provides a clean audit trail.


# ---------- DELETE /grants/{grant_id} — Revoke a grant ----------

@router.delete("/{grant_id}")
async def revoke_grant(
    grant_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Revoke a grant (soft-delete by setting state to revoked)."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    grant = get_grant_by_id(arango_db, grant_id)
    if not grant:
        raise HTTPException(status_code=404, detail="Grant not found")

    # Granters may only revoke their own *pending invites* (grantee_type == "invite").
    # Revoking a claimed/accepted user grant requires can_admin (O).
    is_revocable_invite = (
        grant.granted_by == auth.user_id
        and grant.grantee_type == GrantEntity.GRANTEE_INVITE
    )
    if not is_revocable_invite:
        _require_admin(auth, grant.resource_id, arango_db)

    now = _now_iso()
    grant.state = GrantEntity.STATE_REVOKED
    grant.revoked_by = auth.user_id
    grant.revoked_at = now
    grant.modified_time = now

    updated = update_grant(arango_db, grant)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to revoke grant")

    return {"id": grant_id, "state": "revoked"}


# ---------- POST /grants/{grant_id}/accept — Accept a pending grant ----------

@router.post("/{grant_id}/accept")
async def accept_grant(
    grant_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Accept a pending_accept direct grant."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    grant = get_grant_by_id(arango_db, grant_id)
    if not grant:
        raise HTTPException(status_code=404, detail="Grant not found")

    if grant.state != GrantEntity.STATE_PENDING_ACCEPT:
        raise HTTPException(status_code=400, detail="Grant is not pending acceptance")

    # Only the grantee can accept.
    if grant.grantee_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Only the grantee can accept this grant")

    now = _now_iso()
    grant.state = GrantEntity.STATE_ACTIVE
    grant.accepted_by = auth.user_id
    grant.accepted_at = now
    grant.modified_time = now

    updated = update_grant(arango_db, grant)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to accept grant")

    return _grant_response(updated)
