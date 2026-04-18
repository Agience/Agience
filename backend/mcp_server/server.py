"""
Agience MCP Server  -- tool definitions and Streamable HTTP transport.

This module implements a proper MCP server using the official
modelcontextprotocol/python-sdk.  It exposes Agience's capabilities as
purpose-driven tools (not raw CRUD endpoints) that external MCP clients
(VS Code, Claude Desktop, Cursor, etc.) can discover and invoke.

Architecture
~~~~~~~~~~~~
- FastMCP instance with ``@mcp.tool()`` definitions.
- Mounted on the host FastAPI app at ``/mcp`` via Starlette ``Mount``.
- Auth is resolved per-request through ASGI middleware that validates
    Bearer tokens (JWT or direct API key) and stores the
  authenticated user in a ``contextvars.ContextVar``.
- Tools use the context var to obtain user identity, then create
  short-lived DB sessions to call into the existing service layer.

Tool surface (purpose-driven, not CRUD)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Knowledge & Research:
  search            -- Hybrid search across collections and workspaces.
  get_artifact          -- Retrieve a specific artifact by ID.
  browse_collections  -- List collections, optionally list artifacts within one.
  browse_workspaces   -- List workspaces, optionally list artifacts within one.

Workspace Curation:
  create_artifact       -- Create a new artifact in a workspace.
  update_artifact       -- Update an existing workspace artifact.
  manage_artifact       -- Archive, revert, or delete a workspace artifact.

Analysis & Intelligence:
  extract_information  -- Extract structured information from source artifacts.

Tool Discovery:
  discover_tools       -- Discover available tools across connected MCP servers.

Task Tracking:
  todo_list            -- Manage a task list within a workspace.

Memory (Workspace Bindings):
  memory            -- Read/write named artifacts in a workspace's bound collection.

Communication & Routing:
  ask               -- Ask a question grounded in the knowledge base.
"""


from __future__ import annotations

import contextvars
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from entities.api_key import APIKey as APIKeyEntity
from services.dependencies import AuthContext, resolve_auth
from arango import ArangoClient
from core import config

_repo_root = Path(__file__).resolve().parents[2]
_build_info_path = (
    (_repo_root / os.environ["BUILD_INFO_PATH"])
    if "BUILD_INFO_PATH" in os.environ
    else _repo_root / "build_info.json"
)
_build_info = json.loads(_build_info_path.read_text(encoding="utf-8-sig"))
_app_version: str = _build_info.get("version", "0.0.0")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-request context (set by auth middleware, read by tools)
# ---------------------------------------------------------------------------
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_user_id")
_current_api_key: contextvars.ContextVar[Optional[APIKeyEntity]] = contextvars.ContextVar("mcp_api_key", default=None)
_current_auth_context: contextvars.ContextVar[Optional[AuthContext]] = contextvars.ContextVar("mcp_auth_context", default=None)

# ---------------------------------------------------------------------------
# Transport security -- allow the public hostname alongside localhost
# ---------------------------------------------------------------------------
_parsed = urlparse(config.BACKEND_URI)
_backend_host = _parsed.hostname or "localhost"

_allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
if _backend_host not in ("127.0.0.1", "localhost", "::1"):
    _allowed_hosts.append(f"{_backend_host}:*")
    _allowed_hosts.append(_backend_host)
    _allowed_origins.append(f"https://{_backend_host}")
    _allowed_origins.append(f"https://{_backend_host}:*")
    _allowed_origins.append(f"http://{_backend_host}:*")

# ---------------------------------------------------------------------------
# Client instructions (delivered to MCP clients on initialization)
# ---------------------------------------------------------------------------
_instructions_path = Path(__file__).resolve().parents[2] / ".docs" / "mcp" / "client-instructions.md"
_client_instructions: str | None = None
if _instructions_path.is_file():
    _client_instructions = _instructions_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "Agience",
    instructions=_client_instructions,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)

# FastMCP 1.21.x no longer accepts a top-level ``version=`` constructor
# argument, but the wrapped low-level MCP server still supports it and uses
# that value for the initialization payload returned to clients.
mcp._mcp_server.version = _app_version


# ---------------------------------------------------------------------------
# DB helpers (short-lived sessions per tool call)
# ---------------------------------------------------------------------------
class _ArangoSession:
    """Wraps ArangoClient + StandardDatabase so db.close() delegates to the client."""

    def __init__(self, client: ArangoClient, db):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_db", db)

    def close(self):
        object.__getattribute__(self, "_client").close()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_db"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_db"), name, value)


def _get_arango() -> "_ArangoSession":
    client = ArangoClient(hosts=f"http://{config.ARANGO_HOST}:{config.ARANGO_PORT}")
    db = client.db(
        config.ARANGO_DATABASE,
        username=config.ARANGO_USERNAME,
        password=config.ARANGO_PASSWORD,
    )
    return _ArangoSession(client, db)


def _user_id() -> str:
    return _current_user_id.get()


def _api_key() -> Optional[APIKeyEntity]:
    return _current_api_key.get()


# ===================================================================
# TOOLS  -- Knowledge & Research
# ===================================================================

