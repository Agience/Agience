# services/grant_service.py
"""Grant service — reusable invite creation and claim logic.

Shared surface for:
- ``POST /grants`` HTTP endpoint (direct grants + invites)
- ``POST /grants/claim`` HTTP endpoint (claim a raw invite token)
- ``share`` / ``accept_invite`` MCP tools
- Invite claim frontend page

Identity semantics on claim:

- When an invite declares a ``target_entity`` (email / domain / user_id),
  only the authenticated user that matches that target may claim it.
  Forwarding a link doesn't grant access --- the forwardee must
  authenticate as the intended recipient.
- When no ``target_entity`` is set (open invite), ``max_claims``
  controls who can claim.

PII on the claim page:

- ``get_invite_context`` returns non-PII metadata (is the invite valid,
  does it have a target) and is safe to call pre-auth.
- ``get_invite_details`` returns inviter/resource info only after the
  caller's identity has been verified against the target.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from arango.database import StandardDatabase

from db.arango import (
    create_grant,
    update_grant,
    get_active_grants_for_grantee,
    get_active_grants_for_principal_resource,
)
from entities.grant import Grant as GrantEntity
from services.auth_service import hash_api_key

logger = logging.getLogger(__name__)


_INVITE_TOKEN_PREFIX = "agc_"
_INVITE_TOKEN_BYTES = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_claim_token() -> str:
    """Return a raw, unhashed invite claim token."""
    return f"{_INVITE_TOKEN_PREFIX}{secrets.token_urlsafe(_INVITE_TOKEN_BYTES)}"


# ---------------------------------------------------------------------------
#  Permission helpers
# ---------------------------------------------------------------------------

def is_creator(
    db: StandardDatabase,
    user_id: str,
    resource_id: str,
) -> bool:
    """True when *user_id* is the ``created_by`` of the resource artifact.

    Collections (workspaces included) are stored in the ``artifacts``
    table under the unified artifact store. Creators implicitly hold all
    CRUDEASIO permissions on what they create without needing a grant row.
    """
    if not user_id:
        return False
    try:
        doc = db.collection("artifacts").get(resource_id)
    except Exception:
        return False
    if not doc:
        return False
    creator = doc.get("created_by")
    return bool(creator) and creator == user_id


def user_has_any_flag(
    db: StandardDatabase,
    user_id: str,
    resource_id: str,
    *flags: str,
) -> bool:
    """True when *user_id* holds any of the named ``can_*`` flags on the resource.

    Does not honor the creator fast-path --- combine with
    :func:`is_creator` when you want that too.
    """
    if not user_id:
        return False
    grants = get_active_grants_for_principal_resource(
        db,
        grantee_id=user_id,
        resource_id=resource_id,
    )
    for g in grants:
        if not g.is_active():
            continue
        for flag in flags:
            if getattr(g, flag, False):
                return True
    return False


def can_share(
    db: StandardDatabase,
    user_id: str,
    resource_id: str,
) -> bool:
    """Can *user_id* create invites on this resource?

    Creator OR ``can_share`` OR ``can_admin``. Shared across the HTTP
    router, the ``share`` MCP tool, and anywhere else that needs to
    decide 'may this caller invite someone'.
    """
    if is_creator(db, user_id, resource_id):
        return True
    return user_has_any_flag(
        db, user_id, resource_id, "can_share", "can_admin",
    )


def can_admin(
    db: StandardDatabase,
    user_id: str,
    resource_id: str,
) -> bool:
    """Can *user_id* manage grants on this resource?

    Creator OR ``can_admin``. Used for direct user->user grants, revocation,
    and listing all grants on a resource.
    """
    if is_creator(db, user_id, resource_id):
        return True
    return user_has_any_flag(
        db, user_id, resource_id, "can_admin",
    )


# ---------------------------------------------------------------------------
#  Invite creation
# ---------------------------------------------------------------------------

def create_invite(
    db: StandardDatabase,
    *,
    user_id: str,
    resource_id: str,
    role: str = "viewer",
    target_email: Optional[str] = None,
    max_claims: Optional[int] = 1,
    name: Optional[str] = None,
    notes: Optional[str] = None,
    expires_at: Optional[str] = None,
    message: Optional[str] = None,
    send_email: bool = True,
) -> Tuple[GrantEntity, str]:
    """Create an invite grant using a named role preset.

    Returns ``(grant, raw_claim_token)``. The raw token is returned exactly
    once; the grant stores only a SHA-256 hash.

    When ``target_email`` is set and ``send_email`` is True, also:
    - sends an invite email via ``email_service.send_invite``
    - emits a ``grant.invite.created`` event on the resource

    Raises :class:`ValueError` if *role* is not a known preset.
    """
    preset = GrantEntity.permissions_for_role(role)

    raw_token = _generate_claim_token()
    token_hash = hash_api_key(raw_token)
    now = _now_iso()

    grant = GrantEntity(
        id=str(uuid.uuid4()),
        resource_id=resource_id,
        grantee_type=GrantEntity.GRANTEE_INVITE,
        grantee_id=token_hash,
        granted_by=user_id,
        can_create=preset.get("can_create", False),
        can_read=preset.get("can_read", False),
        can_update=preset.get("can_update", False),
        can_delete=preset.get("can_delete", False),
        can_invoke=preset.get("can_invoke", False),
        can_add=preset.get("can_add", False),
        can_share=preset.get("can_share", False),
        can_admin=preset.get("can_admin", False),
        requires_identity=bool(target_email),
        target_entity=target_email.lower() if target_email else None,
        target_entity_type="email" if target_email else None,
        max_claims=max_claims,
        state=GrantEntity.STATE_ACTIVE,
        name=name,
        notes=notes,
        granted_at=now,
        expires_at=expires_at,
        created_time=now,
        modified_time=now,
    )

    created = create_grant(db, grant)

    # Side effects: email + event emission. Fire-and-forget --- the invite
    # itself is already saved, so failures here should not fail the caller.
    email_sent = False
    if send_email and target_email:
        email_sent = _send_invite_email(db, user_id, resource_id, target_email, raw_token, message)
    _emit_invite_event(
        resource_id,
        "grant.invite.created",
        {
            "grant_id": created.id,
            "role": role,
            "target_email": target_email,
            "email_sent": email_sent,
        },
        actor_id=user_id,
    )

    return created, raw_token


def build_claim_url(raw_token: str) -> str:
    """Build the public claim URL for an invite token.

    Central so HTTP, MCP, and any future surfaces all produce the same URL.
    """
    from core.config import AUTHORITY_ISSUER
    return f"{AUTHORITY_ISSUER}/invite/{raw_token}"


def _send_invite_email(
    db: StandardDatabase,
    user_id: str,
    resource_id: str,
    target_email: str,
    raw_token: str,
    message: Optional[str],
) -> bool:
    """Resolve inviter + resource names and send the invite email.

    Returns True on successful send, False otherwise. Logs and swallows
    errors --- the caller should not fail because mail is unavailable.
    """
    from services import email_service
    from services.person_service import get_user_by_id

    try:
        person = get_user_by_id(db=db, id=user_id)
        from_name = (getattr(person, "name", None) or "").strip() or "Someone"
    except Exception:
        from_name = "Someone"

    resource_name = "a workspace"
    try:
        doc = db.collection("artifacts").get(resource_id)
        if doc:
            ctx = doc.get("context") or {}
            if isinstance(ctx, str):
                import json
                try:
                    ctx = json.loads(ctx)
                except json.JSONDecodeError:
                    ctx = {}
            resource_name = (
                ctx.get("title")
                or doc.get("name")
                or resource_name
            )
    except Exception:
        pass

    claim_url = build_claim_url(raw_token)

    try:
        return _run_async(
            email_service.send_invite(
                target_email, from_name, resource_name, claim_url, message,
            )
        )
    except Exception as exc:
        logger.warning("send_invite email delivery failed: %s", exc)
        return False


def _run_async(coro):
    """Run *coro* to completion from a sync call site.

    Mirrors mcp_server.server._run_async --- handles both "no loop" and
    "loop already running" cases so grant_service can be called from
    either FastAPI handlers or MCP tool bodies.
    """
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _emit_invite_event(
    resource_id: str,
    event_name: str,
    data: dict,
    *,
    actor_id: Optional[str] = None,
) -> None:
    """Emit a grant.invite.* event. Never raises."""
    try:
        from core.event_bus import emit_artifact_event_sync
        emit_artifact_event_sync(
            resource_id, event_name, data, actor_id=actor_id,
        )
    except Exception as exc:
        logger.debug("invite event emission failed: %s", exc)


# ---------------------------------------------------------------------------
#  Claim
# ---------------------------------------------------------------------------

class InviteClaimError(Exception):
    """Base for expected claim failures. Subclasses map to HTTP statuses."""


class InviteNotFound(InviteClaimError):
    """Token doesn't match any active invite grant. → 404."""


