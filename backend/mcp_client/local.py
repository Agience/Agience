"""
Agience Core MCP - Local agent and tool routing.

This module implements the built-in "agience-core" MCP server that routes
to local agents, tools, and resources. It's automatically available to all
users and provides access to Agience's native capabilities.

Architecture:
- Implements MCP protocol (list_tools, list_resources, call_tool)
- Routes tool calls to local agents (agents/*)
- Exposes user's collections as resources
- No network calls - all local Python function invocation
- Injected with DB sessions and user context from mcp_service
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from arango.database import StandardDatabase
from fastapi import HTTPException
from mcp_client.contracts import MCPTool, MCPResourceDesc

from entities.api_key import APIKey

logger = logging.getLogger(__name__)


class AgienceCoreLocalMCP:
    """
    Local MCP implementation that routes to Agience's built-in agents and tools.
    
    This is NOT a network client - it's a local router that implements the
    MCP protocol interface to provide consistency with external MCP servers.
    """
    
    def __init__(self, db: StandardDatabase, arango_db: StandardDatabase, user_id: str, api_key: Optional[APIKey] = None):
        """
        Initialize with database sessions and user context.

        Args:
            db: ArangoDB session (primary)
            arango_db: ArangoDB session for collections
            user_id: Current user's ID for scoping resources
        """
        self.db = db
        self.arango_db = arango_db
        self.user_id = user_id
        self.api_key = api_key
    
    def list_tools(self) -> List[MCPTool]:
        """
        List available local agents as MCP tools.
        
        Discovers agents from agents/* and exposes them as callable tools.
        Tool names follow pattern: agent_name (e.g., "search_artifacts")
        """
        tools = [
            # Collection tools
            MCPTool(
                name="search_collections",
                description="Search collections by name/description substring.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                },
            ),
            MCPTool(
                name="collections.list",
                description="List collections.",
                input_schema={"type": "object", "properties": {}},
            ),
            MCPTool(
                name="collections.artifacts",
                description="List artifacts in a collection.",
                input_schema={
                    "type": "object",
                    "properties": {"collection_id": {"type": "string"}},
                    "required": ["collection_id"],
                },
            ),
            MCPTool(
                name="collections.archive_version",
                description="Archive a collection artifact version by version_id.",
                input_schema={
                    "type": "object",
                    "properties": {"version_id": {"type": "string"}},
                    "required": ["version_id"],
                },
            ),
            MCPTool(
                name="create_collection",
                description="Create a new collection.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["name"],
                },
            ),
            # Workspace tools
            MCPTool(
                name="workspaces.list",
                description="List workspaces.",
                input_schema={"type": "object", "properties": {}},
            ),
            MCPTool(
                name="workspaces.list_cards",
                description="List artifacts in a workspace.",
                input_schema={
                    "type": "object",
                    "properties": {"workspace_id": {"type": "string"}},
                    "required": ["workspace_id"],
                },
            ),
            MCPTool(
                name="workspaces.create_artifact",
                description="Create a new workspace artifact.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "context": {"type": "object"},
                        "content": {"type": "string"},
                    },
                    "required": ["workspace_id"],
                },
            ),
            MCPTool(
                name="workspaces.update_artifact",
                description="Update an existing workspace artifact.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                        "context": {"type": "object"},
                        "content": {"type": "string"},
                        "state": {"type": "string"},
                    },
                    "required": ["workspace_id", "artifact_id"],
                },
            ),
            MCPTool(
                name="workspaces.get_artifact",
                description="Get a workspace artifact.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                    },
                    "required": ["workspace_id", "artifact_id"],
                },
            ),
            MCPTool(
                name="workspaces.delete_artifact",
                description="Delete a workspace artifact.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                    },
                    "required": ["workspace_id", "artifact_id"],
                },
            ),
            MCPTool(
                name="workspaces.reorder",
                description="Reorder artifacts within a workspace.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "changes": {"type": "array"},
                    },
                    "required": ["workspace_id", "changes"],
                },
            ),
            MCPTool(
                name="workspaces.archive_artifact",
                description="Archive a workspace artifact.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                    },
                    "required": ["workspace_id", "artifact_id"],
                },
            ),
            MCPTool(
                name="workspaces.revert_artifact",
                description="Revert a workspace artifact.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                    },
                    "required": ["workspace_id", "artifact_id"],
                },
            ),
            MCPTool(
                name="workspaces.search_artifacts",
                description="Search artifacts within a workspace using unified search.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "query_text": {"type": "string"},
                        "size": {"type": "integer", "default": 20},
                    },
                    "required": ["workspace_id", "query_text"],
                },
            ),
            MCPTool(
                name="extract_information",
                description=(
                    "Extract structured information (decisions/constraints/actions/claims) from a source artifact, optionally using "
                    "additional selected artifact artifacts for context. Creates new workspace artifacts (apply-only)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "description": "Workspace ID"},
                        "source_artifact_id": {"type": "string", "description": "Primary source artifact ID (e.g. transcript or notes)"},
                        "artifact_artifact_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional additional artifact IDs used as artifacts/context",
                            "default": [],
                        },
                        "model": {"type": "string", "description": "OpenAI model override (optional)"},
                        "max_units": {"type": "integer", "description": "Maximum units to create", "default": 12},
                    },
                    "required": ["workspace_id", "source_artifact_id"],
                },
            ),
            MCPTool(
                name="commit_preview",
                description="Preview workspace artifact promotion into collections without applying changes.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["workspace_id"],
                },
            ),
            MCPTool(
                name="commit_workspace",
                description="Commit workspace artifacts to collections.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["workspace_id"],
                },
            ),
            # Search tools
            MCPTool(
                name="search.query",
                description="Unified hybrid search across workspaces and collections.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query_text": {"type": "string"},
                        "size": {"type": "integer", "default": 20},
                        "from": {"type": "integer", "default": 0},
                        "workspace_ids": {"type": "array", "items": {"type": "string"}},
                        "collection_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["query_text"],
                },
            ),
            # Commit helper
            MCPTool(
                name="collections.commit",
                description="Commit workspace artifacts to collections.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "artifact_ids": {"type": "array", "items": {"type": "string"}},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    "required": ["workspace_id"],
                },
            ),
        ]
        
        return tools

    def list_prompts(self) -> List[Dict[str, Any]]:
        """List built-in prompts.

        Placeholder for now; external MCP servers may provide prompts.
        """
        return []
    
    def list_resources(self) -> List[MCPResourceDesc]:
        """
        List user's collections as MCP resources.
        
        Each collection becomes a resource that can be imported into workspaces.
        Resources expose collection metadata and artifact summaries.
        """
        resources: List[MCPResourceDesc] = []
        
        # Collections
        try:
            from services import collection_service

            collections = collection_service.get_collections_for_user(
                self.arango_db,
                self.user_id,
            )
            for collection in collections or []:
                resources.append(
                    MCPResourceDesc(
                        id=f"agience://collections/{collection.id}",
                        kind="collection",
                        uri=f"agience://collections/{collection.id}",
                        title=collection.name,
                        text=collection.description or "",
                        content_type="application/vnd.agience.collection+json",
                        props={
                            "collection_id": collection.id,
                            "artifact_count": len(getattr(collection, "artifact_ids", None) or []),
                        },
                    )
                )
        except Exception as e:
            logger.error(f"Failed to list collection resources: {e}")

        # Workspaces
        try:
            from services import workspace_service

            workspaces = workspace_service.list_workspaces(self.db, self.user_id)
            for ws in workspaces or []:
                resources.append(
                    MCPResourceDesc(
                        id=f"agience://workspaces/{ws.id}",
                        kind="workspace",
                        uri=f"agience://workspaces/{ws.id}",
                        title=ws.name,
                        text=getattr(ws, "description", "") or "",
                        content_type="application/vnd.agience.workspace+json",
                        props={
                            "workspace_id": ws.id,
                        },
                    )
                )
        except Exception as e:
            logger.error(f"Failed to list workspace resources: {e}")

        return resources

    def read_resource(self, uri: str) -> Dict[str, Any]:
        """Read a built-in resource.

        Currently supports:
        - agience://collections/{collection_id}
        - agience://workspaces/{workspace_id}
        """
        if uri.startswith("agience://collections/"):
            collection_id = uri.replace("agience://collections/", "", 1)
            self._require_resource_read("application/vnd.agience.collection+json", "collections", collection_id)

            from services import collection_service
            from db.arango import list_collection_artifacts

            collection = collection_service.get_collection_for_user(
                self.arango_db,
                self.user_id,
                collection_id=collection_id,
            )
            artifacts = list_collection_artifacts(self.arango_db, collection_id)

            contents = {
                "collection": {
                    "id": collection.id,
                    "name": collection.name,
                    "description": getattr(collection, "description", None),
                },
                "artifacts": [
                    {
                        "id": getattr(c, "id", None),
                        "root_id": getattr(c, "root_id", None),
                        "context": getattr(c, "context", None),
                        "content": getattr(c, "content", None),
                        "created_time": getattr(c, "created_time", None),
                        "created_by": getattr(c, "created_by", None),
                    }
                    for c in (artifacts or [])
                ],
                "artifact_count": len(artifacts or []),
            }

            return {
                "uri": uri,
                "name": collection.name,
                "title": collection.name,
                "mimeType": "application/vnd.agience.collection+json",
                "text": json.dumps(contents, ensure_ascii=False, indent=2),
            }

        if uri.startswith("agience://workspaces/"):
            workspace_id = uri.replace("agience://workspaces/", "", 1)
            self._require_resource_read("application/vnd.agience.workspace+json", "workspaces", workspace_id)

            from services import workspace_service

            ws = workspace_service.get_workspace(self.db, self.user_id, workspace_id)
            artifacts = workspace_service.list_workspace_artifacts(self.db, self.user_id, workspace_id)

            contents = {
                "workspace": {
                    "id": ws.id,
                    "name": ws.name,
                    "description": getattr(ws, "description", "") or "",
                },
                "artifacts": [
                    {
                        "id": getattr(c, "id", None),
                        "workspace_id": getattr(c, "workspace_id", None),
                        "state": getattr(c, "state", None),
                        "prev_state": getattr(c, "prev_state", None),
                        "context": getattr(c, "context", None),
                        "content": getattr(c, "content", None),
                        "root_id": getattr(c, "root_id", None),
                        "committed_collection_ids": getattr(c, "committed_collection_ids", None),
                        "order_key": getattr(c, "order_key", None),
                        "created_time": getattr(c, "created_time", None),
                        "created_by": getattr(c, "created_by", None),
                    }
                    for c in (artifacts or [])
                ],
                "artifact_count": len(artifacts or []),
            }

            return {
                "uri": uri,
                "name": ws.name,
                "title": ws.name,
                "mimeType": "application/vnd.agience.workspace+json",
                "text": json.dumps(contents, ensure_ascii=False, indent=2),
            }

        raise ValueError(f"Unsupported resource URI: {uri}")
    
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute local agent function.
        
        Routes tool calls to corresponding agent implementations in agents/*.
        Injects database sessions and user context automatically.
        
        Args:
            tool_name: Agent function name (e.g., "extract_information")
            arguments: Tool arguments from MCP client
            
        Returns:
            Tool execution result with output and metadata
        """
        logger.info(f"Agience Core: Calling tool {tool_name} with args: {arguments}")

        if tool_name == "extract_information":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_extract_information(arguments)

        if tool_name == "commit_preview":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_commit_workspace(arguments, dry_run=True)
        if tool_name == "commit_workspace":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_commit_workspace(arguments, dry_run=False)

        if tool_name == "search_collections":
            return self._call_search_collections(arguments)
        if tool_name == "collections.list":
            return self._call_collections_list(arguments)
        if tool_name == "collections.artifacts":
            return self._call_collections_artifacts(arguments)
        if tool_name == "collections.archive_version":
            return self._call_collections_archive_version(arguments)
        if tool_name == "create_collection":
            self._require_tool_invoke("application/vnd.agience.collection+json")
            return self._call_create_collection(arguments)

        # Workspace tools
        if tool_name == "workspaces.list":
            self._require_tool_search("application/vnd.agience.workspace+json")
            return self._call_workspaces_list(arguments)
        if tool_name == "workspaces.list_cards":
            self._require_tool_search("application/vnd.agience.workspace+json")
            return self._call_workspaces_list_artifacts(arguments)
        if tool_name == "workspaces.get_artifact":
            self._require_tool_search("application/vnd.agience.workspace+json")
            return self._call_workspaces_get_artifact(arguments)
        if tool_name == "workspaces.search_artifacts":
            self._require_tool_search("application/vnd.agience.workspace+json")
            return self._call_workspaces_search_artifacts(arguments)

        if tool_name == "workspaces.create_artifact":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_workspaces_create_artifact(arguments)
        if tool_name == "workspaces.update_artifact":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_workspaces_update_artifact(arguments)
        if tool_name == "workspaces.delete_artifact":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_workspaces_delete_artifact(arguments)
        if tool_name == "workspaces.reorder":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_workspaces_reorder(arguments)
        if tool_name == "workspaces.archive_artifact":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_workspaces_archive_artifact(arguments)
        if tool_name == "workspaces.revert_artifact":
            self._require_tool_invoke("application/vnd.agience.workspace+json")
            return self._call_workspaces_revert_artifact(arguments)

        # Unified search
        if tool_name == "search.query":
            return self._call_search_query(arguments)

        # Commit alias
        if tool_name == "collections.commit":
            return self._call_commit_workspace(arguments, dry_run=bool(arguments.get("dry_run", False)))

        return {
            "error": f"Unknown tool: {tool_name}",
            "available_tools": [t.name for t in self.list_tools()],
        }

    def _call_extract_information(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """MCP tool: extract_information — proxies to Aria's extract_units tool."""
        from services import mcp_service

        workspace_id = str(arguments.get("workspace_id") or "").strip()
        source_artifact_id = str(arguments.get("source_artifact_id") or "").strip()

        if not workspace_id:
            raise HTTPException(status_code=400, detail="workspace_id is required")
        if not source_artifact_id:
            raise HTTPException(status_code=400, detail="source_artifact_id is required")

        artifact_artifact_ids = arguments.get("artifact_artifact_ids") or []
        if not isinstance(artifact_artifact_ids, list):
            artifact_artifact_ids = []
        artifact_artifact_ids = [str(x) for x in artifact_artifact_ids if str(x).strip()]

        tool_args: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "source_artifact_id": source_artifact_id,
            "artifact_artifact_ids": artifact_artifact_ids,
        }
        max_units = arguments.get("max_units")
        if isinstance(max_units, int):
            tool_args["max_units"] = max_units
        model = arguments.get("model")
        if isinstance(model, str) and model.strip():
            tool_args["model"] = model.strip()

        try:
            # Pass the seeded Aria artifact UUID, not the bare name.
            from services import server_registry
            return mcp_service.invoke_tool(
                db=self.arango_db,
                user_id=self.user_id,
                workspace_id=workspace_id,
                server_artifact_id=server_registry.resolve_name_to_id("aria"),
                tool_name="extract_units",
                arguments=tool_args,
            )
        except Exception as exc:
            logger.warning("extract_information failed (Aria may be unavailable): %s", exc)
            return {"error": f"Extraction service unavailable: {exc}"}

    def _require_tool_search(self, content_type: str) -> None:
        if not self.api_key:
            return
        if not self.api_key.has_scope("tool", content_type, "search"):
            raise HTTPException(status_code=403, detail="API key missing required tool search scope")

    def _require_tool_invoke(self, content_type: str) -> None:
        if not self.api_key:
            return
        if not self.api_key.has_scope("tool", content_type, "invoke"):
            raise HTTPException(status_code=403, detail="API key missing required tool invoke scope")

    def _require_resource_read(self, content_type: str, resource_type: str, resource_id: str) -> None:
        if not self.api_key:
            return
        if not self.api_key.has_scope("resource", content_type, "read"):
            raise HTTPException(status_code=403, detail="API key missing required resource read scope")
        if not self.api_key.can_access_resource(resource_type, resource_id):
            raise HTTPException(status_code=403, detail="API key not permitted for this resource")

    def _serialize_collection(self, c: Any) -> Dict[str, Any]:
        return {
            "id": getattr(c, "id", None),
            "name": getattr(c, "name", None),
            "description": getattr(c, "description", None),
            "created_time": getattr(c, "created_time", None),
            "modified_time": getattr(c, "modified_time", None),
            "artifact_ids": getattr(c, "artifact_ids", None),
        }

    def _serialize_artifact_version(self, c: Any) -> Dict[str, Any]:
        return {
            "id": getattr(c, "id", None),
            "root_id": getattr(c, "root_id", None),
            "context": getattr(c, "context", None),
            "content": getattr(c, "content", None),
            "created_time": getattr(c, "created_time", None),
            "created_by": getattr(c, "created_by", None),
        }

    def _serialize_workspace(self, w: Any) -> Dict[str, Any]:
        return {
            "id": getattr(w, "id", None),
            "name": getattr(w, "name", None),
            "description": getattr(w, "description", "") or "",
            "created_time": getattr(w, "created_time", None),
            "modified_time": getattr(w, "modified_time", None),
        }

    def _serialize_workspace_artifact(self, c: Any) -> Dict[str, Any]:
        return {
            "id": getattr(c, "id", None),
            "workspace_id": getattr(c, "workspace_id", None),
            "state": getattr(c, "state", None),
            "prev_state": getattr(c, "prev_state", None),
            "context": getattr(c, "context", None),
            "content": getattr(c, "content", None),
            "root_id": getattr(c, "root_id", None),
            "committed_collection_ids": getattr(c, "committed_collection_ids", None),
            "order_key": getattr(c, "order_key", None),
            "created_time": getattr(c, "created_time", None),
            "created_by": getattr(c, "created_by", None),
        }

    def _call_search_collections(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import collection_service

        query = (arguments.get("query") or "").strip().lower()
        limit = int(arguments.get("limit") or 10)

        collections = collection_service.get_collections_for_user(self.arango_db, self.user_id)
        results = []
        for c in collections or []:
            haystack = f"{getattr(c, 'name', '')} {getattr(c, 'description', '')}".lower()
            if query and query not in haystack:
                continue
            results.append({
                "id": getattr(c, "id", None),
                "name": getattr(c, "name", None),
                "description": getattr(c, "description", None),
            })
            if len(results) >= limit:
                break
        return {"query": arguments.get("query"), "count": len(results), "results": results}

    def _call_collections_list(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import collection_service

        collections = collection_service.get_collections_for_user(self.arango_db, self.user_id)
        items = [self._serialize_collection(c) for c in (collections or [])]
        return {"count": len(items), "collections": items}

    def _call_collections_artifacts(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import collection_service

        collection_id = arguments.get("collection_id")
        artifacts = collection_service.get_collection_artifacts(
            self.arango_db,
            self.user_id,
            collection_id=collection_id,
        )
        items = [self._serialize_artifact_version(c) for c in (artifacts or [])]
        return {"collection_id": collection_id, "count": len(items), "artifacts": items}

    def _call_collections_archive_version(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import collection_service

        version_id = arguments.get("version_id")
        archived = bool(collection_service.archive_artifact_by_version_id(self.arango_db, self.user_id, version_id))
        return {"version_id": version_id, "archived": archived}

    def _call_create_collection(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import collection_service

        name = arguments.get("name")
        description = arguments.get("description") or ""
        created = collection_service.create_new_collection(
            self.arango_db,
            owner_id=self.user_id,
            name=name,
            description=description,
        )
        return {"collection_id": getattr(created, "id", None)}

    def _call_workspaces_list(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspaces = workspace_service.list_workspaces(self.db, self.user_id)
        items = [self._serialize_workspace(w) for w in (workspaces or [])]
        return {"count": len(items), "workspaces": items}

    def _call_workspaces_list_artifacts(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        artifacts = workspace_service.list_workspace_artifacts(self.db, self.user_id, workspace_id)
        items = [self._serialize_workspace_artifact(c) for c in (artifacts or [])]
        return {"workspace_id": workspace_id, "count": len(items), "artifacts": items}

    def _call_workspaces_create_artifact(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        context = arguments.get("context") or {}
        content = arguments.get("content") or ""
        artifact = workspace_service.create_workspace_artifact(
            self.db,
            self.user_id,
            workspace_id,
            context_json=json.dumps(context),
            content=content,
        )
        return {"artifact": self._serialize_workspace_artifact(artifact)}

    def _call_workspaces_update_artifact(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        artifact_id = arguments.get("artifact_id")
        context = arguments.get("context")
        content = arguments.get("content")
        state = arguments.get("state")
        artifact = workspace_service.update_artifact(
            self.db,
            self.user_id,
            workspace_id,
            artifact_id,
            context_json=json.dumps(context) if context is not None else None,
            content=content,
            state=state,

        )
        return {"artifact": self._serialize_workspace_artifact(artifact)}

    def _call_workspaces_get_artifact(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        artifact_id = arguments.get("artifact_id")
        artifact = workspace_service.get_workspace_artifact(self.db, self.user_id, workspace_id, artifact_id)
        return {"artifact": self._serialize_workspace_artifact(artifact)}

    def _call_workspaces_delete_artifact(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        artifact_id = arguments.get("artifact_id")
        workspace_service.delete_artifact(self.db, self.user_id, workspace_id, artifact_id)
        return {"deleted": True}

    def _call_workspaces_reorder(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        changes = arguments.get("changes") or []
        version = None
        for ch in changes:
            version = workspace_service.move_workspace_artifact(
                self.db,
                self.user_id,
                workspace_id,
                ch.get("id"),
                ch.get("before_id"),
                ch.get("after_id"),
                ch.get("version"),
            )
        return {"workspace_id": workspace_id, "version": version}

    def _call_workspaces_archive_artifact(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        artifact_id = arguments.get("artifact_id")
        artifact = workspace_service.update_artifact(
            self.db,
            self.user_id,
            workspace_id,
            artifact_id,
            state="archived",

        )
        return {"artifact": self._serialize_workspace_artifact(artifact)}

    def _call_workspaces_revert_artifact(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        artifact_id = arguments.get("artifact_id")
        artifact = workspace_service.revert_artifact(self.db, self.arango_db, self.user_id, workspace_id, artifact_id)
        return {"artifact": self._serialize_workspace_artifact(artifact)}

    def _call_search_query(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from search.accessor.search_accessor import SearchAccessor, SearchQuery

        query_text = arguments.get("query_text") or ""
        size = int(arguments.get("size") or 20)
        from_ = int(arguments.get("from") or 0)
        workspace_ids = arguments.get("workspace_ids")
        collection_ids = arguments.get("collection_ids")

        accessor = SearchAccessor()
        result = accessor.search(
            SearchQuery(
                query_text=query_text,
                user_id=self.user_id,
                workspace_ids=workspace_ids,
                collection_ids=collection_ids,
                from_=from_,
                size=size,
            )
        )

        hits = []
        for h in getattr(result, "hits", []) or []:
            hits.append({
                "id": getattr(h, "doc_id", None),
                "score": getattr(h, "score", None),
                "root_id": getattr(h, "root_id", None),
                "version_id": getattr(h, "version_id", None),
                "source_type": getattr(h, "source_type", None),
                "title": getattr(h, "title", None),
                "description": getattr(h, "description", None),
                "content": getattr(h, "content", None),
                "tags": getattr(h, "tags", None),
                "metadata": getattr(h, "metadata", None),
                "workspace_id": getattr(h, "workspace_id", None),
                "collection_id": getattr(h, "collection_id", None),
                "owner_id": getattr(h, "created_by", None),
                "state": getattr(h, "state", None),
                "is_head": getattr(h, "is_head", None),
                "highlights": getattr(h, "highlights", None),
            })

        return {
            "total": getattr(result, "total", 0),
            "hits": hits,
            "parsed_query": getattr(result, "parsed_query", None),
            "corrections": getattr(result, "corrections", []) or [],
            "used_hybrid": getattr(result, "used_hybrid", False),
        }

    def _call_workspaces_search_artifacts(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        workspace_id = arguments.get("workspace_id")
        query_text = arguments.get("query_text")
        size = arguments.get("size", 20)
        return self._call_search_query({
            "query_text": query_text,
            "size": size,
            "workspace_ids": [workspace_id] if workspace_id else None,
        })

    def _call_commit_workspace(self, arguments: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
        from services import workspace_service

        workspace_id = arguments.get("workspace_id")
        artifact_ids = arguments.get("artifact_ids")
        res = workspace_service.commit_workspace_to_collections(
            workspace_db=self.db,
            collection_db=self.arango_db,
            user_id=self.user_id,
            workspace_id=workspace_id,
            api_key=self.api_key,
            artifact_ids=artifact_ids,
            dry_run=dry_run,
        )
        return res.model_dump() if hasattr(res, "model_dump") else res

    def close(self):
        """No cleanup needed for local routing."""
        pass


def create_agience_core_client(db: StandardDatabase, arango_db: StandardDatabase, user_id: str) -> AgienceCoreLocalMCP:
    """
    Factory function to create Agience Core MCP client.

    This is called by mcp_service when server_id == "agience-core".

    Args:
        db: ArangoDB session (primary)
        arango_db: ArangoDB session
        user_id: Current user ID

    Returns:
        AgienceCoreLocalMCP instance
    """
    return AgienceCoreLocalMCP(db, arango_db, user_id, api_key=None)