@mcp.tool()
def search(
    query: str,
    limit: int = 20,
    offset: int = 0,
    collection_ids: Optional[List[str]] = None,
) -> dict:
    """Search Agience knowledge  -- hybrid semantic + keyword search across collections and workspaces.

    Returns ranked results with highlights, scores, and source metadata.
    Supports scoping to specific collections (workspaces are collections).

    Args:
        query: Search query text.
        limit: Maximum number of results to return (default 20).
        offset: Number of results to skip for pagination.
        collection_ids: Optional list of collection/workspace IDs to scope the search.
    """
    from search.accessor.search_accessor import SearchAccessor, SearchQuery

    accessor = SearchAccessor()
    result = accessor.search(
        SearchQuery(
            query_text=query,
            user_id=_user_id(),
            collection_ids=collection_ids,
            from_=offset,
            size=limit,
        )
    )

    hits = []
    for h in getattr(result, "hits", []) or []:
        hits.append({
            "id": getattr(h, "doc_id", None),
            "score": getattr(h, "score", None),
            "root_id": getattr(h, "root_id", None),
            "version_id": getattr(h, "version_id", None),
            "title": getattr(h, "title", None),
            "description": getattr(h, "description", None),
            "content": getattr(h, "content", None),
            "tags": getattr(h, "tags", None),
            "collection_id": getattr(h, "collection_id", None),
            "highlights": getattr(h, "highlights", None),
        })

    return {
        "total": getattr(result, "total", 0),
        "hits": hits,
        "used_hybrid": getattr(result, "used_hybrid", False),
    }


@mcp.tool()
def get_artifact(
    artifact_id: str,
    workspace_id: Optional[str] = None,
    collection_id: Optional[str] = None,
) -> dict:
    """Retrieve a specific artifact by ID.

    Provide workspace_id to fetch from a workspace, or collection_id
    to fetch from a collection.  If neither is given the tool attempts
    both, workspace first.
    """
    from services import workspace_service

    user_id = _user_id()

    # Try workspace first
    if workspace_id or not collection_id:
        db = _get_arango()
        try:
            if workspace_id:
                wids = [workspace_id]
            else:
                wids = [w.id for w in workspace_service.list_workspaces(db, user_id)]
            for wid in wids:
                try:
                    artifact = workspace_service.get_workspace_artifact(db, user_id, wid, artifact_id)
                    return _serialize_workspace_artifact(artifact)
                except Exception:
                    continue
        finally:
            db.close()

    # Try collection
    if collection_id or not workspace_id:
        arango = _get_arango()
        try:
            from db.arango import get_artifact
            artifact = get_artifact(arango, artifact_id)
            if artifact:
                return _serialize_artifact_version(artifact)
        except Exception:
            pass

    return {"error": "Artifact not found", "artifact_id": artifact_id}