class InviteExhausted(InviteClaimError):
    """Invite has been revoked or reached its claim limit. → 410."""


class InviteIdentityMismatch(InviteClaimError):
    """Authenticated user doesn't match the invite's target identity. → 403."""


def claim_invite(
    db: StandardDatabase,
    user_id: str,
    raw_token: str,
) -> GrantEntity:
    """Claim an invite for *user_id* using *raw_token*.

    Returns the newly-created user grant.

    Raises:
        InviteNotFound: Token doesn't resolve to an active invite.
        InviteExhausted: Invite is revoked or at its claim limit.
        InviteIdentityMismatch: Target identity doesn't match caller.
    """
    invite = _lookup_active_invite(db, raw_token)

    if invite.target_entity and invite.target_entity_type:
        _verify_target_match(db, user_id, invite)

    if invite.max_claims is not None and invite.claims_count >= invite.max_claims:
        raise InviteExhausted("Invite has reached its claim limit")

    now = _now_iso()
    new_grant = GrantEntity(
        id=str(uuid.uuid4()),
        resource_id=invite.resource_id,
        grantee_type=GrantEntity.GRANTEE_USER,
        grantee_id=user_id,
        granted_by=invite.granted_by,
        can_create=invite.can_create,
        can_read=invite.can_read,
        can_update=invite.can_update,
        can_delete=invite.can_delete,
        can_evict=invite.can_evict,
        can_invoke=invite.can_invoke,
        can_add=invite.can_add,
        can_share=invite.can_share,
        can_admin=invite.can_admin,
        requires_identity=True,
        state=GrantEntity.STATE_ACTIVE,
        name=invite.name,
        notes=f"Claimed from invite {invite.id}",
        granted_at=now,
        expires_at=invite.expires_at,
        created_time=now,
        modified_time=now,
    )
    created = create_grant(db, new_grant)

    invite.claims_count += 1
    invite.modified_time = now
    if invite.max_claims == 1:
        invite.state = GrantEntity.STATE_REVOKED
        invite.revoked_at = now
        invite.revoked_by = user_id
    update_grant(db, invite)

    logger.info(
        "invite claimed: invite=%s resource=%s user=%s",
        invite.id, invite.resource_id, user_id,
    )

    _emit_invite_event(
        invite.resource_id,
        "grant.invite.claimed",
        {
            "grant_id": created.id,
            "invite_id": invite.id,
            "user_id": user_id,
        },
        actor_id=user_id,
    )

    return created


