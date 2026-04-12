"""Operation dispatcher — the generic entry point for artifact operations.

Routers call `dispatch(op_name, artifact, body, ctx)` which:

1. Resolves the `OperationSpec` from the artifact's type.json via
   `types_service.resolve_operation`.
2. Enforces the grant check using existing `_check_grant_permission`.
3. Emits `phase=before` events declared in `op.emits`.
4. Looks up the handler by `op.dispatch.kind` and runs it.
5. Emits `phase=after` events on success, or `phase=error` on failure.
6. Returns the handler's result (or re-raises).

If the type has not declared `operations.{op_name}`, `dispatch()` raises
`OperationNotDeclared` and the router converts it to a 404.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException

from core import event_bus
from services import handler_registry, types_service
from services.dependencies import _check_grant_permission

logger = logging.getLogger(__name__)


class OperationNotDeclared(Exception):
    """Raised when a type does not declare the requested operation.

    Routers catching this convert it to a 404 response.
    """


class OperationNotEnabled(HTTPException):
    def __init__(self, content_type: str, op: str):
        super().__init__(status_code=403, detail=f"Operation '{op}' is not enabled on type '{content_type}'")


class OperationForbidden(HTTPException):
    def __init__(self, op: str):
        super().__init__(status_code=403, detail=f"Not authorized to '{op}' this artifact")


@dataclass
class DispatchContext:
    """Context passed to handlers during dispatch.

    Carries the caller identity (for grant checks and emit payloads), the DB
    session, and optional `inner_callable`/`inner_result` for the
    `artifact_crud` handler's pass-through pattern.
    """

    user_id: Optional[str]
    actor_id: Optional[str]
    grants: List[Any]  # List[GrantEntity]
    arango_db: Any  # StandardDatabase
    request: Any = None

    # For artifact_crud kind: router pre-calls a service and supplies the
    # result here so the dispatcher wraps it in the emit envelope.
    inner_callable: Optional[Callable[[], Any]] = None
    inner_result: Any = None

    # Filled during dispatch so handlers can optionally emit custom events.
    _during_events: List[Dict[str, Any]] = field(default_factory=list)

    def emit(self, name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Buffer a custom `phase=during` event. Flushed after `after` phase."""
        self._during_events.append({"event": name, "payload": payload or {}})


def _extract_content_type(artifact: Dict[str, Any]) -> Optional[str]:
    """Best-effort content type extraction from an artifact document."""
    if not isinstance(artifact, dict):
        return None
    ctx = artifact.get("context")
    if isinstance(ctx, str):
        try:
            import json
            ctx = json.loads(ctx)
        except Exception:
            ctx = {}
    if isinstance(ctx, dict):
        for key in ("content_type",):
            val = ctx.get(key)
            if isinstance(val, str) and val.strip():
                return val
    for key in ("content_type",):
        val = artifact.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def _extract_container_id(artifact: Dict[str, Any]) -> Optional[str]:
    """Extract container (workspace/collection) id from an artifact doc."""
    if not isinstance(artifact, dict):
        return None
    for key in ("workspace_id", "collection_id", "container_id"):
        val = artifact.get(key)
        if val:
            return str(val)
    return None


def _extract_artifact_id(artifact: Dict[str, Any]) -> Optional[str]:
    if not isinstance(artifact, dict):
        return None
    for key in ("id", "_key", "artifact_id"):
        val = artifact.get(key)
        if val:
            return str(val)
    return None


def _summarize_result(result: Any) -> Any:
    """Return a JSON-safe summary of a handler result for event payloads."""
    if result is None:
        return None
    if isinstance(result, (str, int, float, bool)):
        return result
    if isinstance(result, dict):
        # Keep keys but truncate long string values
        out = {}
        for k, v in result.items():
            if isinstance(v, str) and len(v) > 200:
                out[k] = v[:200] + "..."
            elif isinstance(v, (dict, list)):
                out[k] = f"<{type(v).__name__} len={len(v)}>"
            else:
                out[k] = v
        return out
    if isinstance(result, list):
        return f"<list len={len(result)}>"
    return str(type(result).__name__)