@mcp.tool()
def browse_collections(
    collection_id: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Browse collections. Without collection_id lists all accessible collections.
    With collection_id returns that collection's artifacts.
    Optional query filters collections by name/description substring.
    """
    from services import collection_service

    user_id = _user_id()
    arango = _get_arango()

    if collection_id:
        artifacts = collection_service.get_collection_artifacts(
            arango, user_id, collection_id=collection_id,
        )
        items = [_serialize_artifact_version(c) for c in (artifacts or [])]
        return {"collection_id": collection_id, "count": len(items), "artifacts": items}

    collections = collection_service.get_collections_for_user(arango, user_id, grant_key=None)
    results = []
    q = (query or "").strip().lower()
    for c in collections or []:
        if q:
            haystack = f"{getattr(c, 'name', '')} {getattr(c, 'description', '')}".lower()
            if q not in haystack:
                continue
        results.append({
            "id": getattr(c, "id", None),
            "name": getattr(c, "name", None),
            "description": getattr(c, "description", None),
            "artifact_count": len(getattr(c, "artifact_ids", None) or []),
        })
        if len(results) >= limit:
            break
    return {"count": len(results), "collections": results}


@mcp.tool()
def browse_workspaces(
    workspace_id: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Browse workspaces. Without workspace_id lists all accessible workspaces.
    With workspace_id returns that workspace's artifacts.
    """
    from services import workspace_service

    user_id = _user_id()
    db = _get_arango()
    try:
        if workspace_id:
            artifacts = workspace_service.list_workspace_artifacts(db, user_id, workspace_id)
            items = [_serialize_workspace_artifact(c) for c in (artifacts or [])]
            return {"workspace_id": workspace_id, "count": len(items), "artifacts": items}

        workspaces = workspace_service.list_workspaces(db, user_id)
        results = []
        for w in workspaces or []:
            results.append({
                "id": getattr(w, "id", None),
                "name": getattr(w, "name", None),
                "description": getattr(w, "description", "") or "",
            })
            if len(results) >= limit:
                break
        return {"count": len(results), "workspaces": results}
    finally:
        db.close()


# ===================================================================
# TOOLS  -- Workspace Curation
# ===================================================================

@mcp.tool()
def create_artifact(
    content: str = "",
    context: Optional[Dict[str, Any]] = None,
    workspace_id: Optional[str] = None,
    collection_id: Optional[str] = None,
    content_type: Optional[str] = None,
) -> dict:
    """Create a new artifact.

    Provide content (text body) and optional context (JSON metadata).

    content_type: MIME type for the artifact (e.g. "text/markdown", "application/json").
      If provided here, it is also written into context.content_type so viewers
      resolve correctly. If context already contains content_type, this parameter
      takes precedence.

    Destination (exactly one required):
    - workspace_id: stages the artifact as a draft in a workspace (default flow).
    - collection_id: commits the artifact directly to a collection, bypassing
      workspace staging. Access is enforced at the service layer via resource_filters.

    Returns the created artifact.
    """
    if workspace_id and collection_id:
        raise ValueError("Provide either workspace_id or collection_id, not both.")
    if not workspace_id and not collection_id:
        raise ValueError("Either workspace_id or collection_id is required.")

    # Ensure content_type is reflected in context so viewers resolve correctly.
    ctx = context or {}
    if content_type:
        ctx["content_type"] = content_type

    if collection_id:
        # --- Direct-to-collection path ---
        api_key = _api_key()
        from services import collection_service

        arango = _get_arango()
        actor_id = (api_key.name if api_key else None)
        artifact = collection_service.create_and_add_artifact(
            arango,
            _user_id(),
            collection_id,
            context=json.dumps(ctx),
            content=content,
            actor_id=actor_id,
        )
        return {"artifact": artifact.to_dict()}

    else:
        # --- Workspace staging path (original behaviour) ---
        from services import workspace_service

        db = _get_arango()
        try:
            artifact = workspace_service.create_workspace_artifact(
                db,
                _user_id(),
                workspace_id,
                context=json.dumps(ctx),
                content=content,
                content_type=content_type,
            )
            return {"artifact": _serialize_workspace_artifact(artifact)}
        finally:
            db.close()


@mcp.tool()
def update_artifact(
    workspace_id: str,
    artifact_id: str,
    content: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> dict:
    """Update an existing workspace artifact's content and/or context metadata.

    Only fields provided will be modified; omitted fields are unchanged.
    Returns the updated artifact.
    """
    from services import workspace_service

    db = _get_arango()
    try:
        artifact = workspace_service.update_artifact(
            db,
            _user_id(),
            workspace_id,
            artifact_id,
            context=json.dumps(context) if context is not None else None,
            content=content,
        )
        return {"artifact": _serialize_workspace_artifact(artifact)}
    finally:
        db.close()


@mcp.tool()
def manage_artifact(
    workspace_id: str,
    artifact_id: str,
    action: Literal["archive", "revert", "delete"],
) -> dict:
    """Manage a workspace artifact  -- archive, revert, or delete it.

    Actions:
      archive  -- Mark the artifact as archived.
      revert   -- Revert modifications back to the committed state.
      delete   -- Permanently remove the artifact from the workspace.
    """
    from services import workspace_service

    user_id = _user_id()
    db = _get_arango()
    arango = _get_arango()
    try:
        if action == "archive":
            artifact = workspace_service.update_artifact(
                db, user_id, workspace_id, artifact_id,
                state="archived",
            )
            return {"action": "archived", "artifact": _serialize_workspace_artifact(artifact)}

        if action == "revert":
            artifact = workspace_service.revert_artifact(db, arango, user_id, workspace_id, artifact_id)
            return {"action": "reverted", "artifact": _serialize_workspace_artifact(artifact)}

        if action == "delete":
            workspace_service.delete_artifact(db, user_id, workspace_id, artifact_id)
            return {"action": "deleted", "artifact_id": artifact_id}

        return {"error": f"Unknown action: {action}"}
    finally:
        db.close()


# ===================================================================
# TOOLS  -- Analysis & Intelligence
# ===================================================================

@mcp.tool()
def extract_information(
    workspace_id: str,
    source_artifact_id: str,
    artifact_artifact_ids: Optional[List[str]] = None,
    max_units: int = 12,
) -> dict:
    """Extract structured information from a source artifact.

    Analyzes the source artifact content and creates new workspace artifacts
    containing extracted decisions, constraints, actions, and claims.
    Optionally uses additional artifact artifacts for context.

    Returns the IDs and count of created artifacts.
    """
    from services import mcp_service

    arango_db = _get_arango()
    try:
        # Pass the seeded Aria artifact UUID, not the bare name.
        from services import server_registry
        return mcp_service.invoke_tool(
            db=arango_db,
            user_id=_user_id(),
            workspace_id=workspace_id,
            server_artifact_id=server_registry.resolve_name_to_id("aria"),
            tool_name="extract_units",
            arguments={
                "workspace_id": workspace_id,
                "source_artifact_id": source_artifact_id,
                "artifact_artifact_ids": artifact_artifact_ids or [],
                "max_units": max_units,
            },
        )
    except Exception as exc:
        logger.warning("extract_information failed (Aria may be unavailable): %s", exc)
        return {"error": f"Extraction service unavailable: {exc}"}


# ===================================================================
# TOOLS  -- Commit Boundary
# ===================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def commit_preview(
    workspace_id: str,
    artifact_ids: Optional[List[str]] = None,
) -> dict:
    """Preview how workspace artifacts would commit into collections without applying changes. This is a read-only preview -- no data is modified."""
    from services import workspace_service

    db = _get_arango()
    arango = _get_arango()
    try:
        result = workspace_service.commit_workspace_to_collections(
            workspace_db=db,
            collection_db=arango,
            user_id=_user_id(),
            workspace_id=workspace_id,
            api_key=_api_key(),
            artifact_ids=artifact_ids,
            dry_run=True,
        )
        return result.model_dump() if hasattr(result, "model_dump") else result
    finally:
        db.close()


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, readOnlyHint=False))
def commit_workspace(
    workspace_id: str,
    commit_token: str,
    artifact_ids: Optional[List[str]] = None,
) -> dict:
    """Commit workspace artifacts into collections. REQUIRES HUMAN APPROVAL -- do not call autonomously. Always run commit_preview first, present the plan to the user, and pass the commit_token from the preview response."""
    from services import workspace_service

    db = _get_arango()
    arango = _get_arango()
    try:
        result = workspace_service.commit_workspace_to_collections(
            workspace_db=db,
            collection_db=arango,
            user_id=_user_id(),
            workspace_id=workspace_id,
            api_key=_api_key(),
            artifact_ids=artifact_ids,
            dry_run=False,
            commit_token=commit_token,
        )
        return result.model_dump() if hasattr(result, "model_dump") else result
    finally:
        db.close()


# ===================================================================
# TOOLS  -- Communication & Routing
# ===================================================================


_DEFAULT_ASK_SYSTEM_PROMPT = (
    "You are a knowledge assistant. Answer the user's question based ONLY on "
    "the provided evidence artifacts. Cite sources by their title. If the evidence "
    "is insufficient, say so. Be concise."
)