def list_invites_sent(
    db: StandardDatabase,
    user_id: str,
    include_revoked: bool = False,
) -> list[GrantEntity]:
    """List invite grants created by *user_id* that are still pending claim.

    Returns grants where ``grantee_type == "invite"`` and ``granted_by == user_id``.
    By default only ``state == active`` are returned. Pass
    ``include_revoked=True`` to include revoked/exhausted invites too.
    """
    from db.arango import query_documents, COLLECTION_GRANTS

    filters: dict = {
        "grantee_type": GrantEntity.GRANTEE_INVITE,
        "granted_by": user_id,
    }
    if not include_revoked:
        filters["state"] = GrantEntity.STATE_ACTIVE

    return query_documents(db, GrantEntity, COLLECTION_GRANTS, filters)


# ---------------------------------------------------------------------------
#  Pre/post-auth context
# ---------------------------------------------------------------------------

def get_invite_context(db: StandardDatabase, raw_token: str) -> Optional[dict]:
    """Return non-PII metadata about an invite.

    Safe to call without authentication. Does NOT reveal inviter identity,
    resource name, or target email. Returns ``None`` if the token is invalid.
    """
    try:
        invite = _lookup_active_invite(db, raw_token)
    except InviteClaimError:
        return None

    return {
        "valid": True,
        "has_target": bool(invite.target_entity),
        "target_type": invite.target_entity_type,
    }


def get_invite_details(
    db: StandardDatabase,
    raw_token: str,
    user_id: str,
) -> Optional[dict]:
    """Return full invite details after verifying caller identity.

    If the invite has a ``target_entity`` and the caller doesn't match,
    returns ``{"valid": True, "identity_mismatch": True}`` without leaking
    inviter/resource PII.
    """
    try:
        invite = _lookup_active_invite(db, raw_token)
    except InviteClaimError:
        return None

    if invite.target_entity and invite.target_entity_type:
        try:
            _verify_target_match(db, user_id, invite)
        except InviteIdentityMismatch:
            return {"valid": True, "identity_mismatch": True}

    return {
        "valid": True,
        "resource_id": invite.resource_id,
        "granted_by": invite.granted_by,
        "name": invite.name,
    }


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------

def _lookup_active_invite(db: StandardDatabase, raw_token: str) -> GrantEntity:
    token_hash = hash_api_key(raw_token)
    candidates = get_active_grants_for_grantee(db, token_hash, "invite")
    if not candidates:
        raise InviteNotFound("Invite not found or expired")
    invite = candidates[0]
    if invite.grantee_type != GrantEntity.GRANTEE_INVITE:
        raise InviteNotFound("Not an invite grant")
    if invite.state != GrantEntity.STATE_ACTIVE:
        raise InviteExhausted("Invite is no longer active")
    return invite


def _verify_target_match(
    db: StandardDatabase,
    user_id: str,
    invite: GrantEntity,
) -> None:
    """Raise InviteIdentityMismatch unless *user_id* matches the invite target."""
    from services.person_service import get_user_by_id

    match = False
    target_type = invite.target_entity_type
    target = invite.target_entity or ""

    if target_type == "user_id":
        match = user_id == target
    elif target_type in ("email", "domain"):
        try:
            person = get_user_by_id(db=db, id=user_id)
        except Exception:
            person = None
        email = (getattr(person, "email", None) or "").lower() if person else ""
        if email:
            if target_type == "email":
                match = email == target.lower()
            else:  # domain
                match = email.endswith("@" + target.lower())

    if not match:
        raise InviteIdentityMismatch("You are not the intended recipient of this invite")
