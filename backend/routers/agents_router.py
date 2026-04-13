# routers/agents_router.py
#
# Named operator invocation — POST /agents/invoke
#
# Dispatches named operators (e.g. "extract_units", "complete_authorizer_oauth")
# without requiring an artifact ID.  Resolution order:
#
#   1. _OPERATOR_TO_SERVER mapping → MCP tool dispatch
#   2. Agent plugin registry (agents/*.py) → local callable
#   3. LLM fallback via agent_service.invoke()

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from arango.database import StandardDatabase

from api.agents.invoke import InvokeRequest
from services.dependencies import get_auth, AuthContext
from core.dependencies import get_arango_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["Agents"])


@router.post("/invoke")
async def invoke_named_operator(
    body: InvokeRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Invoke a named operator without an artifact ID.

    Resolution order:
    1. MCP server mapping (event_dispatcher._OPERATOR_TO_SERVER)
    2. Local agent plugin (agents/*.py)
    3. LLM fallback (agent_service.invoke)
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User identification required")

    operator_name = (body.operator or "").strip()

    # Build merged params
    merged_params: Dict[str, Any] = dict(body.params or {})
    if body.operator_params:
        merged_params.update(body.operator_params)
    if body.workspace_id and "workspace_id" not in merged_params:
        merged_params["workspace_id"] = body.workspace_id
    if body.artifacts and "artifacts" not in merged_params:
        merged_params["artifacts"] = body.artifacts

    # Route 1: MCP server mapping
    if operator_name:
        from core.event_dispatcher import resolve_operator_server

        server_tool = resolve_operator_server(operator_name)
        if server_tool:
            from services import mcp_service
            from services import server_registry

            server, tool = server_tool
            # _OPERATOR_TO_SERVER yields a persona name; resolve to the
            # seeded vnd.agience.mcp-server+json artifact UUID so
            # mcp_service.invoke_tool dispatches via the artifact-native
            # path. Special-cased dispatch tokens are passed through.
            if server not in ("agience-core", "desktop-host") and not server.startswith("local-mcp:"):
                server = server_registry.resolve_name_to_id(server)
            try:
                result = mcp_service.invoke_tool(
                    db=arango_db,
                    user_id=auth.user_id,
                    workspace_id=body.workspace_id,
                    server_artifact_id=server,
                    tool_name=tool,
                    arguments=merged_params,
                )
                return result
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Invoke failed on {server}:{tool}: {exc}",
                )

    # Route 2: Agent plugin registry (agents/*.py)
    if operator_name:
        from agents import get_agent_callable, AgentNotFoundError
        from services import agent_service

        try:
            agent_fn = get_agent_callable(operator_name)
        except AgentNotFoundError:
            agent_fn = None

        if agent_fn is not None:
            try:
                result = agent_service.invoke(
                    db=arango_db,
                    arango_db=arango_db,
                    user_id=auth.user_id,
                    agent=agent_fn,
                    agent_params=merged_params,
                )
                # Normalise to dict for JSON response
                if isinstance(result, dict):
                    return result
                if hasattr(result, "model_dump"):
                    return result.model_dump()
                if hasattr(result, "__dict__"):
                    d = {k: v for k, v in vars(result).items() if not k.startswith("_")}
                    if d:
                        return d
                return result
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Agent '{operator_name}' failed: {exc}",
                )

    # Route 3: LLM fallback
    if body.input:
        from services import agent_service

        result = agent_service.invoke(
            db=arango_db,
            arango_db=arango_db,
            user_id=auth.user_id,
            input=body.input,
        )
        return {"output": getattr(result, "output", "")}

    raise HTTPException(
        status_code=400,
        detail=f"Cannot resolve operator '{operator_name}'. Provide a valid operator name or input text.",
    )
