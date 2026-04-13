"""Transform artifact execution agent.

Parses the ``run`` block from a Transform artifact and dispatches based on
``run.type``.  Handles single-transform dispatch (``mcp-tool``,
``transform-ref``) directly.  Delegates ``workflow`` orchestration to Verso.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import HTTPException, status

_MAX_TRANSFORM_DEPTH = 10


def _resolve_artifact_content(artifact: Any) -> str:
    """Return the text content of an artifact, fetching from S3 if inline is empty.

    When workspace_service stores new/updated content in S3, artifact.content is
    set to "" and artifact.context["content_key"] carries the S3 key.  Agents
    that need the raw payload (operator JSON, authorizer config, etc.) should use
    this helper instead of accessing artifact.content directly.
    """
    if artifact.content:
        return artifact.content
    try:
        ctx = json.loads(artifact.context or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    content_key = ctx.get("content_key")
    if not content_key:
        return ""
    try:
        from services.content_service import get_text_direct
        return get_text_direct(content_key)
    except Exception:
        return ""


def _resolve_input_mapping(mapping: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve '$.field' and '$.field[N]' references in *mapping* against *params*.

    Non-string values and strings that don't start with '$.' are used as-is.
    Keys that resolve to None are omitted from the output.
    """
    result: Dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, str) and value.startswith("$."):
            path = value[2:]
            if "[" in path:
                field, rest = path.split("[", 1)
                try:
                    idx = int(rest.rstrip("]"))
                except ValueError:
                    idx = 0
                raw = params.get(field)
                resolved = raw[idx] if isinstance(raw, list) and len(raw) > idx else None
            else:
                resolved = params.get(path)
            if resolved is not None:
                result[key] = resolved
        else:
            result[key] = value
    return result