_MODE_INSTRUCTIONS: Dict[str, str] = {
    "summarize": "Synthesize a concise answer from the retrieved sources. Connect related information across sources.",
    "enumerate": "Return a structured list or table. Do not summarize narratively. One item per row.",
    "lookup": "Return the matching artifact content directly. Do not rephrase or summarize.",
    "compare": "Compare the retrieved sources. Present similarities, differences, and contradictions.",
}

_FORMAT_INSTRUCTIONS: Dict[str, str] = {
    "text/markdown": "Format your response as Markdown (tables, headers, lists, code blocks as appropriate).",
    "text/mermaid": "Format your entire response as a single Mermaid diagram (graph, sequenceDiagram, erDiagram, etc.). Output ONLY the Mermaid markup, no surrounding prose.",
    "text/csv": "Format your response as CSV with a header row. Output ONLY the CSV data, no surrounding prose.",
    "text/plain": "Format your response as plain unformatted text. No Markdown syntax.",
    "application/json": "Format your response as valid JSON. Output ONLY the JSON, no surrounding prose.",
}

_SUPPORTED_CONTENT_TYPES = frozenset(_FORMAT_INSTRUCTIONS.keys())
_DEFAULT_CONTENT_TYPE = "text/markdown"


@mcp.tool()
def ask(
    question: str,
    workspace_id: Optional[str] = None,
    collection_ids: Optional[List[str]] = None,
    max_sources: int = 5,
    mode: Optional[str] = None,
    accepts: Optional[List[str]] = None,
) -> dict:
    """Ask a question grounded in the Agience knowledge base.

    Searches relevant artifacts, then synthesizes an answer with citations.

    Scoping:
    - Provide ``workspace_id`` to use the workspace's ask binding
      (collection scope, custom system prompt, workspace LLM config).
    - Provide ``collection_ids`` to search specific collections directly.
    - Omit both for a global search with the platform default LLM.

    Mode (answer shape hint):
    - ``summarize`` (default) — concise narrative answer
    - ``enumerate`` — structured list or table, no narrative
    - ``lookup`` — return matching artifact content directly
    - ``compare`` — highlight differences and contradictions

    Accepts (content-type preferences, ordered):
    - ``text/markdown`` (default), ``text/mermaid``, ``text/csv``,
      ``text/plain``, ``application/json``
    - First producible type is used; falls back to ``text/markdown``.

    Workspace binding shape (set in workspace context):
      {"bindings": {"ask": {"collection_id": "...", "system_prompt_id": "..."}}}

    Returns ``content_type``, ``content``, and ``sources``.
    """
    from search.accessor.search_accessor import SearchAccessor, SearchQuery
    from services import workspace_service
    from services import llm_service
    import db.arango as arango_db

    user_id = _user_id()
    db = _get_arango()

    resolved_mode = mode if mode in _MODE_INSTRUCTIONS else "summarize"
    accept_list = accepts or [_DEFAULT_CONTENT_TYPE]

    # Resolve workspace bindings (search scope + system prompt + renderer support)
    system_prompt = _DEFAULT_ASK_SYSTEM_PROMPT
    ws_supported_types: Optional[frozenset] = None
    if workspace_id:
        # Search scope — resolve ask binding's collection_id
        bound_collection = workspace_service.resolve_binding(
            db, user_id, workspace_id, "ask",
        )
        if bound_collection and not collection_ids:
            collection_ids = [bound_collection]

        # System prompt + workspace renderer support
        try:
            ws_context = workspace_service.get_workspace_context(db, user_id, workspace_id)
            ask_binding = (ws_context or {}).get("bindings", {}).get("ask", {})

            # Custom system prompt
            prompt_artifact_id = ask_binding.get("system_prompt_id")
            if prompt_artifact_id:
                prompt_artifact = arango_db.get_artifact(db, prompt_artifact_id)
                if prompt_artifact:
                    custom_prompt = _resolve_content(prompt_artifact)
                    if custom_prompt:
                        system_prompt = custom_prompt

            # Workspace-supported renderers (optional)
            supported = ask_binding.get("supported_types")
            if isinstance(supported, list) and supported:
                ws_supported_types = frozenset(supported)
        except Exception as e:
            logger.warning("ask tool: failed to resolve workspace ask binding: %s", e)

    # Resolve content type — first match in accepts that is both platform-supported
    # and workspace-supported (if workspace declares a list)
    resolved_content_type = _DEFAULT_CONTENT_TYPE
    for ct in accept_list:
        if ct not in _SUPPORTED_CONTENT_TYPES:
            continue
        if ws_supported_types is not None and ct not in ws_supported_types:
            continue
        resolved_content_type = ct
        break

    # Step 1: search for relevant artifacts
    accessor = SearchAccessor()
    result = accessor.search(
        SearchQuery(
            query_text=question,
            user_id=user_id,
            collection_ids=collection_ids,
            size=max_sources,
        )
    )

    sources = []
    context_parts = []
    for h in getattr(result, "hits", []) or []:
        source = {
            "id": getattr(h, "doc_id", None),
            "title": getattr(h, "title", None),
            "score": getattr(h, "score", None),
        }
        sources.append(source)
        content = getattr(h, "content", None) or getattr(h, "description", None) or ""
        title = getattr(h, "title", None) or ""
        if content:
            context_parts.append(f"[{title}]: {content[:2000]}")

    if not context_parts:
        return {
            "content_type": resolved_content_type,
            "content": "I couldn't find any relevant information in the knowledge base for that question.",
            "sources": [],
        }

    # Step 2: build prompt with mode + format instructions
    prompt_parts = [system_prompt]
    mode_instruction = _MODE_INSTRUCTIONS.get(resolved_mode)
    if mode_instruction:
        prompt_parts.append(mode_instruction)
    format_instruction = _FORMAT_INSTRUCTIONS.get(resolved_content_type)
    if format_instruction:
        prompt_parts.append(format_instruction)
    full_system_prompt = "\n\n".join(prompt_parts)

    # Step 3: synthesize answer with LLM
    try:
        evidence = "\n\n---\n\n".join(context_parts)
        messages = [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": f"Evidence:\n{evidence}\n\nQuestion: {question}"},
        ]
        answer, _ = llm_service.complete(
            db, user_id, messages,
            workspace_id=workspace_id,
            temperature=0.3,
            max_output_tokens=1024,
        )
    except Exception as e:
        logger.error("ask tool: LLM call failed: %s", e)
        answer = "Search returned results but I was unable to synthesize an answer. See sources below."

    return {"content_type": resolved_content_type, "content": answer, "sources": sources}


