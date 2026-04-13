# routers/artifacts_router.py
#
# Unified Artifact API — single REST surface for all artifact operations.
#
# Replaces per-container endpoints (workspaces, collections, agents, inbound,
# search) with a container-agnostic set of verbs:
#
#   POST   /artifacts                → Create
#   GET    /artifacts/{id}           → Read
#   PATCH  /artifacts/{id}           → Update
#   DELETE /artifacts/{id}           → Delete
#   POST   /artifacts/{id}/invoke    → Invoke (execute an operator)
#   PUT    /artifacts/{container_id} → Add item to a container
#   POST   /artifacts/search         → Search
#
# Specialized endpoints:
#   POST   /artifacts/{id}/upload-initiate     → Initiate S3 upload
#   PATCH  /artifacts/{id}/upload-status       → Update upload progress
#   GET    /artifacts/{id}/multipart-part-url  → Presigned URL for upload part
#   GET    /artifacts/{id}/content-url         → Signed content URL
#   POST   /artifacts/{container_id}/commit    → Commit workspace to collections
#   POST   /artifacts/{container_id}/commit/preview → Preview commit (dry run)
#   POST   /artifacts/{id}/revert              → Revert to last committed version
#   PATCH  /artifacts/{container_id}/order      → Reorder workspace artifacts
#   POST   /artifacts/{id}/move                → Move artifact between workspaces
#   POST   /artifacts/batch                    → Batch fetch by IDs
#   GET    /artifacts/{container_id}/commits    → List commits for collection
#
# Real-time event subscription is handled by the unified /events WebSocket
# (see routers/events_router.py), not a per-container SSE endpoint.

import json
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional

from arango.database import StandardDatabase
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
from pydantic.functional_serializers import SerializerFunctionWrapHandler