def _parse_run_block(context_json: str | None) -> Dict[str, Any]:
    """Extract the ``run`` block from a transform artifact's context JSON."""
    try:
        ctx = json.loads(context_json or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Transform artifact has invalid JSON context.")

    run = (
        ctx.get("run")
        or (ctx.get("transform") or {}).get("run")
        or (ctx.get("order") or {}).get("run")
    )
    if not run:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Transform artifact has no 'run' block.")
    return run


def transform_executor(*, db=None, user_id, arango_db=None,
                        agent_params=None, transform_id=None, workspace_id=None,
                        **kwargs):
    """Execute a Transform artifact based on its ``run`` block.

    Accepts ``transform_id`` and ``workspace_id`` either as direct keyword
    arguments or inside ``agent_params``.
    """
    from services import mcp_service, workspace_service
    from services import server_registry
    from core.dependencies import get_arango_db as _get_arango_db

    params = dict(agent_params or {})
    if db is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database session is required.")
    _arango_db = arango_db or next(_get_arango_db())

    # Allow direct kwargs to override agent_params
    _transform_id = transform_id or params.get("transform_id")
    _workspace_id = workspace_id or params.get("workspace_id")
    if _transform_id:
        params["transform_id"] = _transform_id
    if _workspace_id:
        params["workspace_id"] = _workspace_id

    if not params.get("transform_id"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "transform_id is required.")
    if not params.get("workspace_id"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "workspace_id is required.")

    # Recursion depth guard
    depth = params.get("_depth", 0)
    if depth >= _MAX_TRANSFORM_DEPTH:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transform recursion depth exceeded (max {_MAX_TRANSFORM_DEPTH}).",
        )

    try:
        transform_artifact = workspace_service.get_workspace_artifact(
            db, user_id, params["workspace_id"], params["transform_id"]
        )
    except Exception:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Transform artifact not found: {params['transform_id']}",
        )

    # Inject transform-artifact metadata so input_mapping can reference them
    params["_artifact_id"] = params["transform_id"]
    params["_artifact_content"] = _resolve_artifact_content(transform_artifact)

    run = _parse_run_block(transform_artifact.context)
    run_type = run.get("type")

    # --- mcp-tool: call an MCP tool directly ---
    if run_type == "mcp-tool":
        # Phase 7C — accept either a built-in persona slug, "agience-core",
        # or a seeded server artifact UUID. mcp_service.invoke_tool handles
        # all three internally; here we resolve a bare slug to its UUID
        # so the dispatch path is consistent across call sites.
        raw_server = run.get("server_artifact_id") or run.get("server") or "agience-core"
        if raw_server in ("agience-core", "desktop-host") or raw_server.startswith("local-mcp:"):
            server = raw_server
        else:
            server = server_registry.resolve_name_to_id(raw_server)
        tool = run.get("tool")
        if not tool:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Transform artifact 'run' block is missing 'tool'.")

        input_mapping = run.get("input_mapping") or {}
        tool_args = _resolve_input_mapping(input_mapping, params)

        try:
            return mcp_service.invoke_tool(
                db=_arango_db,
                user_id=user_id,
                workspace_id=params["workspace_id"],
                server_artifact_id=server,
                tool_name=tool,
                arguments=tool_args,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        except Exception as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Tool execution failed: {exc}")

    # --- transform-ref: delegate to another Transform artifact ---
    elif run_type in {"transform-ref", "order-ref"}:
        child_transform_id = run.get("transform_id")
        if not child_transform_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "transform-ref run block is missing 'transform_id'.",
            )

        # Build child params: start from current params, apply input_mapping overrides
        child_params = dict(params)
        input_mapping = run.get("input_mapping") or {}
        mapped = _resolve_input_mapping(input_mapping, params)
        child_params.update(mapped)

        # Point at the child transform and increment depth
        child_params["transform_id"] = child_transform_id
        child_params["_depth"] = depth + 1

        return transform_executor(
            db=db,
            user_id=user_id,
            arango_db=arango_db,
            agent_params=child_params,
        )

    # --- workflow: delegate orchestration to Verso ---
    elif run_type == "workflow":
        workflow_params = {
            "workflow_artifact_id": params["transform_id"],
            "workspace_id": params["workspace_id"],
        }
        # Forward state_artifact_id and other params as JSON
        extra = {k: v for k, v in params.items()
                 if k not in ("transform_id", "_artifact_id", "_artifact_content", "_depth")
                 and not k.startswith("_")}
        if extra:
            workflow_params["params"] = json.dumps(extra)

        try:
            return mcp_service.invoke_tool(
                db=_arango_db,
                user_id=user_id,
                workspace_id=params["workspace_id"],
                server_artifact_id=server_registry.resolve_name_to_id("verso"),
                tool_name="run_workflow",
                arguments=workflow_params,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        except Exception as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Workflow execution failed: {exc}")

    # --- llm: delegate prompt execution to Verso ---
    elif run_type == "llm":
        connection_artifact_id = run.get("connection_artifact_id")
        prompt = run.get("prompt", "")
        llm_temperature = run.get("temperature", 0.7)
        llm_max_tokens = run.get("max_output_tokens", 2048)

        # Build messages from prompt template and input
        input_text = params.get("input", "")
        messages = []
        if prompt:
            messages.append({"role": "system", "content": prompt})
        if input_text:
            messages.append({"role": "user", "content": input_text})

        # Apply input_mapping if present
        input_mapping = run.get("input_mapping") or {}
        if input_mapping:
            mapped = _resolve_input_mapping(input_mapping, params)
            if "messages" in mapped:
                # Allow overriding messages entirely via mapping
                raw_msgs = mapped["messages"]
                if isinstance(raw_msgs, str):
                    try:
                        messages = json.loads(raw_msgs)
                    except json.JSONDecodeError:
                        messages.append({"role": "user", "content": raw_msgs})
                elif isinstance(raw_msgs, list):
                    messages = raw_msgs

        if not messages:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "LLM transform has no prompt or input.")

        invoke_args = {
            "connection_artifact_id": connection_artifact_id or "",
            "workspace_id": params["workspace_id"],
            "messages": json.dumps(messages),
            "temperature": llm_temperature,
            "max_output_tokens": llm_max_tokens,
        }

        try:
            return mcp_service.invoke_tool(
                db=_arango_db,
                user_id=user_id,
                workspace_id=params["workspace_id"],
                server_artifact_id=server_registry.resolve_name_to_id("verso"),
                tool_name="invoke_llm",
                arguments=invoke_args,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        except Exception as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"LLM execution failed: {exc}")
    elif run_type == "webhook":
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Transform run type 'webhook' is not yet implemented.")
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown Transform run type: {run_type!r}")


AGENT = transform_executor