# ===================================================================
# TOOLS  -- Tool Discovery
# ===================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def discover_tools(
    query: Optional[str] = None,
    workspace_id: Optional[str] = None,
    server_name: Optional[str] = None,
) -> dict:
    """Discover available tools across connected MCP servers.

    Returns tool names, descriptions, and which server provides them.
    Optionally filter by keyword query, scope to a workspace, or
    list tools on a specific server.

    When the workspace has a ``tools`` binding, discovery is scoped to
    MCP servers in the bound collection only.  When no binding exists,
    all accessible servers are returned.

    Use this to understand what capabilities are available before
    calling tools by name.
    """
    from services import mcp_service, workspace_service

    user_id = _user_id()
    arango = _get_arango()
    try:
        # If workspace has a tools binding, scope to that collection.
        tools_collection_id = None
        if workspace_id:
            tools_collection_id = workspace_service.resolve_binding(
                arango, user_id, workspace_id, "tools",
            )

        if tools_collection_id:
            servers = mcp_service.list_servers_from_collection(arango, tools_collection_id)
        elif workspace_id:
            servers = mcp_service.list_servers_for_workspace(arango, user_id, workspace_id)
        else:
            servers = mcp_service.list_all_servers_for_user(arango, user_id)

        q = (query or "").strip().lower()
        results = []
        for srv in servers or []:
            srv_name = getattr(srv, "name", None) or getattr(srv, "server_name", None) or ""
            srv_id = getattr(srv, "id", None) or getattr(srv, "server_id", None) or ""

            if server_name and srv_name.lower() != server_name.lower():
                continue

            tools = getattr(srv, "tools", None) or []
            for tool in tools:
                t_name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else "")
                t_desc = getattr(tool, "description", None) or (tool.get("description", "") if isinstance(tool, dict) else "")

                if q and q not in t_name.lower() and q not in t_desc.lower():
                    continue

                results.append({
                    "tool": t_name,
                    "description": t_desc,
                    "server": srv_name,
                    "server_id": srv_id,
                })

        return {"count": len(results), "tools": results}
    except Exception as exc:
        logger.warning("discover_tools failed: %s", exc)
        return {"error": str(exc)}
    finally:
        arango.close()


# ===================================================================
# TOOLS  -- Task Tracking
# ===================================================================

@mcp.tool()
def todo_list(
    workspace_id: str,
    command: Literal["list", "add", "update", "remove"],
    item: Optional[str] = None,
    item_id: Optional[int] = None,
    status: Optional[Literal["not-started", "in-progress", "completed"]] = None,
) -> dict:
    """Manage a todo list for tracking tasks within a workspace.

    The todo list is stored as a workspace artifact with a well-known slug.
    Multiple todo lists can exist in a workspace — each is just an artifact.

    Commands:
      list    -- Return all items in the todo list.
      add     -- Add a new item (provide 'item' text).
      update  -- Update an item's status (provide 'item_id' and 'status').
      remove  -- Remove an item (provide 'item_id').
    """
    from services import workspace_service

    user_id = _user_id()
    db = _get_arango()
    try:
        # Find or create the todo artifact by slug
        TODO_SLUG = "todo-list"
        existing = None
        artifacts = workspace_service.list_workspace_artifacts(db, user_id, workspace_id)
        for a in (artifacts or []):
            ctx = getattr(a, "context", None)
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except Exception:
                    ctx = {}
            if isinstance(ctx, dict) and ctx.get("slug") == TODO_SLUG:
                existing = a
                break

        # Parse current items
        items = []
        if existing:
            content = getattr(existing, "content", None) or "[]"
            try:
                items = json.loads(content)
            except Exception:
                items = []

        if command == "list":
            return {"workspace_id": workspace_id, "items": items, "count": len(items)}

        if command == "add":
            if not item:
                return {"error": "Provide 'item' text to add."}
            next_id = max((i.get("id", 0) for i in items), default=0) + 1
            items.append({"id": next_id, "title": item, "status": "not-started"})

        elif command == "update":
            if item_id is None or not status:
                return {"error": "Provide 'item_id' and 'status' to update."}
            found = False
            for i in items:
                if i.get("id") == item_id:
                    i["status"] = status
                    found = True
                    break
            if not found:
                return {"error": f"Item {item_id} not found."}

        elif command == "remove":
            if item_id is None:
                return {"error": "Provide 'item_id' to remove."}
            items = [i for i in items if i.get("id") != item_id]

        # Save back
        new_content = json.dumps(items)
        todo_context = json.dumps({"slug": TODO_SLUG, "type": "todo_list"})

        if existing:
            workspace_service.update_artifact(
                db, user_id, workspace_id, existing.id,
                content=new_content,
            )
        else:
            workspace_service.create_workspace_artifact(
                db, user_id, workspace_id,
                context=todo_context,
                content=new_content,
            )

        return {"workspace_id": workspace_id, "items": items, "count": len(items), "command": command}
    finally:
        db.close()