from core.dependencies import get_arango_db
from entities.collection import WORKSPACE_CONTENT_TYPE, COLLECTION_CONTENT_TYPE
from services.dependencies import (
    get_auth,
    AuthContext,
    check_access,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Request / Response Models
# =============================================================================

class CreateArtifactRequest(BaseModel):
    """Create a new artifact inside a container."""
    container_id: str
    source_artifact_id: Optional[str] = None  # copy content/context from this artifact
    slug: Optional[str] = None
    context: Optional[str] = None       # JSON string
    content: Optional[str] = None
    content_type: Optional[str] = None


class CreateContainerRequest(BaseModel):
    """Create a new container artifact (workspace or collection).

    The container type is determined entirely by ``content_type``:
      - ``application/vnd.agience.workspace+json`` → workspace
      - ``application/vnd.agience.collection+json`` → collection
    """
    content_type: str
    name: Optional[str] = None
    description: Optional[str] = ""


class UpdateArtifactRequest(BaseModel):
    """Partial update to an artifact or container."""
    context: Optional[str] = None
    content: Optional[str] = None
    state: Optional[str] = None
    content_type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None


class InvokeArtifactRequest(BaseModel):
    """Invoke an operator artifact."""
    name: Optional[str] = None              # tool name (for mcp_tool dispatch via $.body.name)
    arguments: Optional[Dict[str, Any]] = None  # tool arguments (for mcp_tool dispatch)
    workspace_id: Optional[str] = None
    artifacts: Optional[List[str]] = None   # context artifact IDs
    input: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class AddItemRequest(BaseModel):
    """Add an item (artifact) to a container."""
    context: Optional[str] = None
    content: Optional[str] = None
    content_type: Optional[str] = None


class RemoveItemRequest(BaseModel):
    """Remove an item (artifact root/current version) from a workspace container."""
    container_id: str


class ArtifactSearchRequest(BaseModel):
    """Search across accessible artifacts."""
    model_config = ConfigDict(populate_by_name=True)

    query_text: str
    scope: Optional[List[str]] = None           # container IDs to restrict
    content_types: Optional[List[str]] = None
    use_hybrid: Optional[bool] = None
    aperture: Optional[float] = None
    from_: int = 0
    size: int = 20
    sort: Optional[Literal["relevance", "recency"]] = None
    highlight: bool = True

    @model_validator(mode="before")
    @classmethod
    def _accept_from_alias(cls, data):
        if isinstance(data, dict) and "from" in data and "from_" not in data:
            data = dict(data)
            data["from_"] = data.pop("from")
        return data


class SearchHitResponse(BaseModel):
    """Search hit with content fields for downstream consumers."""
    id: str
    score: float
    root_id: str
    version_id: str
    collection_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    highlights: Optional[Dict[str, List[str]]] = None


class ArtifactSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    hits: List[SearchHitResponse]
    total: int
    query_text: str
    parsed_query: Optional[str] = None
    corrections: List[str] = Field(default_factory=list)
    used_hybrid: bool
    from_: int = 0
    size: int

    @model_validator(mode="before")
    @classmethod
    def _accept_from_alias(cls, data):
        if isinstance(data, dict) and "from" in data and "from_" not in data:
            data = dict(data)
            data["from_"] = data.pop("from")
        return data

    @model_serializer(mode="wrap")
    def _emit_from_alias(self, handler: SerializerFunctionWrapHandler):
        data = handler(self)
        if isinstance(data, dict) and "from_" in data and "from" not in data:
            data["from"] = data.pop("from_")
        return data


# =============================================================================
# Helpers
# =============================================================================

# Unified artifact store: containers and artifacts both live in `artifacts`.
_COLL_ARTIFACTS = "artifacts"

def _is_collection(db: StandardDatabase, container_id: str) -> bool:
    """Return True if container_id refers to a container artifact (workspace or collection)."""
    try:
        coll = db.collection(_COLL_ARTIFACTS)
        doc = coll.get(container_id)
        return bool(doc and doc.get("content_type") in (WORKSPACE_CONTENT_TYPE, COLLECTION_CONTENT_TYPE))
    except Exception:
        return False


def _find_artifact(db: StandardDatabase, artifact_id: str) -> Optional[dict]:
    """Locate an artifact in the unified store.

    First tries exact _key lookup; if not found, resolves by root_id (operation
    routes commonly receive root_id values for built-in server artifacts).
    Archived artifacts return None.
    """
    try:
        coll = db.collection(_COLL_ARTIFACTS)
        doc = coll.get(artifact_id)
        if doc and doc.get("state") != "archived":
            return doc
    except Exception:
        pass

    # Resolve stable root IDs to the newest non-archived version row.
    try:
        cursor = db.aql.execute(
            """
            FOR a IN @@col
              FILTER a.root_id == @root_id
                AND a.state != "archived"
              SORT a.modified_time DESC
              LIMIT 1
              RETURN a
            """,
            bind_vars={"@col": _COLL_ARTIFACTS, "root_id": artifact_id},
        )
        doc = next(iter(cursor), None)
        if doc:
            return doc
    except Exception:
        pass

    return None


def _normalize_artifact_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure artifact-like response shape for both regular and container docs.

    Workspaces and collections are container artifacts. Defaults ``context``/``content``
    when absent so frontend card rendering always receives a canonical artifact payload.
    """
    normalized = dict(doc)

    artifact_id = normalized.get("id") or normalized.get("_key")
    if artifact_id and not normalized.get("root_id"):
        normalized["root_id"] = artifact_id

    content_type = normalized.get("content_type")
    is_container = isinstance(content_type, str) and content_type.startswith("application/vnd.agience.")

    if normalized.get("context") is None:
        if is_container:
            normalized["context"] = json.dumps(
                {
                    "title": normalized.get("name") or "",
                    "description": normalized.get("description") or "",
                    "content_type": content_type,
                }
            )
        else:
            normalized["context"] = ""

    if normalized.get("content") is None:
        normalized["content"] = normalized.get("description") or ""

    return normalized


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _resolve_builtin_server_artifact_id(
    arango_db: StandardDatabase,
    artifact_id: str,
) -> str:
    """Resolve a built-in persona slug (e.g. ``nexus``) to artifact UUID.

    This is a startup invariant: platform topology must be pre-resolved before
    request handling begins. If the registry is missing, raise an explicit
    error instead of silently falling back to unresolved IDs.
    """
    _ = arango_db  # Signature kept for call-site symmetry.
    from services import server_registry

    if artifact_id not in server_registry.all_names():
        return artifact_id

    return server_registry.resolve_name_to_id(artifact_id)


# =============================================================================
# Router
# =============================================================================

router = APIRouter(prefix="/artifacts", tags=["Artifacts"])


# ---------- GET /artifacts/containers — List workspaces and/or collections ----------

@router.get("/containers")
async def list_containers(
    type: Optional[str] = Query(None, description="Filter by 'workspace' or 'collection'. Omit for both."),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """List containers (workspaces and/or collections) accessible to the user."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    results: List[Dict[str, Any]] = []

    from db import arango as arango_db_module
    from entities.collection import WORKSPACE_CONTENT_TYPE

    if type == "workspace":
        for c in arango_db_module.get_collections_by_owner_and_type(
            arango_db, auth.user_id, WORKSPACE_CONTENT_TYPE
        ):
            d = c.to_dict()
            d["_container_type"] = "workspace"
            results.append(d)
    elif type == "collection":
        import services.collection_service as collection_svc
        for c in collection_svc.get_collections_for_user(arango_db, auth.user_id, auth.bearer_grant):
            d = c.to_dict()
            d["_container_type"] = "collection" if c.content_type != WORKSPACE_CONTENT_TYPE else "workspace"
            results.append(d)
    else:
        # Both: every collection the user can see, tagged by content_type.
        import services.collection_service as collection_svc
        for c in collection_svc.get_collections_for_user(arango_db, auth.user_id, auth.bearer_grant):
            d = c.to_dict()
            d["_container_type"] = "workspace" if c.content_type == WORKSPACE_CONTENT_TYPE else "collection"
            results.append(d)

    return results


# ---------- GET /artifacts/list — List artifacts in a container ----------

@router.get("/list")
async def list_container_artifacts(
    container_id: str = Query(..., description="The container (workspace or collection) to list artifacts from."),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """List all artifacts within a container (workspace or collection)."""
    check_access(auth, container_id, "read", arango_db)

    if not _is_collection(arango_db, container_id):
        raise HTTPException(status_code=404, detail="Container not found")

    from db import arango as arango_db_module
    from entities.artifact import Artifact as ArtifactEntity
    from services.collection_service import attach_committed_collection_ids
    raw_items = arango_db_module.list_collection_artifacts(arango_db, container_id)
    entities = [ArtifactEntity.from_dict(r) for r in raw_items]
    attach_committed_collection_ids(arango_db, entities)
    items = []
    for raw, entity in zip(raw_items, entities):
        d = dict(raw)
        d["committed_collection_ids"] = getattr(entity, "committed_collection_ids", [])
        items.append(d)
    return {"items": items}


# ---------- POST /artifacts — Create ----------

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_artifact(
    body: Dict[str, Any] = Body(...),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Create a new artifact or workspace container."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    # Dispatch: container creation (workspace/collection) or artifact creation.
    # Container type is determined by content_type — no flag-based branching.
    ct = body.get("content_type", "")
    if ct == WORKSPACE_CONTENT_TYPE:
        container_body = CreateContainerRequest(**body)
        from services.workspace_service import create_workspace
        entity = create_workspace(
            db=arango_db,
            user_id=auth.user_id,
            name=container_body.name or "Untitled",
        )
        result = entity.to_dict()
        result["_container_type"] = "workspace"
        return result

    if ct == COLLECTION_CONTENT_TYPE:
        container_body = CreateContainerRequest(**body)
        from services.collection_service import create_new_collection
        entity = create_new_collection(
            db=arango_db,
            owner_id=auth.user_id,
            name=container_body.name or "Untitled Collection",
            description=container_body.description or "",
        )
        result = entity.to_dict()
        result["_container_type"] = "collection"
        return result

    body = CreateArtifactRequest(**body)

    check_access(auth, body.container_id, "create", arango_db)

    if not _is_collection(arango_db, body.container_id):
        raise HTTPException(status_code=404, detail="Container not found")

    # If source_artifact_id is provided, LINK the existing artifact into the
    # target container (add an edge) instead of creating a duplicate.
    if body.source_artifact_id:
        from db.arango import get_artifact as _get_artifact, get_latest_committed_artifact, add_artifact_to_collection
        source = _get_artifact(arango_db, body.source_artifact_id)
        if not source:
            source = get_latest_committed_artifact(arango_db, body.source_artifact_id)
        if not source:
            raise HTTPException(status_code=404, detail="Source artifact not found")
        root_id = source.root_id or source.id
        add_artifact_to_collection(arango_db, body.container_id, root_id)
        return source.to_dict()

    # Build context dict — merge content_type into context if provided.
    context_str = body.context
    if body.content_type and context_str:
        import json
        try:
            ctx = json.loads(context_str)
            ctx.setdefault("content_type", body.content_type)
            context_str = json.dumps(ctx)
        except (json.JSONDecodeError, TypeError):
            pass
    elif body.content_type and not context_str:
        import json
        context_str = json.dumps({"content_type": body.content_type})

    from services import workspace_service

    entity = workspace_service.create_workspace_artifact(
        db=arango_db,
        user_id=auth.user_id,
        workspace_id=body.container_id,
        context=context_str or "",
        content=body.content or "",
        slug=body.slug,
        content_type=body.content_type,
    )
    return entity.to_dict()


# ---------- GET /artifacts/{artifact_id} — Read ----------

@router.get("/{artifact_id}")
async def read_artifact(
    artifact_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Read a single artifact by ID."""
    check_access(auth, artifact_id, "read", arango_db)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    # Normalize ArangoDB internal keys.
    doc.pop("_id", None)
    doc.pop("_rev", None)
    if "_key" in doc:
        doc.setdefault("id", doc.pop("_key"))

    return doc


# ---------- PATCH /artifacts/{artifact_id} — Update ----------

@router.patch("/{artifact_id}")
async def update_artifact(
    artifact_id: str,
    body: UpdateArtifactRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Partially update an artifact or container."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, artifact_id, "update", arango_db)

    # Check if the target is a container (workspace/collection) first.
    if _is_collection(arango_db, artifact_id):
        from services.workspace_service import update_workspace
        updated = update_workspace(
            arango_db,
            auth.user_id,
            artifact_id,
            name=body.name,
            description=body.description,
        )
        result = updated.to_dict()
        result["_container_type"] = "workspace" if updated.content_type == WORKSPACE_CONTENT_TYPE else "collection"
        return result

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    from services import workspace_service
    container_id = doc.get("collection_id")
    if not container_id:
        raise HTTPException(status_code=500, detail="Artifact missing collection_id")

    updated = workspace_service.update_artifact(
        arango_db,
        auth.user_id,
        container_id,
        artifact_id,
        context=body.context,
        content=body.content,
        state=body.state,
        content_type=body.content_type,
    )
    return updated.to_dict()


# ---------- DELETE /artifacts/{artifact_id} ----------

@router.delete("/{artifact_id}", status_code=status.HTTP_200_OK)
async def delete_artifact(
    artifact_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Delete or archive an artifact."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, artifact_id, "delete", arango_db)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    from services import workspace_service
    container_id = doc.get("collection_id")
    if not container_id:
        raise HTTPException(status_code=500, detail="Artifact missing collection_id")

    workspace_service.delete_artifact(arango_db, auth.user_id, container_id, artifact_id)
    return {"id": artifact_id, "deleted": True}


@router.post("/{artifact_id}/remove", status_code=status.HTTP_200_OK)
async def remove_artifact_from_workspace(
    artifact_id: str,
    body: RemoveItemRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Remove an artifact from a workspace without hard-deleting the root."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, body.container_id, "update", arango_db)

    from services import workspace_service

    artifact = workspace_service.remove_artifact_from_workspace(
        arango_db,
        auth.user_id,
        body.container_id,
        artifact_id,
    )
    return {"id": artifact.id, "removed": True, "container_id": body.container_id}


# ---------- POST /artifacts/{artifact_id}/invoke — Invoke ----------

@router.post("/{artifact_id}/invoke")
async def invoke_artifact(
    artifact_id: str,
    body: InvokeArtifactRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Invoke an operator artifact.

    Dispatches through `operation_dispatcher.dispatch("invoke", ...)` which
    reads `operations.invoke` from the artifact type's `type.json`, runs the
    declared handler, and emits `artifact.invoke.{started,completed,failed}`
    events through the operation emit envelope.

    The artifact's type MUST declare `operations.invoke`. Types without a
    declared invoke operation receive 404 "operation not declared" —
    invocation is a per-type contract, not a platform default.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    # Resolve builtin server slug (e.g. "aria") to its DB artifact id.
    artifact_id = _resolve_builtin_server_artifact_id(arango_db, artifact_id)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    # Build merged params for handlers that consume them.
    merged_params: Dict[str, Any] = dict(body.params or {})
    if body.workspace_id and "workspace_id" not in merged_params:
        merged_params["workspace_id"] = body.workspace_id
    if body.artifacts and "artifacts" not in merged_params:
        merged_params["artifacts"] = body.artifacts
    merged_params["transform_id"] = artifact_id

    from services import operation_dispatcher
    from services.operation_dispatcher import (
        DispatchContext,
        OperationNotDeclared,
    )

    dispatch_body: Dict[str, Any] = {
        "name": body.name,
        "workspace_id": body.workspace_id,
        "artifacts": body.artifacts or [],
        "input": body.input or "",
        "params": merged_params,
        "arguments": body.arguments or merged_params,
    }
    # Load the effective grant for the dispatcher's requires_grant check.
    # For user JWT principals, grants are not pre-loaded in the auth context.
    # Platform server artifacts inherit read access from the all-servers collection
    # grant seeded at first login; use the same pattern as /op/{op_name}.
    effective_grants = list(getattr(auth, "grants", []) or [])
    if auth.user_id and not effective_grants:
        doc_key = doc.get("_key", artifact_id)
        try:
            grant = check_access(auth, doc_key, "read", arango_db)
            effective_grants = [grant]
        except HTTPException:
            pass  # Dispatcher will reject with OperationForbidden if required
    dispatch_ctx = DispatchContext(
        user_id=auth.user_id,
        actor_id=auth.user_id,
        grants=effective_grants,
        arango_db=arango_db,
    )

    try:
        return await operation_dispatcher.dispatch(
            "invoke", doc, dispatch_body, dispatch_ctx
        )
    except OperationNotDeclared as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------- POST /artifacts/{artifact_id}/op/{op_name} — Custom operation ----------

@router.post("/{artifact_id}/op/{op_name}")
async def run_artifact_operation(
    artifact_id: str,
    op_name: str,
    body: Dict[str, Any] = Body(default_factory=dict),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Generic custom-operation endpoint — Phase 4, Enterprise Eventing refactor.

    Dispatches any operation declared in the artifact's `type.json`
    `operations.{op_name}` block through `operation_dispatcher.dispatch`.

    Reserved op names (`create`, `read`, `update`, `delete`, `invoke`, `add`,
    `search`) are handled by their dedicated routes above and are **not**
    accepted here — this endpoint is for custom operations like `rotate`,
    `fetch`, `claim`, `publish_jwk`, `share`, etc.

    The dispatcher enforces the grant check (`requires_grant`), runs the
    declared handler (`dispatch.kind`), and emits every event in
    `operations.{op_name}.emits` around the call.
    """
    if not auth.user_id and auth.principal_type not in ("server", "mcp_client", "api_key"):
        raise HTTPException(status_code=401, detail="Authenticated principal required")

    reserved = {"create", "read", "update", "delete", "invoke", "add", "search"}
    if op_name in reserved:
        raise HTTPException(
            status_code=400,
            detail=f"Operation '{op_name}' is handled by a dedicated route; use that instead",
        )

    # Resolve builtin server slug (e.g. "nexus") to its registered artifact UUID.
    artifact_id = _resolve_builtin_server_artifact_id(arango_db, artifact_id)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    from services import operation_dispatcher
    from services.operation_dispatcher import (
        DispatchContext,
        OperationNotDeclared,
    )

    # For user JWT principals, grants are not pre-loaded in the auth context
    # (resolve_auth skips the DB call for performance). Load the effective grant
    # now so the dispatcher's grant check can find it via owner/direct/container
    # inheritance (platform server artifacts inherit read from the all-servers
    # collection grant seeded at first login).
    effective_grants = list(getattr(auth, "grants", []) or [])
    if auth.user_id and not effective_grants:
        from services.dependencies import check_access as _check_access
        # Use the actual _key from the resolved doc. artifact_id may be a
        # root_id (stable UUID) that differs from the versioned doc's _key;
        # check_access does a direct key lookup and would 404 on root_id.
        doc_key = doc.get("_key", artifact_id)
        try:
            grant = _check_access(auth, doc_key, "read", arango_db)
            effective_grants = [grant]
        except HTTPException:
            pass  # Dispatcher will reject with OperationForbidden if required

    dispatch_ctx = DispatchContext(
        user_id=auth.user_id,
        actor_id=auth.user_id or getattr(auth, "principal_id", None),
        grants=effective_grants,
        arango_db=arango_db,
    )

    try:
        return await operation_dispatcher.dispatch(op_name, doc, body, dispatch_ctx)
    except OperationNotDeclared as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------- PUT /artifacts/{container_id} — Add item to container ----------

@router.put("/{container_id}", status_code=status.HTTP_201_CREATED)
async def add_item_to_container(
    container_id: str,
    body: AddItemRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Add a new artifact into an existing container."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, container_id, "add", arango_db)

    artifact_id = str(uuid.uuid4())
    now_iso = _now_iso()

    # Merge content_type into context if provided.
    context_str = body.context
    if body.content_type:
        import json
        try:
            ctx = json.loads(context_str) if context_str else {}
            ctx.setdefault("content_type", body.content_type)
            context_str = json.dumps(ctx)
        except (json.JSONDecodeError, TypeError):
            if not context_str:
                context_str = json.dumps({"content_type": body.content_type})

    if not _is_collection(arango_db, container_id):
        raise HTTPException(status_code=404, detail="Container not found")

    from db.arango import create_artifact as create_collection_artifact, add_artifact_to_collection
    from entities.artifact import Artifact as ArtifactEntity

    entity = ArtifactEntity(
        id=artifact_id,
        root_id=artifact_id,
        collection_id=container_id,
        context=context_str,
        content=body.content,
        content_type=body.content_type,
        created_by=auth.user_id,
        created_time=now_iso,
        modified_by=auth.user_id,
        modified_time=now_iso,
    )
    result = create_collection_artifact(arango_db, entity)
    add_artifact_to_collection(arango_db, container_id, entity.root_id)
    return result.to_dict()


# ---------- POST /artifacts/search — Search ----------

@router.post("/search", response_model=ArtifactSearchResponse)
async def search_artifacts(
    body: ArtifactSearchRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Search across accessible artifacts.

    Supports the same query syntax as the legacy ``POST /search`` endpoint:
    +term (AND), !term (exclude), ~term (semantic), ="phrase" (exact),
    field:value filters, and @hybrid:on/off control.

    Scope can be narrowed with ``scope`` (list of container IDs).
    """
    user_id = auth.user_id
    bearer_grant = auth.bearer_grant
    api_key_grants = auth.grants if auth.principal_type == "api_key" else []

    if not user_id and not bearer_grant:
        raise HTTPException(status_code=401, detail="Missing authorization")

    if not body.query_text or not body.query_text.strip():
        raise HTTPException(status_code=400, detail="query_text is required")

    # Resolve scoped container IDs into collection_ids.
    # A workspace IS a collection — no distinction needed.
    collection_ids: Optional[List[str]] = None

    if body.scope:
        col_ids = [cid for cid in body.scope if _is_collection(arango_db, cid)]
        collection_ids = col_ids or None

    # If no explicit collection scope, expand to all accessible collections.
    if not collection_ids:
        import services.collection_service as collection_svc

        computed: List[str] = []
        if user_id:
            accessible = collection_svc.get_collections_for_user(arango_db, user_id, bearer_grant)
            computed.extend([c.id for c in accessible])

            for g in api_key_grants:
                if g.resource_type == "collection" and getattr(g, "can_read", False) and g.resource_id:
                    computed.append(g.resource_id)

        elif bearer_grant and bearer_grant.resource_type == "collection" and getattr(bearer_grant, "can_read", False):
            computed.append(bearer_grant.resource_id)

        collection_ids = list(dict.fromkeys([c for c in computed if c])) or None

    # Build and execute search query.
    from search.accessor.search_accessor import SearchAccessor, SearchQuery

    query = SearchQuery(
        query_text=body.query_text,
        user_id=user_id or "",
        grant_keys=None,
        collection_ids=collection_ids,
        use_hybrid=body.use_hybrid,
        aperture=body.aperture if body.aperture is not None else 0.75,
        from_=body.from_,
        size=body.size,
        sort=body.sort or "relevance",
        highlight=body.highlight,
    )

    accessor = SearchAccessor()
    try:
        result = accessor.search(query)
    except Exception as e:
        logger.error("Artifact search error: %s", e)
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

    return ArtifactSearchResponse(
        hits=[
            SearchHitResponse(
                id=hit.doc_id,
                score=hit.score,
                root_id=hit.root_id,
                version_id=hit.version_id,
                collection_id=hit.collection_id,
                title=hit.title or None,
                description=hit.description or None,
                content=(hit.content or "")[:500] or None,
                tags=hit.tags or None,
                highlights=hit.highlights,
            )
            for hit in result.hits
        ],
        total=result.total,
        query_text=query.query_text,
        parsed_query=str(result.parsed_query),
        corrections=result.corrections,
        used_hybrid=result.used_hybrid,
        **{"from": body.from_},
        size=body.size,
    )


# =============================================================================
# Specialized Request Models (defined early so static-path endpoints can use them)
# =============================================================================

class UploadInitiateRequest(BaseModel):
    """Initiate an S3 upload for an artifact."""
    filename: str
    content_type: str
    size: int
    order_key: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


class UploadStatusRequest(BaseModel):
    """Update upload progress/completion."""
    status: Optional[str] = None
    progress: Optional[float] = None
    parts: Optional[List[Dict[str, Any]]] = None
    context_patch: Optional[Dict[str, Any]] = None


class CommitRequest(BaseModel):
    """Commit workspace artifacts to collections."""
    artifact_ids: Optional[List[str]] = None
    dry_run: bool = False
    commit_token: Optional[str] = None


class CommitPreviewRequest(BaseModel):
    """Preview a commit (dry run)."""
    artifact_ids: Optional[List[str]] = None


class ReorderRequest(BaseModel):
    """Reorder artifacts in a workspace."""
    ordered_ids: List[str]
    order_version: Optional[int] = None


class MoveArtifactRequest(BaseModel):
    """Move an artifact to a different workspace."""
    target_container_id: str


class BatchFetchRequest(BaseModel):
    """Batch fetch artifacts by IDs."""
    artifact_ids: List[str]


# =============================================================================
# Batch Operations (static path — registered before /{id} sub-paths)
# =============================================================================

# ---------- POST /artifacts/batch ----------

@router.post("/batch")
async def batch_fetch_artifacts(
    body: BatchFetchRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Batch fetch artifacts by IDs across all containers."""
    if not auth.user_id and not auth.bearer_grant:
        raise HTTPException(status_code=401, detail="Missing authorization")

    results = []
    for aid in body.artifact_ids:
        doc = _find_artifact(arango_db, aid)
        if not doc:
            continue

        # Verify read access silently — skip inaccessible artifacts.
        try:
            check_access(auth, aid, "read", arango_db)
        except HTTPException:
            continue

        normalized = _normalize_artifact_doc(doc)
        normalized.pop("_id", None)
        normalized.pop("_rev", None)
        if "_key" in normalized:
            normalized.setdefault("id", normalized.pop("_key"))
        results.append(normalized)

    return {"artifacts": results}


# =============================================================================
# Upload Endpoints
# =============================================================================

# ---------- POST /artifacts/{artifact_id}/upload-initiate ----------

@router.post("/{artifact_id}/upload-initiate")
async def upload_initiate(
    artifact_id: str,
    body: UploadInitiateRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Initiate an S3 upload for an artifact.

    The artifact_id here is the *container* (workspace) the upload belongs to.
    Delegates to workspace_service.initiate_upload_and_create_artifact().
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, artifact_id, "create", arango_db)

    from services.workspace_service import initiate_upload_and_create_artifact

    try:
        out, artifact = initiate_upload_and_create_artifact(
            db=arango_db,
            user_id=auth.user_id,
            workspace_id=artifact_id,
            filename=body.filename,
            content_type=body.content_type,
            size=body.size,
            order_key=body.order_key,
            context=body.context,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Upload initiate failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload initiation failed: {exc}")

    return {
        **out,
        "artifact": artifact.to_dict() if artifact is not None else None,
    }


# ---------- PATCH /artifacts/{artifact_id}/upload-status ----------

@router.patch("/{artifact_id}/upload-status")
async def upload_status(
    artifact_id: str,
    body: UploadStatusRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Update upload progress or mark complete/failed."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, artifact_id, "update", arango_db)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    workspace_id = doc.get("collection_id")
    if not workspace_id:
        raise HTTPException(status_code=500, detail="Artifact missing collection_id")


    from services.workspace_service import update_upload_status as svc_update_upload

    try:
        result = svc_update_upload(
            db=arango_db,
            user_id=auth.user_id,
            workspace_id=workspace_id,
            upload_id=artifact_id,
            status_value=body.status or "uploading",
            progress=body.progress,
            parts=body.parts,
            context_patch=body.context_patch,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Upload status update failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload status update failed: {exc}")

    return result.to_dict() if hasattr(result, "to_dict") else result


# ---------- GET /artifacts/{artifact_id}/multipart-part-url ----------

@router.get("/{artifact_id}/multipart-part-url")
async def multipart_part_url(
    artifact_id: str,
    part_number: int = Query(..., ge=1),
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Get a presigned URL for uploading a specific multipart part."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, artifact_id, "update", arango_db)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    # Read upload context from artifact to get s3_key and multipart_id.
    context_raw = doc.get("context")
    ctx: Dict[str, Any] = {}
    if context_raw:
        try:
            ctx = json.loads(context_raw) if isinstance(context_raw, str) else context_raw
        except (json.JSONDecodeError, TypeError):
            pass

    upload = ctx.get("upload", {})
    s3_key = upload.get("s3_key") or ctx.get("content_key")
    multipart_id = upload.get("multipart_id")

    if not s3_key or not multipart_id:
        raise HTTPException(status_code=400, detail="No active multipart upload for this artifact")

    from services.content_service import generate_multipart_part_url as gen_part_url

    try:
        url = gen_part_url(s3_key, multipart_id, part_number)
    except Exception as exc:
        logger.error("Multipart part URL generation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate part URL: {exc}")

    return {"url": url, "part_number": part_number}


# ---------- GET /artifacts/{artifact_id}/content-url ----------

@router.get("/{artifact_id}/content-url")
async def content_url(
    artifact_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Get a signed content URL for an artifact's stored content."""
    check_access(auth, artifact_id, "read", arango_db)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    context_raw = doc.get("context")
    ctx: Dict[str, Any] = {}
    if context_raw:
        try:
            ctx = json.loads(context_raw) if isinstance(context_raw, str) else context_raw
        except (json.JSONDecodeError, TypeError):
            pass

    content_key = ctx.get("content_key")
    if not content_key:
        raise HTTPException(status_code=404, detail="No downloadable content for this artifact")

    filename = ctx.get("filename")
    content_type = ctx.get("content_type")

    from services.content_service import generate_signed_url

    try:
        url = generate_signed_url(content_key, filename=filename, content_type=content_type)
    except Exception as exc:
        logger.error("Content URL generation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate content URL: {exc}")

    return {"url": url}


# =============================================================================
# Commit / Revert Endpoints
# =============================================================================

# ---------- POST /artifacts/{container_id}/commit ----------

@router.post("/{container_id}/commit")
async def commit_artifacts(
    container_id: str,
    body: CommitRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Commit workspace artifacts to their target collections."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, container_id, "update", arango_db)

    container = arango_db.collection(_COLL_ARTIFACTS).get(container_id)
    if not container or container.get("content_type") != WORKSPACE_CONTENT_TYPE:
        raise HTTPException(status_code=400, detail="Commit only supported on workspaces")

    from services.workspace_service import commit_workspace_to_collections

    try:
        result = commit_workspace_to_collections(
            workspace_db=arango_db,
            collection_db=arango_db,
            user_id=auth.user_id,
            workspace_id=container_id,
            api_key=auth.api_key_entity,
            artifact_ids=body.artifact_ids,
            dry_run=body.dry_run,
            commit_token=body.commit_token,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Commit failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Commit failed: {exc}")

    return result


# ---------- POST /artifacts/{container_id}/commit/preview ----------

@router.post("/{container_id}/commit/preview")
async def commit_preview(
    container_id: str,
    body: CommitPreviewRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Preview a commit (dry run) — shows what would change without applying."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, container_id, "read", arango_db)

    container = arango_db.collection(_COLL_ARTIFACTS).get(container_id)
    if not container or container.get("content_type") != WORKSPACE_CONTENT_TYPE:
        raise HTTPException(status_code=400, detail="Commit preview only supported on workspaces")

    from services.workspace_service import commit_workspace_to_collections

    try:
        result = commit_workspace_to_collections(
            workspace_db=arango_db,
            collection_db=arango_db,
            user_id=auth.user_id,
            workspace_id=container_id,
            api_key=auth.api_key_entity,
            artifact_ids=body.artifact_ids,
            dry_run=True,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Commit preview failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Commit preview failed: {exc}")

    return result


# ---------- POST /artifacts/{artifact_id}/revert ----------

@router.post("/{artifact_id}/revert")
async def revert_artifact(
    artifact_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Revert a workspace artifact to its last committed version."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, artifact_id, "update", arango_db)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    workspace_id = doc.get("collection_id")
    if not workspace_id:
        raise HTTPException(status_code=500, detail="Artifact missing collection_id")

    from services.workspace_service import revert_artifact as svc_revert

    try:
        result = svc_revert(
            workspace_db=arango_db,
            collection_db=arango_db,
            user_id=auth.user_id,
            workspace_id=workspace_id,
            artifact_id=artifact_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Revert failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Revert failed: {exc}")

    if not result:
        raise HTTPException(status_code=400, detail="Artifact is not revertable (not modified or never committed)")

    return result.to_dict()


# =============================================================================
# Ordering / Move Endpoints
# =============================================================================

# ---------- PATCH /artifacts/{container_id}/order ----------

@router.patch("/{container_id}/order")
async def reorder_artifacts(
    container_id: str,
    body: ReorderRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Reorder artifacts in a workspace container."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, container_id, "update", arango_db)

    container = arango_db.collection(_COLL_ARTIFACTS).get(container_id)
    if not container or container.get("content_type") != WORKSPACE_CONTENT_TYPE:
        raise HTTPException(status_code=400, detail="Ordering only supported on workspaces")

    from services.workspace_service import order_workspace_artifacts

    try:
        new_version = order_workspace_artifacts(
            db=arango_db,
            user_id=auth.user_id,
            workspace_id=container_id,
            ordered_ids=body.ordered_ids,
            expected_version=body.order_version,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Reorder failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Reorder failed: {exc}")

    return {"order_version": new_version}


# ---------- POST /artifacts/{artifact_id}/move ----------

@router.post("/{artifact_id}/move")
async def move_artifact(
    artifact_id: str,
    body: MoveArtifactRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Move an artifact from its current workspace to another."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    check_access(auth, artifact_id, "update", arango_db)

    doc = _find_artifact(arango_db, artifact_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    source_workspace_id = doc.get("collection_id")
    if not source_workspace_id:
        raise HTTPException(status_code=500, detail="Artifact missing collection_id")

    from services.workspace_service import move_artifact_between_workspaces

    try:
        result = move_artifact_between_workspaces(
            db=arango_db,
            user_id=auth.user_id,
            source_workspace_id=source_workspace_id,
            target_workspace_id=body.target_container_id,
            artifact_id=artifact_id,

        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Move failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Move failed: {exc}")

    return result.to_dict()


# =============================================================================
# Container Metadata
# =============================================================================

# ---------- GET /artifacts/{container_id}/commits ----------

@router.get("/{container_id}/commits")
async def list_commits(
    container_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """List commits for a collection container."""
    check_access(auth, container_id, "read", arango_db)

    if not _is_collection(arango_db, container_id):
        raise HTTPException(status_code=400, detail="Commits only available for collections")

    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    from services.collection_service import get_commits_for_collection

    try:
        commits = get_commits_for_collection(arango_db, auth.user_id, container_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to list commits: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list commits: {exc}")

    return {
        "commits": [
            {
                "id": getattr(c, "id", None),
                "collection_id": getattr(c, "collection_id", container_id),
                "message": getattr(c, "message", None),
                "author_id": getattr(c, "author_id", None),
                "created_time": getattr(c, "created_time", None),
                "adds": getattr(c, "adds", []),
                "removes": getattr(c, "removes", []),
            }
            for c in commits
        ],
    }


# =============================================================================
# Utilities
# =============================================================================

def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
