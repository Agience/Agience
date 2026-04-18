# services/workspace_service.py
#
# Unified Artifact Store service layer.
#
# In the unified model a "workspace" is just a Collection with
# `content_type == "application/vnd.agience.workspace+json"`. Artifacts live
# in a single `artifacts` table and carry a `state` in
# {draft, committed, archived}. Commit is a state flip; no data copies.
#
# Consumers still call `workspace_service.*` with `workspace_id` — that id
# is the collection_id. `db` and `arango_db` / `workspace_db` / `collection_db`
# parameters are all the same ArangoDB handle.

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from arango.database import StandardDatabase
from fastapi import HTTPException, status

from api.workspaces.commit import (
    ArtifactCommitChange,
    CommitActorSummary,
    CollectionChangeSummary,
    CollectionCommitSummary,
    WorkspaceCommitPlanSummary,
    WorkspaceCommitResponse,
)
from entities.artifact import Artifact as ArtifactEntity
from entities.collection import (
    Collection as CollectionEntity,
    WORKSPACE_CONTENT_TYPE,
)
from entities.api_key import APIKey as APIKeyEntity

import db.arango as arango
from db.arango import after_key, mid_key
import core.event_bus as event_bus

logger = logging.getLogger(__name__)


# Workspaces are collections with this content type.
WORKSPACE_MIME = WORKSPACE_CONTENT_TYPE

# ---------------------------------------------------------------------------
# Commit-preview token (HMAC-SHA256, process-scoped secret)
# ---------------------------------------------------------------------------
_COMMIT_TOKEN_SECRET = os.urandom(32)
_COMMIT_TOKEN_TTL_SECONDS = 1800  # 30 minutes


def _commit_token_payload(workspace_id: str, user_id: str, artifact_ids: Optional[List[str]]) -> str:
    ids = ",".join(sorted(artifact_ids)) if artifact_ids else ""
    return f"{workspace_id}|{user_id}|{ids}"