@mcp.tool()
def relay_status() -> dict:
    """Return the Desktop Host Relay connection status for the current user.

    Reports whether a Desktop Host Relay session is active and, if so, when
    it was last seen. Use this to check relay connectivity from a viewer or tool.

    Returns a dict with ``connected`` (bool) and, when connected, ``session``
    containing ``session_id``, ``device_id``, ``display_name``, and ``last_seen_at``.
    """
    from services.desktop_host_relay_service import relay_manager

    sessions = relay_manager.list_sessions_for_user(_user_id())
    if not sessions:
        return {"connected": False}
    # Return the most-recently-seen session
    latest = max(sessions, key=lambda s: s.get("last_seen_at") or "")
    return {
        "connected": True,
        "session": {
            "session_id": latest.get("session_id"),
            "device_id": latest.get("device_id"),
            "display_name": latest.get("display_name"),
            "last_seen_at": latest.get("last_seen_at"),
        },
    }


# ===================================================================
# TOOLS  -- Memory (Workspace Bindings)
# ===================================================================

@mcp.tool()
def memory(
    workspace_id: str,
    command: Literal["read", "write", "list", "search", "delete"],
    key: Optional[str] = None,
    content: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
    scope: str = "memory",
) -> dict:
    """Read and write named artifacts in a workspace's bound memory collection.

    The workspace must have a binding for the given scope (default "memory")
    pointing to a collection.  Set bindings in workspace context:
      {"bindings": {"memory": {"collection_id": "<collection-id>"}}}

    Commands:
      read    -- Return the content of the artifact with the given key (slug).
      write   -- Create or update an artifact by key. Provide content and
                 optional context metadata.
      list    -- Return all artifacts in the bound collection.
      search  -- Search within the bound collection (provide query).
      delete  -- Archive the artifact with the given key.
    """
    from services import workspace_service
    import db.arango as arango_db

    user_id = _user_id()
    db = _get_arango()
    try:
        # Resolve binding
        collection_id = workspace_service.resolve_binding(
            db, user_id, workspace_id, scope,
        )
        if not collection_id:
            return {
                "error": f"No '{scope}' binding in workspace {workspace_id}. "
                f"To fix, update the workspace context to include a binding: "
                f'{{"bindings": {{"{scope}": {{"collection_id": "<target-collection-id>"}}}}}}'  
            }

        # -- read --
        if command == "read":
            if not key:
                return {"error": "Provide 'key' for read command."}
            artifact = arango_db.find_artifact_by_slug_in_collection(
                db, collection_id, key,
            )
            if not artifact:
                return {"error": f"No artifact with key '{key}' in bound collection."}
            return {
                "key": key,
                "artifact_id": artifact.id,
                "content": _resolve_content(artifact),
                "context": artifact.context,
            }

        # -- write --
        if command == "write":
            if not key:
                return {"error": "Provide 'key' for write command."}
            existing = arango_db.find_artifact_by_name_in_collection(
                db, collection_id, key,
            )
            ctx = json.dumps(context or {})
            if existing:
                updated = workspace_service.update_artifact(
                    db, user_id, collection_id, existing.id,
                    content=content,
                    context=ctx if context else None,
                )
                return {
                    "action": "updated",
                    "key": key,
                    "artifact_id": updated.id,
                }
            else:
                created = workspace_service.create_workspace_artifact(
                    db, user_id, collection_id,
                    context=ctx,
                    content=content or "",
                    name=key,
                )
                return {
                    "action": "created",
                    "key": key,
                    "artifact_id": created.id,
                }

        # -- list --
        if command == "list":
            rows = arango_db.list_collection_artifacts(db, collection_id)
            items = []
            for r in rows:
                items.append({
                    "id": r.get("id"),
                    "slug": r.get("slug"),
                    "state": r.get("state"),
                    "content_type": r.get("content_type"),
                    "created_time": r.get("created_time"),
                })
            return {
                "collection_id": collection_id,
                "count": len(items),
                "items": items,
            }

        # -- search --
        if command == "search":
            if not query:
                return {"error": "Provide 'query' for search command."}
            from search.accessor.search_accessor import SearchAccessor, SearchQuery

            accessor = SearchAccessor()
            result = accessor.search(
                SearchQuery(
                    query_text=query,
                    user_id=user_id,
                    collection_ids=[collection_id],
                    size=20,
                )
            )
            hits = []
            for h in getattr(result, "hits", []) or []:
                hits.append({
                    "id": getattr(h, "doc_id", None),
                    "title": getattr(h, "title", None),
                    "score": getattr(h, "score", None),
                    "content": getattr(h, "content", None),
                })
            return {
                "collection_id": collection_id,
                "total": getattr(result, "total", 0),
                "hits": hits,
            }

        # -- delete --
        if command == "delete":
            if not key:
                return {"error": "Provide 'key' for delete command."}
            artifact = arango_db.find_artifact_by_name_in_collection(
                db, collection_id, key,
            )
            if not artifact:
                return {"error": f"No artifact with key '{key}' in bound collection."}
            workspace_service.update_artifact(
                db, user_id, collection_id, artifact.id,
                state="archived",
            )
            return {
                "action": "archived",
                "key": key,
                "artifact_id": artifact.id,
            }

        return {"error": f"Unknown command: {command}"}
    finally:
        db.close()


# ===================================================================
# MCP Resources
# ===================================================================

@mcp.resource("agience://collections/{collection_id}")
def collection_resource(collection_id: str) -> str:
    """Read a collection and its artifacts as a JSON resource."""
    from services import collection_service
    from db.arango import list_collection_artifacts

    user_id = _user_id()
    arango = _get_arango()

    collection = collection_service.get_collection_for_user(
        arango, user_id, collection_id=collection_id,
    )
    artifacts = list_collection_artifacts(arango, collection_id)

    contents = {
        "collection": {
            "id": collection.id,
            "name": collection.name,
            "description": getattr(collection, "description", None),
        },
        "artifacts": [_serialize_artifact_version(c) for c in (artifacts or [])],
        "artifact_count": len(artifacts or []),
    }
    return json.dumps(contents, ensure_ascii=False, indent=2, default=str)


