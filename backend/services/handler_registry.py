"""Handler registry for operation dispatch.

Each registered handler implements the `OperationHandler` protocol and is
identified by a `kind` string matching the `dispatch.kind` in `type.json`.
The operation dispatcher uses `get(kind)` to look up a handler at call time.

Built-in handlers (`mcp_tool`, `native`, `artifact_crud`) are registered at
app startup in `main.py` via `register_builtin_handlers()`.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSONPath-lite resolution
# ---------------------------------------------------------------------------

def resolve_ref(
    ref: Any,
    artifact: Dict[str, Any],
    *,
    body: Optional[Dict[str, Any]] = None,
    ctx: Any = None,
) -> Any:
    """Resolve a JSONPath-lite reference against an artifact document, the
    dispatch request body, or the dispatch context.

    Supported roots:
    - `$._key`, `$.context.foo.bar` → walks the artifact document. The
      `context` field is opportunistically JSON-decoded if stored as a string.
    - `$.body.name`, `$.body.arguments.query` → walks the dispatch request body.
    - `$.ctx.user_id`, `$.ctx.actor_id` → reads named attributes off the
      `DispatchContext` object.

    Literal strings (no leading `$.`) are returned as-is. Non-string refs
    (numbers, dicts) are returned as-is. Missing paths return `None`.
    """
    if not isinstance(ref, str):
        return ref
    if not ref.startswith("$."):
        return ref

    parts = ref[2:].split(".")
    if not parts:
        return None

    first = parts[0]
    remainder = parts[1:]

    if first == "body":
        return _walk_dict(body, remainder)
    if first == "ctx":
        if ctx is None or not remainder:
            return None
        attr = remainder[0]
        cur: Any = getattr(ctx, attr, None)
        return _walk_dict(cur, remainder[1:]) if len(remainder) > 1 else cur

    # Default root: the artifact document itself. Reinclude `first` in the walk.
    return _walk_dict(artifact, parts)


def _walk_dict(root: Any, parts: list[str]) -> Any:
    """Walk a dict (or JSON-string-containing-dict) along a dotted path."""
    cur: Any = root
    for part in parts:
        if cur is None:
            return None
        if isinstance(cur, str):
            import json
            try:
                cur = json.loads(cur)
            except Exception:
                return None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class OperationHandler(Protocol):
    kind: str

    async def run(
        self,
        artifact: Dict[str, Any],
        op_spec: "OperationSpec",
        body: Dict[str, Any],
        ctx: "DispatchContext",
    ) -> Any:
        ...


# Forward reference types — defined in operation_dispatcher to avoid circular import.
# We import lazily inside run() or use Any-typed duck typing here.
OperationSpec = Any  # dispatcher's OperationSpec dataclass
DispatchContext = Any  # dispatcher's DispatchContext


# ---------------------------------------------------------------------------
# Registry state
# ---------------------------------------------------------------------------

_handlers: Dict[str, OperationHandler] = {}
_native_targets: Dict[str, Callable[..., Awaitable[Any]]] = {}


def register(kind: str, handler: OperationHandler) -> None:
    """Register a handler for a dispatch kind. Overwrites any existing entry."""
    _handlers[kind] = handler
    logger.info("Registered operation handler: %s", kind)


def get(kind: str) -> Optional[OperationHandler]:
    """Look up a handler by kind. Returns None if not registered."""
    return _handlers.get(kind)


def clear() -> None:
    """Clear all handlers (for tests)."""
    _handlers.clear()
    _native_targets.clear()


def register_native_target(name: str, func: Callable[..., Awaitable[Any]]) -> None:
    """Register a callable that the `native` handler can invoke by name."""
    _native_targets[name] = func


def get_native_target(name: str) -> Optional[Callable[..., Awaitable[Any]]]:
    """Look up a registered native callable. Falls back to importlib if
    `name` is a dotted module path and nothing is registered."""
    if name in _native_targets:
        return _native_targets[name]
    # Fallback: dotted import path (e.g. "services.auth_service.generate_api_key"
    # or short form "auth_service.generate_api_key" resolved under services/).
    if "." in name:
        mod_name, _, attr = name.rpartition(".")
        candidates = [mod_name]
        if not mod_name.startswith("services.") and not mod_name.startswith("core."):
            candidates.insert(0, f"services.{mod_name}")
        for candidate in candidates:
            try:
                mod = importlib.import_module(candidate)
                target = getattr(mod, attr, None)
                if callable(target):
                    return target
            except Exception as exc:
                logger.debug("Native target import failed for %s (%s): %s", name, candidate, exc)
    return None


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------

@dataclass
class McpToolHandler:
    """Dispatch kind `mcp_tool` — call an MCP server tool.

    `op_spec.dispatch` must contain `server_ref` and `tool_ref` (literals or
    `$.path.to.field` refs into the artifact doc).
    """

    kind: str = "mcp_tool"

    async def run(
        self,
        artifact: Dict[str, Any],
        op_spec: Any,
        body: Dict[str, Any],
        ctx: Any,
    ) -> Any:
        dispatch = op_spec.dispatch or {}
        server_id = resolve_ref(dispatch.get("server_ref"), artifact, body=body, ctx=ctx)
        tool_name = resolve_ref(dispatch.get("tool_ref"), artifact, body=body, ctx=ctx)

        if not server_id or not tool_name:
            raise ValueError(
                f"mcp_tool dispatch could not resolve server ({dispatch.get('server_ref')!r}) "
                f"or tool ({dispatch.get('tool_ref')!r}) from artifact/body/ctx"
            )

        # Lazy import to avoid circular dependency on mcp_service
        from services import mcp_service

        workspace_id = body.get("workspace_id") if isinstance(body, dict) else None
        arguments = body.get("arguments") if isinstance(body, dict) and "arguments" in body else body

        # `mcp_service.invoke_tool` is synchronous (uses httpx.Client). Run it
        # in a thread-pool executor so it does not block the asyncio event loop.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(
                mcp_service.invoke_tool,
                db=ctx.arango_db,
                user_id=ctx.user_id,
                workspace_id=workspace_id,
                server_artifact_id=str(server_id),
                tool_name=str(tool_name),
                arguments=arguments or {},
            ),
        )
        return result


@dataclass
class NativeHandler:
    """Dispatch kind `native` — call a registered Python callable by name."""

    kind: str = "native"

    async def run(
        self,
        artifact: Dict[str, Any],
        op_spec: Any,
        body: Dict[str, Any],
        ctx: Any,
    ) -> Any:
        dispatch = op_spec.dispatch or {}
        target_name = dispatch.get("target")
        if not target_name:
            raise ValueError("native dispatch missing 'target'")

        target = get_native_target(str(target_name))
        if target is None:
            raise ValueError(f"native dispatch target '{target_name}' not registered")

        result = target(artifact, body, ctx)
        if hasattr(result, "__await__"):
            return await result
        return result


@dataclass
class ArtifactCrudHandler:
    """Dispatch kind `artifact_crud` — default create/read/update/delete.

    This is a thin pass-through that lets the dispatcher wrap existing
    router→service CRUD paths in the emit envelope without re-implementing
    them. The router calls its existing service function and hands the result
    back to the dispatcher; the handler is a no-op placeholder so the
    dispatcher can still look up a handler and emit events around the call.
    """

    kind: str = "artifact_crud"

    async def run(
        self,
        artifact: Dict[str, Any],
        op_spec: Any,
        body: Dict[str, Any],
        ctx: Any,
    ) -> Any:
        # When the dispatcher is invoked with kind=artifact_crud, the caller
        # supplies a `ctx.inner_result` (already-computed service result) or
        # a `ctx.inner_callable` to run. This keeps back-compat with existing
        # router code that calls services directly.
        inner = getattr(ctx, "inner_callable", None)
        if inner is not None:
            result = inner()
            if hasattr(result, "__await__"):
                return await result
            return result
        return getattr(ctx, "inner_result", None)


def register_builtin_handlers() -> None:
    """Register the default set of dispatch handlers. Called from main.py lifespan."""
    register("mcp_tool", McpToolHandler())
    register("native", NativeHandler())
    register("artifact_crud", ArtifactCrudHandler())
