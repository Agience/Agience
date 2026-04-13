from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TypeResolutionResult:
    content_type: str
    definition: Dict[str, Any]
    sources: List[str]
    validation_errors: List[str]


def _normalize_content_type(content_type: str) -> str:
    content_type = (content_type or "").strip().lower()
    if not content_type:
        return ""
    # Drop parameters like charset.
    if ";" in content_type:
        content_type = content_type.split(";", 1)[0].strip()
    return content_type


def _repo_root() -> Path:
    # backend/services/types_service.py -> backend -> repo root
    return Path(__file__).resolve().parents[2]


def _builtin_types_root() -> Path:
    return _repo_root() / "types"


def _default_server_ui_roots() -> List[Path]:
    servers_root = _repo_root() / "servers"
    if not servers_root.exists() or not servers_root.is_dir():
        return []

    roots: List[Path] = []
    for server_dir in sorted(servers_root.iterdir(), key=lambda path: path.name.lower()):
        if not server_dir.is_dir() or server_dir.name.startswith("."):
            continue
        candidate = server_dir / "ui"
        if candidate.exists() and candidate.is_dir():
            roots.append(candidate)
    return roots


def get_types_roots() -> List[Path]:
    """Return search roots for the folder-based type system.

    Order matters: earlier roots take precedence.

    Env vars:
    - AGIENCE_TYPES_PATHS: extra roots (os.pathsep-separated)
    - AGIENCE_TYPES_DISABLE_BUILTIN: if truthy, do not include repo `types/`
    """
    roots: List[Path] = []
    seen: set[Path] = set()

    def add_root(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        roots.append(resolved)

    extra = os.getenv("AGIENCE_TYPES_PATHS", "")
    if extra:
        for raw in extra.split(os.pathsep):
            raw = (raw or "").strip()
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = _repo_root() / p
            if p.exists() and p.is_dir():
                add_root(p)

    disable_builtin = os.getenv("AGIENCE_TYPES_DISABLE_BUILTIN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    if not disable_builtin:
        builtin = _builtin_types_root()
        if builtin.exists() and builtin.is_dir():
            add_root(builtin)

    for server_root in _default_server_ui_roots():
        add_root(server_root)

    return roots


def _content_type_to_rel_folder(content_type: str) -> Optional[Path]:
    content_type = _normalize_content_type(content_type)
    if not content_type or "/" not in content_type:
        return None
    top, sub = content_type.split("/", 1)
    if sub == "*":
        sub = "_wildcard"
    return Path(top) / sub


def _find_type_folder(roots: Iterable[Path], content_type: str) -> Optional[Tuple[Path, str]]:
    """Return (folder_path, source_label) for the single registered definition.

    Each content type must be registered in exactly one root. If two or more
    roots declare the same type the registration is ambiguous — this is a
    configuration error and resolution fails so the problem is surfaced early
    rather than silently resolved by alphabetical ordering.
    """
    rel = _content_type_to_rel_folder(content_type)
    if rel is None:
        return None

    matches: List[Tuple[Path, str]] = []
    for root in roots:
        candidate = root / rel
        if candidate.exists() and candidate.is_dir():
            matches.append((candidate, str(candidate)))

    if len(matches) == 0:
        return None
    if len(matches) == 1:
        return matches[0]

    paths = ", ".join(str(m[0]) for m in matches)
    logger.error(
        "Type '%s' is registered in multiple roots — this is a configuration error. "
        "Each type must have exactly one definition. Conflicting locations: %s",
        content_type,
        paths,
    )
    return None


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_handlers(handlers_dir: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not handlers_dir.exists() or not handlers_dir.is_dir():
        return out

    for file in sorted(handlers_dir.glob("*.json")):
        obj = _read_json_file(file)
        if not isinstance(obj, dict):
            continue
        cap = obj.get("capability")
        if isinstance(cap, str) and cap:
            out[cap] = obj
        else:
            # Fallback to filename stem.
            out[file.stem] = obj
    return out


def _deep_merge(parent: Any, child: Any) -> Any:
    """Deterministic merge: objects recurse, child wins; lists replaced by child."""
    if isinstance(parent, dict) and isinstance(child, dict):
        merged = dict(parent)
        for k, v in child.items():
            if k in merged:
                merged[k] = _deep_merge(merged[k], v)
            else:
                merged[k] = v
        return merged
    return child


def _resolve_handler_target(handler_obj: Dict[str, Any]) -> Optional[str]:
    """Resolve a callable target from a handler contract object.

    Supports both current and draft forms:
    - {"tool": "extract_text"}
    - {"implementation": {"kind": "builtin", "id": "text.extract_text"}}
    - {"implementation": {"kind": "mcp-tool", "tool": "extract_text"}}
    """
    if not isinstance(handler_obj, dict):
        return None

    direct_tool = handler_obj.get("tool")
    if isinstance(direct_tool, str) and direct_tool.strip():
        return direct_tool.strip()

    impl = handler_obj.get("implementation")
    if not isinstance(impl, dict):
        return None

    # Builtin handlers typically use `id`, while tool handlers may use `tool`.
    for key in ("tool", "id"):
        value = impl.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_handler_binding(handler_obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Resolve handler binding metadata from a handler contract object.

    Returns a mapping with:
    - tool: required target identifier
    - server_artifact_id: optional MCP server artifact id/name
    """
    tool = _resolve_handler_target(handler_obj)
    if not tool:
        return None

    binding: Dict[str, str] = {"tool": tool}

    # Allow server declaration at the handler root.
    root_server = handler_obj.get("server") or handler_obj.get("server_artifact_id")
    if isinstance(root_server, str) and root_server.strip():
        binding["server_artifact_id"] = root_server.strip()

    # Allow server declaration inside implementation.
    impl = handler_obj.get("implementation")
    if isinstance(impl, dict):
        impl_server = impl.get("server") or impl.get("server_artifact_id")
        if isinstance(impl_server, str) and impl_server.strip():
            binding["server_artifact_id"] = impl_server.strip()

    return binding


def _load_folder_definition(folder: Path) -> Tuple[Dict[str, Any], List[str]]:
    sources = [str(folder)]

    type_json = _read_json_file(folder / "type.json") or {}
    schema_json = _read_json_file(folder / "schema.json")
    preview_json = _read_json_file(folder / "preview.json")
    behaviors_json = _read_json_file(folder / "behaviors.json")
    handlers = _load_handlers(folder / "handlers")

    # UI metadata lives inside type.json["ui"] (merged format).
    ui_json = type_json.pop("ui", None)
    # `operations` is a top-level block under type.json (Phase 0 — Enterprise
    # Eventing refactor). Promote it so resolve_operation() can find it at
    # `definition["operations"]` without digging through `definition["type"]`.
    operations_json = type_json.pop("operations", None)

    definition: Dict[str, Any] = {
        "type": type_json,
        "handlers": handlers,
    }
    if schema_json is not None:
        definition["schema"] = schema_json
    if ui_json is not None:
        definition["ui"] = ui_json
    if operations_json is not None:
        definition["operations"] = operations_json
    if preview_json is not None:
        definition["preview"] = preview_json
    if behaviors_json is not None:
        definition["behaviors"] = behaviors_json

    return definition, sources


def _collect_type_validation_errors(definition: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    handlers = definition.get("handlers")
    if not isinstance(handlers, dict):
        handlers = {}

    behaviors = definition.get("behaviors")
    if not isinstance(behaviors, dict):
        return errors

    events = behaviors.get("events")
    if events is None:
        return errors
    if not isinstance(events, dict):
        return ["behaviors.events must be an object"]

    for event_name, event_obj in events.items():
        if not isinstance(event_name, str) or not event_name.strip():
            errors.append("behaviors.events keys must be non-empty strings")
            continue
        if not isinstance(event_obj, dict):
            errors.append(f"behaviors.events.{event_name} must be an object")
            continue

        has_tool = isinstance(event_obj.get("tool"), str) and bool(str(event_obj.get("tool")).strip())
        has_handler = isinstance(event_obj.get("handler"), str) and bool(str(event_obj.get("handler")).strip())

        if has_tool and has_handler:
            errors.append(f"behaviors.events.{event_name} must declare only one of 'tool' or 'handler'")
        if not has_tool and not has_handler:
            errors.append(f"behaviors.events.{event_name} must declare either 'tool' or 'handler'")

        for server_key in ("server", "server_artifact_id"):
            if server_key in event_obj and not (
                isinstance(event_obj.get(server_key), str) and str(event_obj.get(server_key)).strip()
            ):
                errors.append(f"behaviors.events.{event_name}.{server_key} must be a non-empty string")

        if has_handler:
            cap = Path(str(event_obj.get("handler"))).stem
            handler_obj = handlers.get(cap)
            if not isinstance(handler_obj, dict):
                errors.append(
                    f"behaviors.events.{event_name}.handler references missing handler '{cap}'"
                )
                continue
            binding = _resolve_handler_binding(handler_obj)
            if not isinstance(binding, dict) or not binding.get("tool"):
                errors.append(f"handlers.{cap} does not define a valid callable target")

    return errors


def resolve_type_definition(content_type: str, *, roots: Optional[List[Path]] = None) -> Optional[TypeResolutionResult]:
    """Resolve a type definition for a content type by folder convention.

    Matching order:
    1) exact `top/subtype`
    2) wildcard `top/_wildcard`

    Inheritance is applied via `type.json` -> `inherits` (e.g. ["text/*"]).
    """
    content_type = _normalize_content_type(content_type)
    if not content_type:
        return None

    roots = roots if roots is not None else get_types_roots()

    match = _find_type_folder(roots, content_type)
    if match is None and "/" in content_type:
        top, _sub = content_type.split("/", 1)
        match = _find_type_folder(roots, f"{top}/*")

    if match is None:
        return None

    folder, _source = match
    base_def, sources = _load_folder_definition(folder)

    type_obj = base_def.get("type") if isinstance(base_def.get("type"), dict) else {}
    inherits = type_obj.get("inherits") if isinstance(type_obj, dict) else None

    merged = base_def
    if isinstance(inherits, list) and inherits:
        # Apply parents in order; child overrides.
        for parent_ct in inherits:
            if not isinstance(parent_ct, str):
                continue
            parent_res = resolve_type_definition(parent_ct, roots=roots)
            if parent_res is None:
                continue
            sources = parent_res.sources + sources
            merged = _deep_merge(parent_res.definition, merged)

    validation_errors = _collect_type_validation_errors(merged)
    if validation_errors:
        logger.warning("Type '%s' has %d contract validation error(s)", content_type, len(validation_errors))

    return TypeResolutionResult(
        content_type=content_type,
        definition=merged,
        sources=sources,
        validation_errors=validation_errors,
    )


def resolve_capability_target(
    content_type: str,
    capability: str,
    *,
    roots: Optional[List[Path]] = None,
) -> Optional[str]:
    """Resolve the declared target for a type capability.

    Returns a target name suitable for runtime invocation.
    For builtins this is typically a builtin id (e.g. `text.extract_text`),
    for remote handlers this is typically a tool name.
    """
    if not capability:
        return None

    res = resolve_type_definition(content_type, roots=roots)
    if res is None:
        return None

    handlers = res.definition.get("handlers")
    if not isinstance(handlers, dict):
        return None

    handler_obj = handlers.get(capability)
    if not isinstance(handler_obj, dict):
        return None

    return _resolve_handler_target(handler_obj)


def resolve_event_target(
    content_type: str,
    event_name: str,
    *,
    roots: Optional[List[Path]] = None,
) -> Optional[str]:
    """Resolve a declared target for a lifecycle event.

    Supports:
    - Draft event contract: behaviors.events.<event>.tool
    - Existing file-ref form: behaviors.events.<event>.handler -> handlers/<capability>.json
    """
    binding = resolve_event_binding(content_type, event_name, roots=roots)
    if not isinstance(binding, dict):
        return None
    tool = binding.get("tool")
    return tool if isinstance(tool, str) and tool.strip() else None


def resolve_event_binding(
    content_type: str,
    event_name: str,
    *,
    roots: Optional[List[Path]] = None,
) -> Optional[Dict[str, str]]:
    """Resolve a lifecycle event binding (tool + optional server)."""
    if not event_name:
        return None

    res = resolve_type_definition(content_type, roots=roots)
    if res is None:
        return None

    behaviors = res.definition.get("behaviors")
    if not isinstance(behaviors, dict):
        return None

    events = behaviors.get("events")
    if not isinstance(events, dict):
        return None

    event_obj = events.get(event_name)
    if not isinstance(event_obj, dict):
        return None

    direct_tool = event_obj.get("tool")
    if isinstance(direct_tool, str) and direct_tool.strip():
        binding: Dict[str, str] = {"tool": direct_tool.strip()}
        direct_server = event_obj.get("server") or event_obj.get("server_artifact_id")
        if isinstance(direct_server, str) and direct_server.strip():
            binding["server_artifact_id"] = direct_server.strip()
        return binding

    handler_ref = event_obj.get("handler")
    if not isinstance(handler_ref, str) or not handler_ref.strip():
        return None

    # Common form: "handlers/open.json" -> capability "open"
    cap = Path(handler_ref).stem
    handlers = res.definition.get("handlers")
    if not isinstance(handlers, dict):
        return None
    handler_obj = handlers.get(cap)
    if not isinstance(handler_obj, dict):
        return None

    # Event-level server (if provided) overrides handler-level server.
    binding = _resolve_handler_binding(handler_obj)
    if not binding:
        return None

    event_server = event_obj.get("server") or event_obj.get("server_artifact_id")
    if isinstance(event_server, str) and event_server.strip():
        binding["server_artifact_id"] = event_server.strip()

    return binding


# ---------------------------------------------------------------------------
# Operations schema (Phase 0 — Enterprise Eventing refactor)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperationSpec:
    """Normalized view of an `operations.{op_name}` entry in type.json."""

    name: str
    enabled: bool
    requires_grant: str
    dispatch: Dict[str, Any]
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    emits: List[Dict[str, Any]]
    observe: Optional[Dict[str, Any]]
    audit: bool


_OP_NAME_TO_GRANT_FLAG = {
    "create": "create",
    "read": "read",
    "update": "update",
    "delete": "delete",
    "invoke": "invoke",
    "add": "add",
    "search": "search",
    "own": "own",
}


# Process-wide type resolution cache. Keyed by content type. Cleared via invalidate_type_cache().
_type_cache: Dict[str, Optional[TypeResolutionResult]] = {}


def resolve_type_definition_cached(content_type: str) -> Optional[TypeResolutionResult]:
    """Cached variant of `resolve_type_definition` using default roots.

    The cache is in-process and survives until `invalidate_type_cache()` is
    called (e.g. on an admin reload). Tests should call `invalidate_type_cache()`
    in their setup if they mutate type files.
    """
    key = _normalize_content_type(content_type)
    if key in _type_cache:
        return _type_cache[key]
    res = resolve_type_definition(key)
    _type_cache[key] = res
    return res


def invalidate_type_cache() -> None:
    """Clear the cached type resolutions. Call after hot-reloading type files."""
    _type_cache.clear()


def resolve_operation(content_type: str, op_name: str) -> Optional[OperationSpec]:
    """Resolve an operation specification for a content type + op name.

    Walks `definition["operations"][op_name]` from the (cached) type
    resolution and normalizes it into an `OperationSpec`. Returns `None` if
    the type doesn't declare the operation.
    """
    if not op_name:
        return None

    res = resolve_type_definition_cached(content_type)
    if res is None:
        return None

    ops = res.definition.get("operations")
    if not isinstance(ops, dict):
        return None

    op = ops.get(op_name)
    if not isinstance(op, dict):
        return None

    enabled = bool(op.get("enabled", True))

    requires_grant = op.get("requires_grant")
    if not isinstance(requires_grant, str) or not requires_grant.strip():
        requires_grant = _OP_NAME_TO_GRANT_FLAG.get(op_name, "read")

    dispatch = op.get("dispatch")
    if not isinstance(dispatch, dict):
        dispatch = {"kind": "artifact_crud"} if op_name in _OP_NAME_TO_GRANT_FLAG else {}

    input_schema = op.get("input_schema") if isinstance(op.get("input_schema"), dict) else {}
    output_schema = op.get("output_schema") if isinstance(op.get("output_schema"), dict) else {}

    emits_raw = op.get("emits")
    emits: List[Dict[str, Any]] = []
    if isinstance(emits_raw, list):
        for entry in emits_raw:
            if isinstance(entry, dict) and isinstance(entry.get("event"), str):
                emits.append({
                    "event": entry["event"],
                    "phase": entry.get("phase", "after"),
                    "optional": bool(entry.get("optional", False)),
                })

    observe = op.get("observe") if isinstance(op.get("observe"), dict) else None
    audit = bool(op.get("audit", False))

    return OperationSpec(
        name=op_name,
        enabled=enabled,
        requires_grant=requires_grant,
        dispatch=dispatch,
        input_schema=input_schema,
        output_schema=output_schema,
        emits=emits,
        observe=observe,
        audit=audit,
    )


def list_available_content_types(*, roots: Optional[List[Path]] = None) -> List[str]:
    """List content type patterns available across roots (exact + wildcards)."""
    roots = roots if roots is not None else get_types_roots()
    content_types: List[str] = []
    seen: set[str] = set()

    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for top in sorted([p for p in root.iterdir() if p.is_dir()]):
            for sub in sorted([p for p in top.iterdir() if p.is_dir()]):
                if sub.name.startswith("."):
                    continue
                if sub.name == "_wildcard":
                    content_type = f"{top.name}/*"
                else:
                    content_type = f"{top.name}/{sub.name}"
                if content_type not in seen:
                    seen.add(content_type)
                    content_types.append(content_type)

    return content_types