@mcp.resource("agience://workspaces/{workspace_id}")
def workspace_resource(workspace_id: str) -> str:
    """Read a workspace and its artifacts as a JSON resource."""
    from services import workspace_service

    user_id = _user_id()
    db = _get_arango()
    try:
        ws = workspace_service.get_workspace(db, user_id, workspace_id)
        artifacts = workspace_service.list_workspace_artifacts(db, user_id, workspace_id)

        contents = {
            "workspace": {
                "id": ws.id,
                "name": ws.name,
                "description": getattr(ws, "description", "") or "",
            },
            "artifacts": [_serialize_workspace_artifact(c) for c in (artifacts or [])],
            "artifact_count": len(artifacts or []),
        }
        return json.dumps(contents, ensure_ascii=False, indent=2, default=str)
    finally:
        db.close()


# ===================================================================
# Serialisation helpers
# ===================================================================

def _resolve_content(c: Any) -> Optional[str]:
    """Return artifact content, fetching from S3 if the inline field is empty."""
    inline = getattr(c, "content", None)
    if inline:
        return inline
    try:
        import json as _json
        ctx = _json.loads(getattr(c, "context", None) or "{}")
    except (ValueError, TypeError):
        return None
    ck = ctx.get("content_key")
    if not ck:
        return None
    try:
        from services.content_service import get_text_direct
        return get_text_direct(ck)
    except Exception:
        return None


def _serialize_workspace_artifact(c: Any) -> Dict[str, Any]:
    return {
        "id": getattr(c, "id", None),
        "workspace_id": getattr(c, "workspace_id", None),
        "state": getattr(c, "state", None),
        "context": getattr(c, "context", None),
        "content": _resolve_content(c),
        "root_id": getattr(c, "root_id", None),
        "created_time": str(getattr(c, "created_time", None) or ""),
    }


def _serialize_artifact_version(c: Any) -> Dict[str, Any]:
    return {
        "id": getattr(c, "id", None),
        "root_id": getattr(c, "root_id", None),
        "context": getattr(c, "context", None),
        "content": _resolve_content(c),
        "created_time": str(getattr(c, "created_time", None) or ""),
        "created_by": getattr(c, "created_by", None),
    }


# ===================================================================
# TOOLS  -- Share Loop (Phase 1 GTM)
# ===================================================================

def _run_async(coro: Any) -> Any:
    """Run *coro* to completion from a sync MCP tool.

    FastMCP tool handlers are sync functions, but several code paths we
    depend on (``operation_dispatcher.dispatch``, ``email_service.send_*``)
    are async. This helper handles both "no running loop" and "loop already
    running in this thread" (FastAPI/uvicorn worker) cases.
    """
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside a running loop (FastAPI worker). Offload to a thread
    # so we don't deadlock on asyncio.run() inside a running loop.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, readOnlyHint=False))