def generate_commit_token(workspace_id: str, user_id: str, artifact_ids: Optional[List[str]] = None) -> str:
    ts = str(int(time.time()))
    payload = f"{ts}|{_commit_token_payload(workspace_id, user_id, artifact_ids)}"
    sig = hmac.new(_COMMIT_TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def validate_commit_token(
    token: Optional[str],
    workspace_id: str,
    user_id: str,
    artifact_ids: Optional[List[str]] = None,
) -> bool:
    if not token or "." not in token:
        return False
    try:
        ts_s, sig = token.split(".", 1)
        ts = int(ts_s)
    except Exception:
        return False
    if abs(time.time() - ts) > _COMMIT_TOKEN_TTL_SECONDS:
        return False
    payload = f"{ts_s}|{_commit_token_payload(workspace_id, user_id, artifact_ids)}"
    expected = hmac.new(_COMMIT_TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# Commit actor (retained for response shape)
# ---------------------------------------------------------------------------

@dataclass
class CommitActor:
    actor_type: str = "user"
    actor_id: str = ""
    subject_user_id: Optional[str] = None
    presenter_type: Optional[str] = None
    presenter_id: Optional[str] = None
    client_id: Optional[str] = None
    host_id: Optional[str] = None
    server_id: Optional[str] = None
    agent_id: Optional[str] = None
    api_key_id: Optional[str] = None
    commit_authorized_by_flag: bool = False

    def to_summary(self) -> CommitActorSummary:
        return CommitActorSummary(**self.__dict__)


def _resolve_commit_actor(user_id: str, api_key: Optional[APIKeyEntity]) -> CommitActor:
    if api_key is not None:
        return CommitActor(
            actor_type="api_key",
            actor_id=str(getattr(api_key, "id", "")),
            subject_user_id=user_id,
            api_key_id=str(getattr(api_key, "id", "")),
        )
    return CommitActor(actor_type="user", actor_id=user_id, subject_user_id=user_id)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_json_str(d: Optional[dict]) -> str:
    return json.dumps(d or {}, separators=(",", ":"), ensure_ascii=False)


def _safe_parse_context(context_json: Optional[str]) -> Dict[str, Any]:
    if not context_json:
        return {}
    try:
        parsed = json.loads(context_json)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _emit_event(collection_id: str, name: str, payload: dict, *, actor_id: Optional[str] = None) -> None:
    try:
        event_bus.emit_artifact_event_sync(collection_id, name, payload, actor_id=actor_id)
    except Exception:
        logger.debug("event bus emit failed", exc_info=True)


def _dispatch_handlers(
    db: StandardDatabase,
    user_id: str,
    collection_id: str,
    event_type: str,
    artifact: Optional[ArtifactEntity],
) -> None:
    try:
        from core.event_dispatcher import dispatch_workspace_event
        dispatch_workspace_event(
            db,
            user_id,
            collection_id,
            event_type=event_type,
            source_artifact_id=getattr(artifact, "id", None),
            source_artifact=artifact,
        )
    except Exception:
        logger.debug("workspace event dispatch failed", exc_info=True)


# ---------------------------------------------------------------------------
# Workspace (= collection) CRUD
# ---------------------------------------------------------------------------

def create_workspace(
    db: StandardDatabase,
    user_id: str,
    name: str,
    is_inbox: bool = False,
    arango_db: Optional[StandardDatabase] = None,
) -> CollectionEntity:
    """Create a new workspace (a collection with content_type=workspace).

    If *is_inbox* is True the workspace id is pinned to *user_id* — this is the
    per-user home collection that holds both drafts and committed artifacts.
    """
    workspace_id = user_id if is_inbox else str(uuid.uuid4())
    entity = CollectionEntity(
        id=workspace_id,
        name=name,
        created_by=user_id,
        content_type=WORKSPACE_CONTENT_TYPE,
        state=CollectionEntity.STATE_COMMITTED if is_inbox else CollectionEntity.STATE_DRAFT,
        context="",
        created_time=_now_iso(),
        modified_time=_now_iso(),
    )
    arango.create_collection(db, entity)

    from services.collection_service import ensure_collection_descriptor
    ensure_collection_descriptor(db, entity)

    # Issue explicit full-CRUDEASIO grant to the creator.
    arango.upsert_user_collection_grant(
        db,
        user_id=user_id,
        collection_id=workspace_id,
        granted_by=user_id,
        can_create=True,
        can_read=True,
        can_update=True,
        can_delete=True,
        can_evict=True,
        can_invoke=True,
        can_add=True,
        can_share=True,
        can_admin=True,
    )

    return entity


def list_workspaces(db: StandardDatabase, user_id: str) -> List[CollectionEntity]:
    return arango.get_collections_by_owner_and_type(db, user_id, WORKSPACE_CONTENT_TYPE)


def get_workspace(db: StandardDatabase, user_id: str, workspace_id: str) -> CollectionEntity:
    entity = arango.get_collection_by_id(db, workspace_id)
    if not entity:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    grants = arango.get_active_grants_for_principal_resource(
        db, grantee_id=user_id, resource_id=workspace_id,
    )
    if not any(getattr(g, "can_read", False) for g in grants):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return entity


def get_workspace_unsafe(db: StandardDatabase, workspace_id: str) -> Optional[CollectionEntity]:
    return arango.get_collection_by_id(db, workspace_id)


def update_workspace(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    name: Optional[str],
    description: Optional[str],
    context: Optional[str] = None,
) -> CollectionEntity:
    ws = get_workspace(db, user_id, workspace_id)
    changed = False
    if name is not None and name != ws.name:
        ws.name = name
        changed = True
    if description is not None and description != ws.description:
        ws.description = description
        changed = True
    if changed:
        ws.modified_time = _now_iso()
        arango.update_collection(db, ws)

        from services.collection_service import ensure_collection_descriptor
        ensure_collection_descriptor(db, ws)

    if context is not None:
        parsed = _safe_parse_context(context) if isinstance(context, str) else context
        update_workspace_context(db, user_id, workspace_id, parsed)
        ws = get_workspace(db, user_id, workspace_id)

    return ws


def delete_workspace(db: StandardDatabase, user_id: str, workspace_id: str) -> None:
    get_workspace(db, user_id, workspace_id)

    # Drop all artifacts in the workspace + their edges + search index docs.
    rows = arango.list_collection_artifacts(db, workspace_id, include_archived=True)
    for row in rows:
        art_id = row.get("id")
        root_id = row.get("root_id") or art_id
        if art_id:
            try:
                from search.ingest.pipeline_unified import delete_artifact_from_index
                delete_artifact_from_index(art_id, root_id)
            except Exception:
                logger.debug("index delete failed", exc_info=True)
            arango.delete_artifacts_by_root(db, root_id)
            arango.remove_all_edges_for_root(db, root_id)

    arango.delete_collection(db, workspace_id)


def get_workspace_context(db: StandardDatabase, user_id: str, workspace_id: str) -> dict:
    ws = get_workspace(db, user_id, workspace_id)
    parsed = _safe_parse_context(ws.context) if isinstance(ws.context, str) else (ws.context or {})
    parsed.setdefault("collections", [])
    return parsed


def update_workspace_context(
    db: StandardDatabase, user_id: str, workspace_id: str, context: dict
) -> dict:
    ws = get_workspace(db, user_id, workspace_id)
    if not isinstance(context, dict):
        context = {}
    context.setdefault("collections", [])
    ws.context = json.dumps(context)
    ws.modified_time = _now_iso()
    arango.update_collection(db, ws)
    return context


def apply_workspace_card_actions(
    db: StandardDatabase, user_id: str, workspace_id: str, actions: List[dict]
) -> dict:
    current = get_workspace_context(db, user_id, workspace_id)
    coll_by_id = {c.get("collection_id"): c for c in current.get("collections", []) if isinstance(c, dict)}
    for act in actions or []:
        if not isinstance(act, dict):
            continue
        if act.get("type") == "attach_collection":
            cid = act.get("collection_id")
            if not cid:
                continue
            existing = coll_by_id.get(cid)
            if existing:
                mode = act.get("mode")
                if mode in ("own", "shared"):
                    existing["mode"] = mode
            else:
                item = {
                    "collection_id": cid,
                    "mode": act.get("mode") if act.get("mode") in ("own", "shared") else "own",
                }
                current["collections"].append(item)
                coll_by_id[cid] = item
    return update_workspace_context(db, user_id, workspace_id, current)


# ---------------------------------------------------------------------------
# Workspace Bindings — Cascade Resolution
# ---------------------------------------------------------------------------

def _extract_bindings(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read the ``bindings`` dict from a parsed context, returning ``{}`` if absent or invalid."""
    if not context or not isinstance(context, dict):
        return {}
    bindings = context.get("bindings")
    if not isinstance(bindings, dict):
        return {}
    return bindings


def _user_can_read_collection(
    db: StandardDatabase, user_id: str, collection_id: str
) -> bool:
    """Return ``True`` if *user_id* has at least read access to *collection_id*."""
    col = arango.get_collection_by_id(db, collection_id)
    if not col:
        return False
    grants = arango.get_active_grants_for_principal_resource(
        db, grantee_id=user_id, resource_id=collection_id,
    )
    return any(getattr(g, "can_read", False) for g in grants)


def _resolve_binding_from(
    bindings: Dict[str, Any], role: str,
) -> Optional[str]:
    """Extract the bound artifact id for *role* from a bindings dict, or ``None``."""
    entry = bindings.get(role)
    if isinstance(entry, dict):
        aid = entry.get("artifact_id")
        if isinstance(aid, str) and aid:
            return aid
    return None


def _resolve_binding_multi_from(
    bindings: Dict[str, Any], role: str,
) -> List[str]:
    """Extract a list of bound artifact ids for a multi-valued *role*."""
    entry = bindings.get(role)
    if isinstance(entry, dict):
        ids = entry.get("artifact_ids")
        if isinstance(ids, list):
            return [i for i in ids if isinstance(i, str) and i]
    return []


def resolve_binding(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    role: str,
    *,
    transform_context: Optional[Dict[str, Any]] = None,
    step_context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve a binding role through the cascade: step → transform → workspace.

    Returns the artifact id of the first accessible binding found, or
    ``None`` if the role is unbound at every level.
    """
    # 1. Step-level
    cid = _resolve_binding_from(_extract_bindings(step_context), role)
    if cid and _user_can_read_collection(db, user_id, cid):
        return cid

    # 2. Transform-level
    cid = _resolve_binding_from(_extract_bindings(transform_context), role)
    if cid and _user_can_read_collection(db, user_id, cid):
        return cid

    # 3. Workspace-level
    ws_context = get_workspace_context(db, user_id, workspace_id)
    cid = _resolve_binding_from(_extract_bindings(ws_context), role)
    if cid and _user_can_read_collection(db, user_id, cid):
        return cid

    # 4. Platform defaults — not implemented in Phase 1
    return None


def resolve_all_bindings(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    *,
    transform_context: Optional[Dict[str, Any]] = None,
    step_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Resolve all binding roles through the cascade.

    Returns ``{role: artifact_id}`` for every role that resolves to an
    accessible artifact.  Roles where the user lacks access are omitted.
    """
    ws_context = get_workspace_context(db, user_id, workspace_id)

    # Collect all role names across cascade levels.
    all_roles: set[str] = set()
    all_roles.update(_extract_bindings(ws_context).keys())
    all_roles.update(_extract_bindings(transform_context).keys())
    all_roles.update(_extract_bindings(step_context).keys())

    result: Dict[str, str] = {}
    for role in all_roles:
        # Inline cascade to avoid re-reading workspace context per role.
        cid = _resolve_binding_from(_extract_bindings(step_context), role)
        if cid and _user_can_read_collection(db, user_id, cid):
            result[role] = cid
            continue

        cid = _resolve_binding_from(_extract_bindings(transform_context), role)
        if cid and _user_can_read_collection(db, user_id, cid):
            result[role] = cid
            continue

        cid = _resolve_binding_from(_extract_bindings(ws_context), role)
        if cid and _user_can_read_collection(db, user_id, cid):
            result[role] = cid

    return result


# ---------------------------------------------------------------------------
# Workspace Bindings — Multi-valued Resolution
# ---------------------------------------------------------------------------

# Known binding roles and their cardinality.
SINGLE_BINDING_ROLES = {"memory", "tools", "resources", "ask_prompt", "engagement_channels"}
MULTI_BINDING_ROLES = {"target_collections"}
KNOWN_BINDING_ROLES = SINGLE_BINDING_ROLES | MULTI_BINDING_ROLES


def resolve_binding_multi(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    role: str,
    *,
    transform_context: Optional[Dict[str, Any]] = None,
    step_context: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Resolve a multi-valued binding role through the cascade.

    Returns a list of artifact ids for which the user has read access.
    """
    for ctx in (step_context, transform_context):
        ids = _resolve_binding_multi_from(_extract_bindings(ctx), role)
        accessible = [i for i in ids if _user_can_read_collection(db, user_id, i)]
        if accessible:
            return accessible

    ws_context = get_workspace_context(db, user_id, workspace_id)
    ids = _resolve_binding_multi_from(_extract_bindings(ws_context), role)
    return [i for i in ids if _user_can_read_collection(db, user_id, i)]


# ---------------------------------------------------------------------------
# Workspace Bindings — Set / Clear
# ---------------------------------------------------------------------------

def set_binding(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    role: str,
    *,
    artifact_id: Optional[str] = None,
    artifact_ids: Optional[List[str]] = None,
) -> dict:
    """Set a workspace binding for *role*.

    Single-valued roles accept ``artifact_id``; multi-valued roles accept
    ``artifact_ids``.  Raises ``ValueError`` on unknown role or cardinality
    mismatch.  Raises ``HTTPException(403)`` if the caller cannot update.
    """
    if role not in KNOWN_BINDING_ROLES:
        raise ValueError(f"Unknown binding role: {role}")

    if role in MULTI_BINDING_ROLES:
        if artifact_id is not None or artifact_ids is None:
            raise ValueError(f"Multi-valued role '{role}' requires artifact_ids, not artifact_id")
        value: Dict[str, Any] = {"artifact_ids": artifact_ids}
    else:
        if artifact_ids is not None or artifact_id is None:
            raise ValueError(f"Single-valued role '{role}' requires artifact_id, not artifact_ids")
        value = {"artifact_id": artifact_id}

    ctx = get_workspace_context(db, user_id, workspace_id)
    bindings = ctx.setdefault("bindings", {})
    bindings[role] = value
    update_workspace_context(db, user_id, workspace_id, ctx)

    event_bus.emit("workspace.binding.set", {
        "workspace_id": workspace_id,
        "role": role,
        "binding": value,
    })
    return value


def clear_binding(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    role: str,
) -> None:
    """Remove the binding for *role* from the workspace context."""
    ctx = get_workspace_context(db, user_id, workspace_id)
    bindings = ctx.get("bindings")
    if isinstance(bindings, dict) and role in bindings:
        del bindings[role]
        update_workspace_context(db, user_id, workspace_id, ctx)

    event_bus.emit("workspace.binding.cleared", {
        "workspace_id": workspace_id,
        "role": role,
    })


# ---------------------------------------------------------------------------
# Artifact CRUD (collection-scoped)
# ---------------------------------------------------------------------------

def list_workspace_artifacts(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
) -> List[ArtifactEntity]:
    get_workspace(db, user_id, workspace_id)
    rows = arango.list_collection_artifacts(db, workspace_id)
    return [ArtifactEntity.from_dict(r) for r in rows]


def get_workspace_artifact(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_id: str,
) -> ArtifactEntity:
    get_workspace(db, user_id, workspace_id)
    artifact = arango.get_artifact(db, artifact_id)
    if not artifact or artifact.collection_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")
    return artifact


def get_artifact_unsafe_by_id(db: StandardDatabase, artifact_id: str) -> Optional[ArtifactEntity]:
    return arango.get_artifact(db, artifact_id)


def get_workspace_artifacts_batch(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_ids: List[str],
) -> List[ArtifactEntity]:
    get_workspace(db, user_id, workspace_id)
    out: List[ArtifactEntity] = []
    for aid in artifact_ids:
        a = arango.get_artifact(db, aid)
        if a and a.collection_id == workspace_id:
            out.append(a)
    return out


def get_workspace_artifacts_batch_global(
    db: StandardDatabase,
    user_id: str,
    artifact_ids: List[str],
) -> List[ArtifactEntity]:
    """Fetch artifacts across any workspace the user owns."""
    workspaces = {w.id for w in list_workspaces(db, user_id)}
    out: List[ArtifactEntity] = []
    for aid in artifact_ids:
        a = arango.get_artifact(db, aid)
        if a and a.collection_id in workspaces:
            out.append(a)
    return out


def _store_content_in_s3(
    artifact_id: str,
    content: str,
    context_str: str,
) -> Tuple[str, str]:
    """Upload inline content to S3 and return (content_key, inline_content).

    For small text (< 128 KB), the content is also kept inline in ArangoDB as a
    fallback so the artifact remains readable even if S3 is temporarily
    unreachable.  Large content (>= 128 KB) is cleared from inline to keep the
    document store lean.

    Derives the content_type from the artifact context if present.
    Falls back to text/plain. Idempotent — safe to call on every create/update.
    """
    from services.content_service import put_text_direct

    try:
        ctx = json.loads(context_str) if context_str else {}
    except (json.JSONDecodeError, TypeError):
        ctx = {}

    content_type = ctx.get("content_type") or "text/plain"
    content_key = ctx.get("content_key") or f"artifacts/{artifact_id}.content"

    try:
        put_text_direct(content_key, content, content_type)
    except Exception:
        logger.warning("Failed to upload artifact content to S3 for %s — keeping inline", artifact_id, exc_info=True)
        return content_key, content  # degrade gracefully: keep inline

    # Keep small text inline as a fallback; clear inline for large content.
    if len(content.encode("utf-8")) <= 131_072:  # 128 KB
        return content_key, content
    return content_key, ""


def _link_to_target_collections(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact: ArtifactEntity,
) -> None:
    """Create ``collection_artifacts`` edges to each target collection bound to the workspace.

    Skips silently (logs at INFO) for any target the caller cannot add to.
    """
    target_ids = resolve_binding_multi(db, user_id, workspace_id, "target_collections")
    for target_id in target_ids:
        try:
            # Check the user has at least read (access proxy for "can_add") on the target.
            if not _user_can_read_collection(db, user_id, target_id):
                logger.info(
                    "Skipping target_collection link: user %s lacks access to %s",
                    user_id, target_id,
                )
                continue
            arango.add_artifact_to_collection(db, target_id, artifact.root_id)
            event_bus.emit("workspace.target_collection.linked", {
                "workspace_id": workspace_id,
                "target_collection_id": target_id,
                "artifact_id": artifact.id,
            })
        except Exception:
            logger.info(
                "Failed to link artifact %s to target_collection %s",
                artifact.id, target_id, exc_info=True,
            )


def create_workspace_artifact(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    context: str,
    content: str,
    root_id: Optional[str] = None,
    order_key: Optional[str] = None,
    enqueue_index: bool = True,
    dispatch_handlers: bool = True,
    name: Optional[str] = None,
    content_type: Optional[str] = None,
) -> ArtifactEntity:
    get_workspace(db, user_id, workspace_id)

    now = _now_iso()
    artifact_id = str(uuid.uuid4())

    # Store content in S3; update context with content_key.
    resolved_content = content
    if content:
        content_key, resolved_content = _store_content_in_s3(artifact_id, content, context)
        # Inject content_key into context JSON if not already present.
        try:
            ctx_obj = json.loads(context) if context else {}
        except (json.JSONDecodeError, TypeError):
            ctx_obj = {}
        if "content_key" not in ctx_obj:
            ctx_obj["content_key"] = content_key
            context = json.dumps(ctx_obj)

    artifact = ArtifactEntity(
        id=artifact_id,
        root_id=root_id or artifact_id,
        collection_id=workspace_id,
        context=context,
        content=resolved_content,
        state=ArtifactEntity.STATE_DRAFT,
        created_by=user_id,
        modified_by=user_id,
        created_time=now,
        modified_time=now,
        name=name,
        content_type=content_type,
    )
    arango.create_artifact(db, artifact)

    # Insert the stable collection_artifacts edge pointing at root_id.
    if order_key is None:
        order_key = after_key(arango.get_last_order_key(db, workspace_id))
    arango.add_artifact_to_collection(db, workspace_id, artifact.root_id, order_key)

    # Wire target_collections binding: create collection_artifacts edges to
    # each target collection so the draft artifact is associated immediately.
    _link_to_target_collections(db, user_id, workspace_id, artifact)

    if enqueue_index:
        try:
            from search.ingest.pipeline_unified import enqueue_index_artifact
            enqueue_index_artifact(artifact, artifact.collection_id, tenant_id=user_id)
        except Exception:
            logger.debug("index enqueue failed", exc_info=True)

    if dispatch_handlers:
        _dispatch_handlers(db, user_id, workspace_id, "artifact_created", artifact)

    _emit_event(workspace_id, "artifact.created", {"artifact": artifact.to_dict()}, actor_id=user_id)
    return artifact


def create_workspace_artifacts_bulk(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    items: Sequence[Union[Tuple[str, str], Tuple[str, str, Optional[Sequence[str]]], Dict[str, Any]]],
    dispatch_handlers: bool = True,
) -> List[ArtifactEntity]:
    get_workspace(db, user_id, workspace_id)

    out: List[ArtifactEntity] = []
    for raw in items:
        if isinstance(raw, dict):
            context_val = raw.get("context", "")
            content_val = raw.get("content", "")
        elif isinstance(raw, (list, tuple)):
            context_val = raw[0] if len(raw) >= 1 else ""
            content_val = raw[1] if len(raw) >= 2 else ""
        else:
            raise ValueError("Bulk item must be mapping or tuple")

        if not isinstance(context_val, str):
            context_val = json.dumps(context_val or {})
        if not isinstance(content_val, str):
            content_val = json.dumps(content_val)

        out.append(
            create_workspace_artifact(
                db,
                user_id,
                workspace_id,
                context=context_val,
                content=content_val,
                enqueue_index=True,
                dispatch_handlers=dispatch_handlers,
            )
        )
    return out


def _ensure_draft(
    db: StandardDatabase, user_id: str, committed: ArtifactEntity
) -> ArtifactEntity:
    """
    Edit-after-commit: create a new draft record with the same root_id
    containing a copy of the committed content, leaving the committed
    version untouched.
    """
    existing_draft = arango.get_draft_artifact(db, committed.root_id, committed.collection_id)
    if existing_draft:
        return existing_draft

    now = _now_iso()
    new_id = str(uuid.uuid4())

    draft = ArtifactEntity(
        id=new_id,
        root_id=committed.root_id,
        collection_id=committed.collection_id,
        context=committed.context,
        content=committed.content,
        state=ArtifactEntity.STATE_DRAFT,
        created_by=user_id,
        modified_by=user_id,
        created_time=now,
        modified_time=now,
    )
    arango.create_artifact(db, draft)
    return draft


def update_artifact(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_id: str,
    context: Optional[str] = None,
    content: Optional[str] = None,
    state: Optional[str] = None,
    content_type: Optional[str] = None,
    reindex: bool = True,
    dispatch_handlers: bool = True,
) -> ArtifactEntity:
    get_workspace(db, user_id, workspace_id)

    target = arango.get_artifact(db, artifact_id)
    if target is not None and target.collection_id != workspace_id:
        target = None
    if target is None:
        target = arango.get_draft_artifact(db, artifact_id, workspace_id)
    if target is None:
        target = arango.get_latest_committed_artifact(db, artifact_id, workspace_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")

    # Archive toggle.
    if state == ArtifactEntity.STATE_ARCHIVED and target.state != ArtifactEntity.STATE_ARCHIVED:
        target.state = ArtifactEntity.STATE_ARCHIVED
        target.modified_by = user_id
        target.modified_time = _now_iso()
        if arango.update_artifact(db, target) is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to persist artifact update")
        _emit_event(workspace_id, "artifact.updated", {"artifact": target.to_dict()}, actor_id=user_id)
        return target

    if target.state == ArtifactEntity.STATE_ARCHIVED and state and state != ArtifactEntity.STATE_ARCHIVED:
        target.state = ArtifactEntity.STATE_DRAFT
        target.modified_by = user_id
        target.modified_time = _now_iso()
        if arango.update_artifact(db, target) is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to persist artifact update")
        _emit_event(workspace_id, "artifact.updated", {"artifact": target.to_dict()}, actor_id=user_id)
        return target

    if target.state == ArtifactEntity.STATE_ARCHIVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot edit an archived artifact")

    # If we're editing a committed version, promote to a new draft with same root_id.
    if target.state == ArtifactEntity.STATE_COMMITTED:
        target = _ensure_draft(db, user_id, target)

    dirty = False
    if context is not None and context != target.context:
        target.context = context
        dirty = True
    if content_type is not None and content_type != target.content_type:
        target.content_type = content_type
        dirty = True
    if content is not None and content != target.content:
        # Store new content in S3; update target.context with content_key.
        if content:
            content_key, stored_content = _store_content_in_s3(target.id, content, target.context)
            try:
                ctx_obj = json.loads(target.context) if target.context else {}
            except (json.JSONDecodeError, TypeError):
                ctx_obj = {}
            ctx_obj["content_key"] = content_key
            target.context = json.dumps(ctx_obj)
            target.content = stored_content
        else:
            target.content = content
        dirty = True
    if not dirty:
        return target

    target.modified_by = user_id
    target.modified_time = _now_iso()
    result = arango.update_artifact(db, target)
    if result is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to persist artifact update")

    if reindex:
        try:
            from search.ingest.pipeline_unified import enqueue_index_artifact
            enqueue_index_artifact(target, target.collection_id, tenant_id=user_id)
        except Exception:
            logger.debug("reindex failed", exc_info=True)

    if dispatch_handlers:
        _dispatch_handlers(db, user_id, workspace_id, "artifact_updated", target)

    _emit_event(workspace_id, "artifact.updated", {"artifact": target.to_dict()}, actor_id=user_id)
    return target


def delete_artifact(
    db: StandardDatabase, user_id: str, workspace_id: str, artifact_id: str
) -> None:
    artifact = get_workspace_artifact(db, user_id, workspace_id, artifact_id)

    # S3 cleanup — read the content_key straight from context (spec § delete fix).
    try:
        ctx = _safe_parse_context(artifact.context)
        content_key = ctx.get("content_key")
        if content_key:
            from services.content_service import delete_object
            delete_object(content_key)
    except Exception:
        logger.debug("S3 cleanup failed", exc_info=True)

    # If this was the only version for the root, drop all edges too.
    other_versions = [
        v for v in arango.list_version_history(db, artifact.root_id) if v.id != artifact_id
    ]
    draft = arango.get_draft_artifact(db, artifact.root_id, workspace_id)

    arango.delete_artifact(db, artifact_id)

    if not other_versions and (draft is None or draft.id == artifact_id):
        arango.remove_all_edges_for_root(db, artifact.root_id)

    try:
        from search.ingest.pipeline_unified import delete_artifact_from_index
        delete_artifact_from_index(artifact_id, artifact.root_id)
    except Exception:
        logger.debug("search delete failed", exc_info=True)

    _dispatch_handlers(db, user_id, workspace_id, "artifact_deleted", artifact)
    _emit_event(workspace_id, "artifact.deleted", {"artifact_id": artifact_id}, actor_id=user_id)


def remove_artifact_from_workspace(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_id: str,
) -> ArtifactEntity:
    """Remove an artifact from a workspace without hard-deleting the underlying root.

    The workspace view is edge-based. Removing a card should unlink the root from the
    workspace. If the current workspace version is a draft row owned by this workspace,
    delete that draft row as part of the removal so the draft does not linger.
    """
    get_workspace(db, user_id, workspace_id)

    # Resolve the root_id: the caller may pass a version id or a root_id.
    root_id = artifact_id
    artifact = arango.get_artifact(db, artifact_id)
    if artifact:
        root_id = artifact.root_id or artifact.id

    # The edge is the canonical link. If there's no edge, the artifact
    # is not in this workspace.
    edge = arango.get_edge(db, workspace_id, root_id)
    if not edge:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")

    arango.remove_artifact_from_collection(db, workspace_id, root_id)

    # If a draft version is owned by this workspace, clean it up.
    local = arango.get_current_in_collection(db, workspace_id, root_id)
    if local and local.state == ArtifactEntity.STATE_DRAFT and local.collection_id == workspace_id:
        arango.delete_artifact(db, local.id)
        try:
            from search.ingest.pipeline_unified import delete_artifact_from_index
            delete_artifact_from_index(local.id, local.root_id)
        except Exception:
            logger.debug("search delete failed", exc_info=True)

    _emit_event(workspace_id, "artifact.deleted", {"artifact_id": artifact_id}, actor_id=user_id)
    return artifact or arango.get_artifact(db, root_id) or ArtifactEntity(id=root_id, root_id=root_id)


def revert_artifact(
    workspace_db: StandardDatabase,
    collection_db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_id: str,
) -> Optional[ArtifactEntity]:
    """
    Revert: drop the current draft, leaving the committed version in place.
    Returns the surviving committed version (or None if none exists).
    """
    get_workspace(workspace_db, user_id, workspace_id)
    target = arango.get_artifact(workspace_db, artifact_id)
    if not target or target.collection_id != workspace_id:
        return None

    if target.state != ArtifactEntity.STATE_DRAFT:
        return target

    committed = arango.get_latest_committed_artifact(
        workspace_db, target.root_id, workspace_id
    )
    if not committed:
        return None

    arango.delete_artifact(workspace_db, target.id)

    try:
        from search.ingest.pipeline_unified import enqueue_index_artifact
        enqueue_index_artifact(committed, committed.collection_id, tenant_id=user_id)
    except Exception:
        logger.debug("reindex failed", exc_info=True)

    _dispatch_handlers(workspace_db, user_id, workspace_id, "artifact_updated", committed)
    _emit_event(workspace_id, "artifact.updated", {"artifact": committed.to_dict()}, actor_id=user_id)
    return committed


def add_artifact_to_workspace(
    workspace_db: StandardDatabase,
    collection_db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_root_id: str,
    api_key: Optional[APIKeyEntity] = None,
) -> Optional[ArtifactEntity]:
    """
    Link an existing artifact (by root_id) into another collection via an edge.
    This is the "publish" action from the spec — it does not create a new
    artifact record.
    """
    get_workspace(workspace_db, user_id, workspace_id)

    # Must resolve to a committed version somewhere.
    committed = arango.get_latest_committed_artifact(workspace_db, artifact_root_id)
    if not committed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not readable")

    order_key = after_key(arango.get_last_order_key(workspace_db, workspace_id))
    arango.add_artifact_to_collection(workspace_db, workspace_id, artifact_root_id, order_key)
    return committed


def move_artifact_between_workspaces(
    db: StandardDatabase,
    user_id: str,
    source_workspace_id: str,
    target_workspace_id: str,
    artifact_id: str,
) -> ArtifactEntity:
    get_workspace(db, user_id, source_workspace_id)
    get_workspace(db, user_id, target_workspace_id)

    artifact = arango.get_artifact(db, artifact_id)
    if not artifact or artifact.collection_id != source_workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")

    artifact.collection_id = target_workspace_id
    artifact.modified_by = user_id
    artifact.modified_time = _now_iso()
    arango.update_artifact(db, artifact)

    arango.remove_artifact_from_collection(db, source_workspace_id, artifact.root_id)
    order_key = after_key(arango.get_last_order_key(db, target_workspace_id))
    arango.add_artifact_to_collection(db, target_workspace_id, artifact.root_id, order_key)

    _emit_event(source_workspace_id, "artifact.deleted", {"artifact_id": artifact_id}, actor_id=user_id)
    _emit_event(target_workspace_id, "artifact.created", {"artifact": artifact.to_dict()}, actor_id=user_id)
    return artifact


def order_workspace_artifacts(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    ordered_ids: List[str],
    expected_version: Optional[int] = None,
) -> int:
    get_workspace(db, user_id, workspace_id)
    # `ordered_ids` may be version ids OR root ids — resolve to root ids.
    ordered_roots: List[str] = []
    for aid in ordered_ids:
        a = arango.get_artifact(db, aid)
        if a:
            ordered_roots.append(a.root_id)
    arango.reorder_collection_artifacts(db, workspace_id, ordered_roots)
    return 0


def move_workspace_artifact(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_id: str,
    before_id: Optional[str],
    after_id: Optional[str],
    expected_version: Optional[int] = None,
) -> int:
    get_workspace(db, user_id, workspace_id)
    artifact = arango.get_artifact(db, artifact_id)
    if not artifact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")
    root_id = artifact.root_id

    before_key = None
    after_key_str = None
    if before_id:
        before = arango.get_edge(db, workspace_id, before_id) or {}
        before_key = before.get("order_key")
    if after_id:
        after_rec = arango.get_edge(db, workspace_id, after_id) or {}
        after_key_str = after_rec.get("order_key")

    new_key = mid_key(before_key, after_key_str)
    arango.set_edge_order_key(db, workspace_id, root_id, new_key)
    return 0


def get_artifacts_order_version(db: StandardDatabase, user_id: str, workspace_id: str) -> int:
    get_workspace(db, user_id, workspace_id)
    return 0  # no longer tracked — edges are authoritative


# ---------------------------------------------------------------------------
# Commit — batch state flip
# ---------------------------------------------------------------------------

def commit_workspace_to_collections(
    workspace_db: StandardDatabase,
    collection_db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    *,
    artifact_ids: Optional[List[str]] = None,
    dry_run: bool = False,
    api_key: Optional[APIKeyEntity] = None,
    commit_token: Optional[str] = None,
    **_unused: Any,
) -> WorkspaceCommitResponse:
    """
    Commit = flip drafts to committed in-place. No cross-table copies, no
    membership deltas. `artifact_ids` optionally narrows to a subset;
    `dry_run` returns the planned changes without mutating state.
    """
    get_workspace(workspace_db, user_id, workspace_id)

    # Collect candidate drafts.
    drafts = arango.list_draft_artifacts(workspace_db, workspace_id)
    if artifact_ids:
        wanted = set(artifact_ids)
        # Accept either version id or root id.
        drafts = [d for d in drafts if d.id in wanted or d.root_id in wanted]

    # Build the plan.
    changes: List[ArtifactCommitChange] = []
    for d in drafts:
        changes.append(
            ArtifactCommitChange(
                artifact_id=d.id,
                root_id=d.root_id,
                action="commit",
                state_before=ArtifactEntity.STATE_DRAFT,
                state_after=ArtifactEntity.STATE_COMMITTED,
                target_collections=[workspace_id],
                committed_collections=[workspace_id],
            )
        )

    plan = WorkspaceCommitPlanSummary(
        artifacts=changes,
        collections=[
            CollectionChangeSummary(
                collection_id=workspace_id,
                added_artifacts=[d.id for d in drafts],
            )
        ] if drafts else [],
        warnings=[],
        total_artifacts=len(drafts),
        total_adds=len(drafts),
        total_removes=0,
    )

    actor = _resolve_commit_actor(user_id, api_key).to_summary()

    if dry_run or not drafts:
        token = generate_commit_token(workspace_id, user_id, [d.id for d in drafts])
        return WorkspaceCommitResponse(
            workspace_id=workspace_id,
            plan=plan,
            actor=actor,
            dry_run=dry_run,
            commit_token=token,
            updated_workspace_artifacts=[],
            deleted_workspace_artifact_ids=[],
            skipped_workspace_artifact_ids=[],
            per_collection=[
                CollectionCommitSummary(
                    collection_id=workspace_id,
                    adds=[d.id for d in drafts],
                )
            ] if drafts else [],
        )

    # Apply: single AQL UPDATE for the whole batch.
    now = _now_iso()
    committed_count = arango.batch_commit_drafts(
        workspace_db,
        collection_id=workspace_id,
        artifact_ids=[d.id for d in drafts],
        committed_by=user_id,
        committed_time=now,
    )

    # Provenance record.
    try:
        from entities.commit import Commit as CommitEntity, CommitItem
        commit_entity = CommitEntity(
            collection_id=workspace_id,
            author_id=user_id,
            message=f"commit {committed_count} drafts",
            timestamp=now,
            item_ids=[d.id for d in drafts],
        )
        arango.create_commit(collection_db, commit_entity)
        arango.create_commit_items(
            collection_db,
            [
                CommitItem(
                    commit_id=commit_entity.id,
                    collection_id=workspace_id,
                    artifact_root_id=d.root_id,
                    artifact_version_id=d.id,
                )
                for d in drafts
            ],
        )
    except Exception:
        logger.debug("provenance record failed", exc_info=True)

    # Re-index each committed artifact.
    try:
        from search.ingest.pipeline_unified import enqueue_index_artifact
        for d in drafts:
            d.state = ArtifactEntity.STATE_COMMITTED
            enqueue_index_artifact(d, d.collection_id, tenant_id=user_id)
    except Exception:
        logger.debug("post-commit reindex failed", exc_info=True)

    for d in drafts:
        _emit_event(workspace_id, "artifact.updated", {"artifact": d.to_dict()}, actor_id=user_id)

    updated_dicts = [d.to_dict() for d in drafts]
    return WorkspaceCommitResponse(
        workspace_id=workspace_id,
        plan=plan,
        actor=actor,
        dry_run=False,
        commit_token=None,
        updated_workspace_artifacts=updated_dicts,
        deleted_workspace_artifact_ids=[],
        skipped_workspace_artifact_ids=[],
        per_collection=[
            CollectionCommitSummary(
                collection_id=workspace_id,
                adds=[d.id for d in drafts],
            )
        ],
    )


# ---------------------------------------------------------------------------
# File upload helpers
# ---------------------------------------------------------------------------

def _tenant_prefix_for_workspace(db: StandardDatabase, user_id: str, workspace_id: str) -> str:
    ws = get_workspace(db, user_id, workspace_id)
    return f"{ws.created_by}"


def initiate_upload_and_create_artifact(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    filename: str,
    content_type: str,
    size: int,
    order_key: Optional[str] = None,
    context: Optional[dict] = None,
):
    from services.content_service import presign_put_or_multipart, get_content_storage_mode
    from services.ingest_runner_service import describe_content_processing

    tenant = _tenant_prefix_for_workspace(db, user_id, workspace_id)

    base_ctx: Dict[str, Any] = {
        "content_source": "agience-content",
        "access": "private",
        "filename": filename,
        "content_type": content_type,
        "size": size,
        "storage": {"mode": get_content_storage_mode()},
        "processing": describe_content_processing(content_type, upload_complete=False),
        "upload": {"status": "initiated", "progress": 0.0},
    }
    if context:
        base_ctx.update(context)

    artifact = create_workspace_artifact(
        db,
        user_id,
        workspace_id,
        context=_ensure_json_str(base_ctx),
        content="",
        order_key=order_key,
        enqueue_index=False,
        dispatch_handlers=False,
    )

    key = f"{tenant}/{artifact.id}.content"
    presign = presign_put_or_multipart(key, content_type, size)

    patched_ctx = _safe_parse_context(artifact.context)
    patched_ctx["content_key"] = key
    up = patched_ctx.setdefault("upload", {})
    up["mode"] = presign["mode"]
    up["s3_key"] = key
    if presign["mode"] == "multipart":
        up["multipart_id"] = presign["uploadId"]

    updated = update_artifact(
        db, user_id, workspace_id, artifact.id,
        context=_ensure_json_str(patched_ctx),
        reindex=False,
        dispatch_handlers=False,
    )

    return (
        {
            "upload_id": updated.id,
            "mode": presign["mode"],
            "url": presign.get("url"),
            "uploadId": presign.get("uploadId"),
            "key": key,
        },
        updated,
    )


def update_upload_status(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    upload_id: str,
    status_value: str,
    progress: Optional[float] = None,
    parts: Optional[List[Dict]] = None,
    context_patch: Optional[Dict] = None,
):
    from services.content_service import (
        complete_multipart,
        head_object,
        persist_object_to_durable,
    )
    from services.ingest_runner_service import describe_content_processing

    artifact = get_workspace_artifact(db, user_id, workspace_id, upload_id)
    ctx = _safe_parse_context(artifact.context)

    if context_patch:
        for k, v in context_patch.items():
            ctx[k] = v

    up = dict(ctx.get("upload") or {})
    if progress is not None:
        up["progress"] = max(0.0, min(1.0, progress))

    key = up.get("s3_key")
    mode = up.get("mode")

    if status_value in ("uploading", "failed"):
        up["status"] = status_value
        ctx["upload"] = up
        if status_value == "failed":
            processing = dict(ctx.get("processing") or {})
            for k in ("asset_status", "content_status", "index_status", "status"):
                processing[k] = "failed"
            ctx["processing"] = processing
        return update_artifact(
            db, user_id, workspace_id, upload_id,
            context=_ensure_json_str(ctx),
            reindex=False,
            dispatch_handlers=False,
        )

    if status_value == "complete":
        if not key or not mode:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload context missing key or mode")

        if mode == "multipart":
            multipart_id = up.get("multipart_id")
            if not multipart_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Multipart requires multipart_id")
            effective_parts = parts
            if not effective_parts:
                head = head_object(key)
                if not head:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Multipart requires parts list")
                effective_parts = []
            normalized: List[Dict[str, Any]] = []
            for p in effective_parts or []:
                if not isinstance(p, dict):
                    continue
                part_num = p.get("PartNumber") or p.get("part_number") or p.get("partNumber") or p.get("part")
                etag = p.get("ETag") or p.get("etag") or p.get("e_tag")
                if not part_num or not etag:
                    continue
                try:
                    part_num_int = int(part_num)
                except Exception:
                    continue
                if isinstance(etag, str) and etag.startswith('"') and etag.endswith('"'):
                    etag = etag[1:-1]
                normalized.append({"PartNumber": part_num_int, "ETag": etag})
            if normalized:
                normalized.sort(key=lambda x: x["PartNumber"])
                complete_multipart(key, multipart_id, normalized)

        head = head_object(key)
        if not head:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Object not found in S3")

        ctx["size"] = head.get("ContentLength", ctx.get("size"))
        ctx["content_type"] = head.get("ContentType", ctx.get("content_type"))
        ctx["processing"] = describe_content_processing(ctx.get("content_type") or "", upload_complete=True)

        try:
            if persist_object_to_durable(key):
                storage = dict(ctx.get("storage") or {})
                storage["durable_synced"] = True
                storage["durable_key"] = key
                ctx["storage"] = storage
        except Exception:
            logger.warning("Durable content sync failed for key=%s", key)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Failed to persist upload to durable storage")

        ctx.pop("upload", None)

        result = update_artifact(
            db, user_id, workspace_id, upload_id,
            context=_ensure_json_str(ctx),
        )
        _emit_event(workspace_id, "upload.complete", {"artifact": result.to_dict()}, actor_id=user_id)
        return result

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status")


# ---------------------------------------------------------------------------
# Artifact-scoped API keys (inbound / stream)
# ---------------------------------------------------------------------------

_KEY_CONTEXT_MAP: Dict[str, Tuple[str, str, str]] = {
    "stream":  ("stream",  "obs_api_key_id", "Stream Source"),
    "inbound": ("inbound", "api_key_id",     "Inbound Source"),
}


def rotate_artifact_key(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    artifact_id: str,
    arango_db: StandardDatabase,
    key_context: str,
) -> Dict[str, str]:
    from services import auth_service
    binding = _KEY_CONTEXT_MAP.get(key_context)
    if not binding:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown key_context: {key_context}")
    context_section, key_id_field, key_label_prefix = binding

    artifact = get_workspace_artifact(db, user_id, workspace_id, artifact_id)
    ctx = _safe_parse_context(artifact.context)
    section_cfg = dict(ctx.get(context_section) or {})

    old_key_id = section_cfg.get(key_id_field)
    if isinstance(old_key_id, str) and old_key_id.strip():
        try:
            arango.delete_api_key(arango_db, old_key_id)
        except Exception:
            pass

    raw_key = auth_service.generate_api_key()
    key_hash = auth_service.hash_api_key(raw_key)
    now = _now_iso()
    api_key = APIKeyEntity(
        id=str(uuid.uuid4()),
        user_id=user_id,
        key_hash=key_hash,
        name=f"{key_label_prefix} - {artifact_id}",
        scopes=[
            f"resource:{WORKSPACE_CONTENT_TYPE}:read",
            f"resource:{WORKSPACE_CONTENT_TYPE}:write",
        ],
        resource_filters={"collections": [workspace_id]},
        created_time=now,
        modified_time=now,
        expires_at=None,
        last_used_at=None,
        is_active=True,
    )
    created = arango.create_api_key(arango_db, api_key)

    section_cfg[key_id_field] = created.id
    ctx[context_section] = section_cfg
    update_artifact(db, user_id, workspace_id, artifact_id, context=_ensure_json_str(ctx))

    return {
        "workspace_id": workspace_id,
        "artifact_id": artifact_id,
        "key_id": created.id,
        "key": f"{artifact_id}:{raw_key}",
    }


def resolve_card_api_key(
    db: StandardDatabase,
    artifact_id: str,
    token: str,
    arango_db: StandardDatabase,
    key_context: Optional[str] = None,
) -> Tuple[ArtifactEntity, CollectionEntity, str]:
    from services import auth_service

    artifact = arango.get_artifact(db, artifact_id)
    if not artifact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    workspace_id = artifact.collection_id
    if not token.startswith("agc_"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    api_key = auth_service.verify_api_key(arango_db, token)
    if not api_key or not api_key.user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    ws = arango.get_collection_by_id(db, workspace_id)
    if not ws:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    grants = arango.get_active_grants_for_principal_resource(
        db, grantee_id=api_key.user_id, resource_id=workspace_id,
    )
    if not any(getattr(g, "can_read", False) for g in grants):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if not api_key.can_access_resource("collections", workspace_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    ctx = _safe_parse_context(artifact.context)
    binding = _KEY_CONTEXT_MAP.get(key_context) if key_context else None
    if binding:
        section_cfg = ctx.get(binding[0]) or {}
        if isinstance(section_cfg, dict):
            expected = section_cfg.get(binding[1])
            if isinstance(expected, str) and expected.strip():
                if str(getattr(api_key, "id", "") or "") != expected.strip():
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    return artifact, ws, ws.created_by


def receive_card_inbound_message(
    db: StandardDatabase,
    artifact_id: str,
    token: str,
    text: str,
    channel: Optional[str] = None,
    context: Optional[dict] = None,
    metadata: Optional[dict] = None,
    arango_db: Optional[StandardDatabase] = None,
) -> Tuple[str, str]:
    source_artifact, ws, owner_id = resolve_card_api_key(
        db, artifact_id, token, arango_db or db, key_context="inbound"
    )
    card_ctx: Dict[str, Any] = {
        "source_artifact_id": artifact_id,
        "inbound": {"channel": channel or "unknown", "via": "webhook"},
    }
    if isinstance(context, dict):
        card_ctx.update(context)
    if isinstance(metadata, dict):
        card_ctx.setdefault("metadata", metadata)

    msg = create_workspace_artifact(
        db=db, user_id=owner_id, workspace_id=ws.id,
        context=_ensure_json_str(card_ctx), content=text or "",
    )
    return msg.id or "", ws.id


# ---------------------------------------------------------------------------
# Native dispatch handlers — called by operation_dispatcher for type.json
# ``dispatch: { kind: "native", target: "workspace_service.<fn>" }``
# ---------------------------------------------------------------------------

async def dispatch_create_workspace(artifact: dict, body: dict, ctx: Any) -> dict:
    """Create a workspace via the ``create`` operation on workspace type."""
    name = (body or {}).get("name", "New Workspace")
    ws = create_workspace(ctx.arango_db, ctx.user_id, name)
    return ws.to_dict()


async def dispatch_commit(artifact: dict, body: dict, ctx: Any) -> dict:
    """Commit workspace drafts via the ``commit`` operation."""
    workspace_id = artifact.get("_key") or artifact.get("id")
    artifact_ids = (body or {}).get("artifact_ids")
    result = commit_workspace_to_collections(
        workspace_db=ctx.arango_db,
        collection_db=ctx.arango_db,
        user_id=ctx.user_id,
        workspace_id=workspace_id,
        artifact_ids=artifact_ids,
        dry_run=False,
    )
    return result.to_dict() if hasattr(result, "to_dict") else {"status": "committed"}


async def dispatch_commit_preview(artifact: dict, body: dict, ctx: Any) -> dict:
    """Dry-run commit preview via the ``commit_preview`` operation."""
    workspace_id = artifact.get("_key") or artifact.get("id")
    artifact_ids = (body or {}).get("artifact_ids")
    result = commit_workspace_to_collections(
        workspace_db=ctx.arango_db,
        collection_db=ctx.arango_db,
        user_id=ctx.user_id,
        workspace_id=workspace_id,
        artifact_ids=artifact_ids,
        dry_run=True,
    )
    return result.to_dict() if hasattr(result, "to_dict") else {"status": "preview"}


async def dispatch_revert(artifact: dict, body: dict, ctx: Any) -> dict:
    """Revert an artifact via the ``revert`` operation."""
    workspace_id = artifact.get("collection_id") or artifact.get("_key") or ""
    artifact_id = (body or {}).get("artifact_id") or artifact.get("_key") or ""
    result = revert_artifact(
        workspace_db=ctx.arango_db,
        collection_db=ctx.arango_db,
        user_id=ctx.user_id,
        workspace_id=workspace_id,
        artifact_id=artifact_id,
    )
    if result:
        return result.to_dict()
    return {"status": "reverted"}