async def _emit_phase(
    phase: str,
    op_spec: Any,
    artifact: Dict[str, Any],
    ctx: DispatchContext,
    *,
    result: Any = None,
    error: Optional[Exception] = None,
    body: Optional[Dict[str, Any]] = None,
) -> None:
    """Publish all events declared in op_spec.emits for the given phase."""
    if not op_spec.emits:
        return

    artifact_id = _extract_artifact_id(artifact)
    container_id = _extract_container_id(artifact)
    content_type = _extract_content_type(artifact)

    for emit_entry in op_spec.emits:
        if emit_entry.get("phase") != phase:
            continue

        payload: Dict[str, Any] = {
            "artifact_id": artifact_id,
            "container_id": container_id,
            "content_type": content_type,
            "op": op_spec.name,
            "phase": phase,
            "actor_id": ctx.actor_id or ctx.user_id,
            "ts": time.time(),
        }

        if phase == "after" and result is not None:
            payload["result"] = _summarize_result(result)
        if phase == "error" and error is not None:
            payload["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        if op_spec.audit and body is not None:
            payload["request"] = body
            if phase == "after":
                payload["response"] = result

        event = event_bus.Event(
            name=emit_entry["event"],
            payload=payload,
            container_id=container_id,
            artifact_id=artifact_id,
            content_type=content_type,
            actor_id=ctx.actor_id or ctx.user_id,
        )
        try:
            await event_bus.publish_event(event)
        except Exception as exc:
            if not emit_entry.get("optional", False):
                logger.warning(
                    "Failed to publish %s event (op=%s, phase=%s): %s",
                    emit_entry["event"], op_spec.name, phase, exc,
                )


async def dispatch(
    op_name: str,
    artifact: Dict[str, Any],
    body: Dict[str, Any],
    ctx: DispatchContext,
    *,
    content_type_override: Optional[str] = None,
) -> Any:
    """Run an operation through the emit envelope.

    Raises `OperationNotDeclared` if the type has not declared the operation
    (callers convert to 404). Raises `HTTPException` for authorization or
    enablement failures.
    """
    content_type = content_type_override or _extract_content_type(artifact)
    if not content_type:
        raise OperationNotDeclared(f"Cannot resolve content type for operation '{op_name}'")

    op_spec = types_service.resolve_operation(content_type, op_name)
    if op_spec is None:
        raise OperationNotDeclared(f"Type '{content_type}' has no declared operation '{op_name}'")

    if not op_spec.enabled:
        raise OperationNotEnabled(content_type, op_name)

    # Grant check — scoped to this artifact where possible.
    artifact_id = _extract_artifact_id(artifact)
    if not _check_grant_permission(
        ctx.grants or [],
        op_spec.requires_grant,
        resource_id=artifact_id,
    ):
        # Fall back to unscoped check (user holds the permission somewhere)
        if not _check_grant_permission(ctx.grants or [], op_spec.requires_grant):
            raise OperationForbidden(op_name)

    # Resolve handler
    handler = handler_registry.get(op_spec.dispatch.get("kind", "artifact_crud"))
    if handler is None:
        raise HTTPException(
            status_code=500,
            detail=f"No handler registered for dispatch kind '{op_spec.dispatch.get('kind')}'",
        )

    # Phase: before
    await _emit_phase("before", op_spec, artifact, ctx, body=body)

    # Execute
    try:
        result = await handler.run(artifact, op_spec, body, ctx)
    except HTTPException:
        await _emit_phase("error", op_spec, artifact, ctx, error=HTTPException, body=body)
        raise
    except Exception as exc:
        logger.error("Dispatch failed for %s.%s: %s\n%s",
                     content_type, op_name, exc, traceback.format_exc())
        await _emit_phase("error", op_spec, artifact, ctx, error=exc, body=body)
        raise

    # Flush any `during` events the handler buffered
    if ctx._during_events:
        artifact_id = _extract_artifact_id(artifact)
        container_id = _extract_container_id(artifact)
        for e in ctx._during_events:
            await event_bus.publish_event(event_bus.Event(
                name=e["event"],
                payload={
                    "artifact_id": artifact_id,
                    "container_id": container_id,
                    "content_type": content_type,
                    "op": op_name,
                    "phase": "during",
                    "actor_id": ctx.actor_id or ctx.user_id,
                    "ts": time.time(),
                    **e["payload"],
                },
                container_id=container_id,
                artifact_id=artifact_id,
                content_type=content_type,
                actor_id=ctx.actor_id or ctx.user_id,
            ))
        ctx._during_events.clear()

    # Phase: after
    await _emit_phase("after", op_spec, artifact, ctx, result=result, body=body)

    return result