def invoke_artifact(
    artifact_id: str,
    workspace_id: Optional[str] = None,
    input: Optional[str] = None,
    artifacts: Optional[List[str]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    """Invoke an artifact's ``invoke`` operation.

    Generic across artifact types: transforms, packages, or anything with
    an ``operations.invoke`` block in its ``type.json``. Dispatches through
    the operation_dispatcher so grant checks, event emission, and handler
    selection all run the same way as ``POST /artifacts/{id}/invoke``.
    """
    from services.operation_dispatcher import dispatch, DispatchContext

    db = _get_arango()
    try:
        user_id = _user_id()
        doc = db.collection("artifacts").get(artifact_id)
        if not doc:
            return {"error": f"Artifact {artifact_id} not found"}

        # Match the shape artifacts_router builds so the handler's
        # input_mapping resolution sees the fields it expects.
        merged_params = dict(params or {})
        if workspace_id and "workspace_id" not in merged_params:
            merged_params["workspace_id"] = workspace_id
        if artifacts and "artifacts" not in merged_params:
            merged_params["artifacts"] = artifacts
        merged_params["transform_id"] = artifact_id

        body: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "artifacts": artifacts or [],
            "input": input or "",
            "params": merged_params,
            "arguments": merged_params,
        }

        ctx = DispatchContext(
            user_id=user_id,
            actor_id=user_id,
            grants=list(getattr(_current_auth_context.get(None), "grants", []) or []),
            arango_db=db,
        )

        result = _run_async(dispatch("invoke", doc, body, ctx))
        if hasattr(result, "to_dict"):
            return {"result": result.to_dict()}
        return {"result": result}
    except Exception as exc:  # noqa: BLE001 --- MCP tools return error dicts
        logger.warning("invoke_artifact failed: %s", exc)
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, readOnlyHint=False))
def share(
    workspace_id: str,
    role: str = "viewer",
    target_email: Optional[str] = None,
    max_claims: int = 1,
    message: Optional[str] = None,
) -> dict:
    """Share a workspace by creating an invite link.

    Creates an invite grant with a role preset (viewer / editor /
    collaborator / admin) and returns a claim URL. When *target_email* is
    set, sends a transactional invite email via the configured provider.

    Workspace-only by design: collections are reached through packages /
    registry / direct grants, not the invite flow.

    Grant-level access check: the caller must hold ``can_share`` or
    ``can_admin`` on the workspace.

    Human gate: ``destructiveHint=True`` so MCP clients (Claude Code,
    Cursor, etc.) prompt the user before executing.
    """
    from services import grant_service

    db = _get_arango()
    try:
        user_id = _user_id()

        # Enforce can_share permission on the workspace.
        if not grant_service.can_share(db, user_id, workspace_id):
            return {"error": "You need share or admin permission on this workspace."}

        try:
            grant, raw_token = grant_service.create_invite(
                db,
                user_id=user_id,
                resource_id=workspace_id,
                role=role,
                target_email=target_email,
                max_claims=max_claims,
                message=message,
            )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "grant_id": grant.id,
            "claim_url": grant_service.build_claim_url(raw_token),
            "claim_token": raw_token,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("share failed: %s", exc)
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, readOnlyHint=False))
def accept_invite(
    token: str,
) -> dict:
    """Accept a workspace invite by presenting the claim token.

    Requires a human-initiated JWT (``principal_type == "user"``).
    Server tokens and raw API keys cannot accept invites --- accepting is
    a consent decision that should not happen autonomously.

    Delegation tokens (issued by Core when a server acts on a user's
    behalf) resolve to ``principal_type = "user"`` with an ``actor`` set,
    so they are allowed.
    """
    from services import grant_service
    from services.grant_service import (
        InviteNotFound,
        InviteExhausted,
        InviteIdentityMismatch,
    )

    db = _get_arango()
    try:
        user_id = _user_id()

        auth = _current_auth_context.get(None)
        principal_type = getattr(auth, "principal_type", "user") if auth else "user"
        if principal_type != "user":
            return {"error": "Only human-initiated tokens can accept invites"}

        try:
            grant = grant_service.claim_invite(db, user_id, token)
        except InviteIdentityMismatch as exc:
            return {"error": str(exc)}
        except InviteExhausted as exc:
            return {"error": str(exc)}
        except InviteNotFound as exc:
            return {"error": str(exc)}
        # grant_service.claim_invite emits grant.invite.claimed internally.

        return {
            "grant_id": grant.id,
            "resource_id": grant.resource_id,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("accept_invite failed: %s", exc)
        return {"error": str(exc)}
    finally:
        db.close()


# ===================================================================
# Direct tool dispatch (for Order-artifact execution without HTTP round-trip)
# ===================================================================

#: Map of tool name -> callable  -- populated once all @mcp.tool() functions are defined.
TOOL_REGISTRY: Dict[str, Any] = {
    "search": search,
    "get_artifact": get_artifact,
    "browse_collections": browse_collections,
    "browse_workspaces": browse_workspaces,
    "create_artifact": create_artifact,
    "update_artifact": update_artifact,
    "manage_artifact": manage_artifact,
    "extract_information": extract_information,
    "commit_preview": commit_preview,
    "commit_workspace": commit_workspace,
    "ask": ask,
    "discover_tools": discover_tools,
    "todo_list": todo_list,
    "memory": memory,
    # Share loop
    "invoke_artifact": invoke_artifact,
    "share": share,
    "accept_invite": accept_invite,
}


def call_local_tool(tool_name: str, tool_args: Dict[str, Any], user_id: str) -> Any:
    """Call an agience-core MCP tool directly from Python without an HTTP round-trip.

    Sets the per-request ``_current_user_id`` context var so the tool can
    determine the caller identity, then invokes the registered function.

    Raises ``ValueError`` if *tool_name* is not registered.
    """
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        raise ValueError(f"Unknown agience-core tool: {tool_name!r}")
    tok = _current_user_id.set(user_id)
    try:
        return fn(**tool_args)
    finally:
        _current_user_id.reset(tok)


# ===================================================================
# Auth middleware (ASGI)
# ===================================================================

class MCPAuthMiddleware:
    """ASGI middleware that validates Bearer tokens on incoming MCP requests.

    Sets ``_current_user_id`` context var so tool functions can
    identify the caller. Supports JWTs (user and server), direct API keys,
    and server JWTs with ``X-On-Behalf-Of`` for delegated user identity.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if not auth_header.startswith("Bearer "):
            await _send_401(send, "Missing Bearer token")
            return

        token = auth_header.replace("Bearer ", "", 1).strip()
        if not token:
            await _send_401(send, "Empty Bearer token")
            return

        arango = _get_arango()
        try:
            ctx = resolve_auth(token=token, arango_db=arango)

            # Determine effective user_id
            if ctx.principal_type == "server":
                # Server JWT -- check X-On-Behalf-Of for delegated user identity
                on_behalf_of = headers.get(b"x-on-behalf-of", b"").decode().strip()
                user_id = on_behalf_of or ""
            else:
                user_id = str(ctx.user_id) if ctx.user_id else ""

            if not user_id:
                await _send_401(send, "Missing subject user")
                return
            api_key = ctx.api_key_entity
            if api_key and not getattr(api_key, "client_id", None):
                api_key.client_id = getattr(api_key, "name", None)
        except HTTPException as exc:
            detail = exc.detail if getattr(exc, "detail", None) else "Invalid token or API key"
            await _send_401(send, str(detail))
            return
        except Exception as exc:
            detail = getattr(exc, "detail", "Invalid token or API key")
            await _send_401(send, str(detail))
            return

        # Set context vars and proceed
        tok = _current_user_id.set(user_id)
        api_tok = _current_api_key.set(api_key)
        ctx_tok = _current_auth_context.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_auth_context.reset(ctx_tok)
            _current_api_key.reset(api_tok)
            _current_user_id.reset(tok)


async def _send_401(send, detail: str):
    """Send an HTTP 401 response."""
    body = json.dumps({"error": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            [b"content-type", b"application/json"],
            [b"content-length", str(len(body)).encode()],
        ],
    })
    await send({"type": "http.response.body", "body": body})


# ===================================================================
# App factory
# ===================================================================

def create_mcp_app():
    """Return the MCP ASGI app wrapped with auth middleware.

    Mount this on the host FastAPI app::

        from starlette.routing import Mount
        app.routes.append(Mount("/mcp", app=create_mcp_app()))
    """
    return MCPAuthMiddleware(mcp.streamable_http_app())
