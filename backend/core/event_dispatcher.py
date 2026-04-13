"""Workspace event dispatcher — Core platform infrastructure.

Scans workspace artifacts for handler definitions (context.type ==
"workspace-event-handler"), matches them against the incoming event, and
invokes the configured actions via MCP tool dispatch.

This module belongs in Core because event dispatching is type-agnostic
platform infrastructure — it routes events to registered handlers regardless
of content type. The handlers themselves live on servers.

Handler artifact schema (context JSON):
{
  "type": "workspace-event-handler",
  "enabled": true,
  "on": {
    "event_types": ["upload_complete", "artifact_created", ...],
    "source": {
      "context_type": "optional-match",
      "content_type": "optional-match",
      "require_transcript_status": "optional-match"
    }
  },
  "actions": [
    {
      "type": "invoke_operator",
      "operator": "ingest_runner",
      "operator_params": {...}
    }
  ]
}

The "invoke_operator" action type routes via MCP to the appropriate server
tool based on the operator name mapping below.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, Optional

from arango.database import StandardDatabase

from services import mcp_service
from services import server_registry
from services import types_service
from core.dependencies import get_arango_db as _get_arango_db

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")
_TRANSFORM_MIME = "application/vnd.agience.transform+json"

# Maps operator names to (server, tool) pairs for MCP dispatch.
# All domain logic lives on the server; this table is the only Core knowledge
# of which server owns which operator name.
_OPERATOR_TO_SERVER: Dict[str, tuple[str, str]] = {
    "transcribe":                  ("verso", "transcribe_artifact"),
    "ingest_runner":               ("astra", "ingest_text"),
    "extract_units":               ("aria",  "extract_units"),
    "provenance":                  ("aria",  "attach_provenance"),
    "transform_executor":          ("verso", "execute_transform"),
    # Colon-namespaced names used by inter-server calls
    "verso:invoke_llm":            ("verso", "invoke_llm"),
    "ophan:check_llm_allowance":   ("ophan", "check_llm_allowance"),
    "ophan:record_llm_usage":      ("ophan", "record_llm_usage"),
}


def resolve_operator_server(operator_name: str) -> Optional[tuple[str, str]]:
    """Look up the (server, tool) pair for a named operator.

    Returns ``None`` if the operator name has no server mapping.  This is the
    public API; callers outside this module should use this function rather
    than accessing ``_OPERATOR_TO_SERVER`` directly.
    """
    return _OPERATOR_TO_SERVER.get(operator_name)


def _parse_context(artifact) -> Optional[Dict[str, Any]]:
    """Safely parse artifact.context JSON string to dict."""
    raw = getattr(artifact, "context", None) or ""
    if not raw:
        return None
    try:
        ctx = json.loads(raw) if isinstance(raw, str) else raw
        return ctx if isinstance(ctx, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _resolve_source_content_type(source_ctx: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(source_ctx, dict):
        return None
    content_type = source_ctx.get("content_type")
    if not isinstance(content_type, str):
        return None
    cleaned = content_type.strip()
    return cleaned or None


_CONTRACT_LIFECYCLE_EVENTS = frozenset({"upload_complete"})


def _dispatch_contract_event(
    *,
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    event_type: str,
    source_artifact_id: str,
    source_ctx: Optional[Dict[str, Any]],
) -> None:
    # Contract-driven dispatch for artifact lifecycle events and upload events.
    if not isinstance(event_type, str):
        return
    if not event_type.startswith("artifact_") and event_type not in _CONTRACT_LIFECYCLE_EVENTS:
        return

    source_content_type = _resolve_source_content_type(source_ctx)
    if not source_content_type:
        return

    binding = types_service.resolve_event_binding(source_content_type, event_type)
    if not isinstance(binding, dict):
        return

    target_tool = binding.get("tool")
    if not isinstance(target_tool, str) or not target_tool.strip():
        return

    target_server = binding.get("server_artifact_id") or binding.get("server")
    if not isinstance(target_server, str) or not target_server.strip():
        target_server = "agience-core"

    # Resolve builtin server names to seeded server artifact UUIDs.
    # `agience-core`, `desktop-host`, and `local-mcp:*` are passed through.
    if target_server not in ("agience-core", "desktop-host") and not target_server.startswith("local-mcp:"):
        target_server = server_registry.resolve_name_to_id(target_server)

    mcp_service.invoke_tool(
        db=db,
        user_id=user_id,
        workspace_id=workspace_id,
        server_artifact_id=target_server,
        tool_name=target_tool.strip(),
        arguments={
            "workspace_id": workspace_id,
            "artifact_id": source_artifact_id,
            "source_artifact_id": source_artifact_id,
            "event_type": event_type,
        },
    )


def _is_handler(ctx: Dict[str, Any]) -> bool:
    return ctx.get("type") == "workspace-event-handler" and ctx.get("enabled", True) is not False


def _is_transform(ctx: Dict[str, Any]) -> bool:
    return ctx.get("content_type") == _TRANSFORM_MIME


def _matches_event_types(handler_ctx: Dict[str, Any], event_type: str) -> bool:
    on = handler_ctx.get("on") or {}
    event_types = on.get("event_types")
    if not event_types:
        return True  # no filter = match all
    if isinstance(event_types, list):
        return event_type in event_types
    return False


def _matches_source(handler_ctx: Dict[str, Any], source_ctx: Optional[Dict[str, Any]]) -> bool:
    on = handler_ctx.get("on") or {}
    source_filter = on.get("source")
    if not source_filter or not isinstance(source_filter, dict):
        return True  # no source filter = match all

    if source_ctx is None:
        return False

    ctx_type = source_filter.get("context_type") or source_filter.get("type")
    if ctx_type and source_ctx.get("type") != ctx_type:
        return False

    content_type = source_filter.get("content_type")
    if content_type and source_ctx.get("content_type") != content_type:
        return False

    req_status = source_filter.get("require_transcript_status")
    if req_status:
        transcript = source_ctx.get("transcript") or {}
        actual_status = transcript.get("status") if isinstance(transcript, dict) else source_ctx.get("transcript_status")
        if actual_status != req_status:
            return False

    return True


def _replace_templates(value: Any, variables: Dict[str, str]) -> Any:
    """Replace {{var}} templates in string values (shallow)."""
    if isinstance(value, str):
        return _TEMPLATE_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _replace_templates(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_templates(v, variables) for v in value]
    return value


def _resolve_json_path(expr: str, variables: Dict[str, Any]) -> Any:
    if not isinstance(expr, str) or not expr.startswith("$."):
        return expr

    current: Any = variables
    for segment in expr[2:].split("."):
        if current is None:
            return None
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?", segment)
        if not match:
            return None
        key, index = match.groups()
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if index is not None:
            if not isinstance(current, list):
                return None
            idx = int(index)
            if idx >= len(current):
                return None
            current = current[idx]
    return current


def _matches_transform_selector(transform_ctx: Dict[str, Any], selector: Optional[Dict[str, Any]]) -> bool:
    if not selector:
        return True
    order = transform_ctx.get("order") or {}
    if not isinstance(order, dict):
        order = {}

    kind = selector.get("kind")
    if kind and order.get("kind") != kind:
        return False
    subtype = selector.get("subtype")
    if subtype and order.get("subtype") != subtype:
        return False
    artifact_id = selector.get("artifact_id")
    if artifact_id and transform_ctx.get("id") != artifact_id:
        return False
    return True


def _matches_transform_source(transform_ctx: Dict[str, Any], source_ctx: Optional[Dict[str, Any]]) -> bool:
    if source_ctx is None:
        return False
    accepts = transform_ctx.get("accepts") or {}
    if not isinstance(accepts, dict):
        accepts = {}
    if not accepts:
        return True

    source_content_type = source_ctx.get("content_type") or ""
    processing = source_ctx.get("processing") or {}
    processing_handler = processing.get("handler") if isinstance(processing, dict) else None

    content_types = accepts.get("content_types") or []
    if content_types and source_content_type not in content_types:
        return False

    content_type_prefixes = accepts.get("content_type_prefixes") or []
    if content_type_prefixes and not any(source_content_type.startswith(prefix) for prefix in content_type_prefixes if isinstance(prefix, str)):
        return False

    processing_handlers = accepts.get("processing_handlers") or []
    if processing_handlers and processing_handler not in processing_handlers:
        return False

    return True


def _resolve_transform_artifact(
    artifacts: Iterable[Any],
    *,
    source_ctx: Optional[Dict[str, Any]],
    explicit_transform_id: Optional[str] = None,
    selector: Optional[Dict[str, Any]] = None,
):
    for artifact in artifacts:
        ctx = _parse_context(artifact)
        if ctx is None or not _is_transform(ctx):
            continue
        ctx = dict(ctx)
        ctx["id"] = getattr(artifact, "id", None)
        if explicit_transform_id and str(ctx.get("id")) != str(explicit_transform_id):
            continue
        if not _matches_transform_selector(ctx, selector):
            continue
        if not _matches_transform_source(ctx, source_ctx):
            continue
        return artifact, ctx
    return None, None


def _invoke_transform(
    transform_ctx: Dict[str, Any],
    *,
    transform_artifact_id: str,
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    source_artifact_id: str,
    collection_db: Optional[StandardDatabase] = None,
) -> None:
    run = transform_ctx.get("run") or (transform_ctx.get("order") or {}).get("run") or {}
    if not isinstance(run, dict):
        logger.warning("Transform %s has invalid run configuration", transform_artifact_id)
        return

    variables: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "source_artifact_id": source_artifact_id,
        "artifact_id": source_artifact_id,
        "transform_artifact_id": transform_artifact_id,
        "artifacts": [source_artifact_id],
    }
    input_mapping = run.get("input_mapping") or {}
    tool_args = {
        key: _resolve_json_path(value, variables)
        for key, value in input_mapping.items()
    }

    run_type = run.get("type")
    if run_type != "mcp-tool":
        logger.warning("Transform %s has unsupported run.type '%s'", transform_artifact_id, run_type)
        return

    server = run.get("server_artifact_id") or run.get("server") or "agience-core"
    tool_name = (run.get("tool") or "").strip()
    if not tool_name:
        logger.warning("Transform %s has no tool configured", transform_artifact_id)
        return
    if not isinstance(server, str) or not server.strip():
        logger.warning("Transform %s has no server configured", transform_artifact_id)
        return

    # Resolve builtin server names to seeded server artifact UUIDs.
    if server not in ("agience-core", "desktop-host") and not server.startswith("local-mcp:"):
        server = server_registry.resolve_name_to_id(server)

    try:
        mcp_service.invoke_tool(
            db=db,
            user_id=user_id,
            workspace_id=workspace_id,
            server_artifact_id=server,
            tool_name=tool_name,
            arguments=tool_args,
        )
    except Exception:
        logger.exception(
            "Transform %s failed via server %s tool %s",
            transform_artifact_id,
            server,
            tool_name,
        )
        raise


def _execute_action(
    action: Dict[str, Any],
    *,
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    source_artifact_id: str,
    event_type: str,
    all_artifacts: Iterable[Any],
    source_ctx: Optional[Dict[str, Any]] = None,
    collection_db: Optional[StandardDatabase] = None,
) -> None:
    action_type = (action.get("type") or "").strip()

    if action_type == "invoke_transform":
        explicit_transform_id = action.get("transform_artifact_id") or action.get("artifact_id")
        selector = action.get("selector") if isinstance(action.get("selector"), dict) else None
        transform_artifact, transform_ctx = _resolve_transform_artifact(
            all_artifacts,
            source_ctx=source_ctx,
            explicit_transform_id=str(explicit_transform_id) if explicit_transform_id else None,
            selector=selector,
        )
        if transform_artifact is None or transform_ctx is None:
            logger.info("No transform matched source artifact %s for event %s", source_artifact_id, event_type)
            return
        _invoke_transform(
            transform_ctx,
            transform_artifact_id=str(getattr(transform_artifact, "id", "")),
            db=db,
            user_id=user_id,
            workspace_id=workspace_id,
            source_artifact_id=source_artifact_id,
            collection_db=collection_db,
        )
        return

    if action_type != "invoke_operator":
        logger.info("Skipping unsupported action type '%s'", action_type)
        return

    operator_name = (action.get("operator") or "").strip()
    if not operator_name:
        return

    server_tool = _OPERATOR_TO_SERVER.get(operator_name)
    if not server_tool:
        logger.warning("Handler references unknown operator '%s' (no server mapping)", operator_name)
        return

    server, tool = server_tool

    # _OPERATOR_TO_SERVER lookup yields a persona name; resolve to
    # the seeded server artifact UUID so mcp_service.invoke_tool dispatches
    # via the artifact-native path.
    if server not in ("agience-core", "desktop-host") and not server.startswith("local-mcp:"):
        server = server_registry.resolve_name_to_id(server)

    # Build params with injected variables
    raw_params = action.get("operator_params") or {}
    if not isinstance(raw_params, dict):
        raw_params = {}

    variables = {
        "workspace_id": workspace_id,
        "artifact_id": source_artifact_id,
        "event_type": event_type,
    }
    params = _replace_templates(raw_params, variables)
    params["workspace_id"] = workspace_id
    params["source_artifact_id"] = source_artifact_id
    params["artifact_id"] = source_artifact_id

    try:
        mcp_service.invoke_tool(
            db=db,
            user_id=user_id,
            workspace_id=workspace_id,
            server_artifact_id=server,
            tool_name=tool,
            arguments=params,
        )
    except Exception:
        logger.exception(
            "invoke_operator '%s' → %s:%s failed for artifact %s",
            operator_name, server, tool, source_artifact_id,
        )
        raise


def dispatch_workspace_event(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    event_type: str,
    source_artifact_id: str,
    source_artifact=None,
    collection_db: Optional[StandardDatabase] = None,
) -> None:
    """Evaluate artifact-based workspace event handlers for *event_type*.

    Scans all workspace artifacts for handlers, matches against the event and
    source artifact, and executes each matched handler's actions via MCP dispatch.
    """
    from db import arango as arango_db_module

    arango_db: StandardDatabase = collection_db or next(_get_arango_db())
    all_artifact_dicts = arango_db_module.list_collection_artifacts(arango_db, workspace_id)

    # Wrap dicts as SimpleNamespace for attribute access compatibility
    from types import SimpleNamespace
    all_artifacts = [SimpleNamespace(**d) for d in all_artifact_dicts]

    source_ctx: Optional[Dict[str, Any]] = None
    if source_artifact is not None:
        source_ctx = _parse_context(source_artifact)

    try:
        _dispatch_contract_event(
            db=arango_db,
            user_id=user_id,
            workspace_id=workspace_id,
            event_type=event_type,
            source_artifact_id=source_artifact_id,
            source_ctx=source_ctx,
        )
    except Exception as exc:
        logger.warning(
            "Contract event dispatch failed for event %s artifact %s: %s",
            event_type,
            source_artifact_id,
            exc,
        )

    for artifact in all_artifacts:
        ctx = _parse_context(artifact)
        if ctx is None:
            continue
        if not _is_handler(ctx):
            continue

        if source_artifact is not None and getattr(source_artifact, "id", None) == getattr(artifact, "id", None):
            continue
        if source_ctx and source_ctx.get("type") == "workspace-event-handler":
            continue

        if not _matches_event_types(ctx, event_type):
            continue
        if not _matches_source(ctx, source_ctx):
            continue

        actions = ctx.get("actions")
        if not isinstance(actions, list):
            continue

        for action in actions:
            if not isinstance(action, dict):
                continue
            try:
                _execute_action(
                    action,
                    db=arango_db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    source_artifact_id=source_artifact_id,
                    event_type=event_type,
                    all_artifacts=all_artifacts,
                    source_ctx=source_ctx,
                    collection_db=collection_db,
                )
            except Exception as exc:
                logger.warning(
                    "Handler %s action failed for artifact %s: %s",
                    getattr(artifact, "id", "?"),
                    source_artifact_id,
                    exc,
                )
