import json
from pathlib import Path

import pytest

from services import types_service


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_resolve_exact_with_inheritance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Arrange: create a minimal types tree with wildcard parent and exact child.
    root = tmp_path / "types"

    _write_json(
        root / "text" / "_wildcard" / "type.json",
        {"content_type": "text/*", "version": 1},
    )
    _write_json(
        root / "text" / "_wildcard" / "preview.json",
        {"version": 1, "icon": "file-text", "preview": {"kind": "text_excerpt"}},
    )

    _write_json(
        root / "text" / "plain" / "type.json",
        {"content_type": "text/plain", "version": 1, "inherits": ["text/*"]},
    )
    _write_json(
        root / "text" / "plain" / "preview.json",
        {"version": 1, "icon": "note", "preview": {"max_chars": 12}},
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")
    monkeypatch.setattr(types_service, "_default_server_ui_roots", lambda: [])

    # Act
    res = types_service.resolve_type_definition("text/plain; charset=utf-8")

    # Assert
    assert res is not None
    assert res.content_type == "text/plain"

    preview = res.definition.get("preview")
    assert isinstance(preview, dict)

    # icon overridden by child
    assert preview.get("icon") == "note"

    # inherited kind from parent
    assert preview.get("preview", {}).get("kind") == "text_excerpt"

    # child-only field present
    assert preview.get("preview", {}).get("max_chars") == 12


def test_resolve_falls_back_to_wildcard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "types"

    _write_json(
        root / "text" / "_wildcard" / "type.json",
        {"content_type": "text/*", "version": 1},
    )
    _write_json(
        root / "text" / "_wildcard" / "preview.json",
        {"version": 1, "icon": "file-text"},
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")
    monkeypatch.setattr(types_service, "_default_server_ui_roots", lambda: [])

    res = types_service.resolve_type_definition("text/csv")
    assert res is not None
    assert res.content_type == "text/csv"
    assert res.definition.get("preview", {}).get("icon") == "file-text"


@pytest.mark.asyncio
async def test_router_resolve_uses_env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client):
    root = tmp_path / "types"

    _write_json(
        root / "application" / "json" / "type.json",
        {"content_type": "application/json", "version": 1},
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")
    monkeypatch.setattr(types_service, "_default_server_ui_roots", lambda: [])

    resp = await client.get("/types/resolve", params={"content_type": "application/json"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["content_type"] == "application/json"
    assert data["definition"]["type"]["content_type"] == "application/json"
    assert data["validation_errors"] == []


def test_get_types_roots_includes_builtin_and_server_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    builtin_root = tmp_path / "types"
    server_root = tmp_path / "servers" / "astra" / "ui"

    _write_json(
        builtin_root / "application" / "json" / "type.json",
        {"content_type": "application/json", "version": 1},
    )
    _write_json(
        server_root / "application" / "vnd.agience.stream+json" / "type.json",
        {"content_type": "application/vnd.agience.stream+json", "version": 1},
    )

    monkeypatch.delenv("AGIENCE_TYPES_PATHS", raising=False)
    monkeypatch.delenv("AGIENCE_TYPES_DISABLE_BUILTIN", raising=False)
    monkeypatch.setattr(types_service, "_repo_root", lambda: tmp_path)

    roots = types_service.get_types_roots()

    assert roots == [builtin_root.resolve(), server_root.resolve()]


def test_resolve_capability_target_from_handler_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "types"

    _write_json(
        root / "application" / "vnd.test+json" / "type.json",
        {"content_type": "application/vnd.test+json", "version": 1},
    )
    _write_json(
        root / "application" / "vnd.test+json" / "handlers" / "extract_text.json",
        {"capability": "extract_text", "tool": "extract_text"},
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")

    target = types_service.resolve_capability_target("application/vnd.test+json", "extract_text")
    assert target == "extract_text"


def test_resolve_event_target_from_behaviors_handler_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "types"

    _write_json(
        root / "application" / "vnd.test+json" / "type.json",
        {"content_type": "application/vnd.test+json", "version": 1},
    )
    _write_json(
        root / "application" / "vnd.test+json" / "behaviors.json",
        {"version": 1, "events": {"on_commit": {"handler": "handlers/on_commit.json"}}},
    )
    _write_json(
        root / "application" / "vnd.test+json" / "handlers" / "on_commit.json",
        {
            "capability": "on_commit",
            "implementation": {"kind": "mcp-tool", "tool": "on_commit"},
        },
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")

    target = types_service.resolve_event_target("application/vnd.test+json", "on_commit")
    assert target == "on_commit"


def test_resolve_event_binding_prefers_event_server_over_handler_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "types"

    _write_json(
        root / "application" / "vnd.test+json" / "type.json",
        {"content_type": "application/vnd.test+json", "version": 1},
    )
    _write_json(
        root / "application" / "vnd.test+json" / "behaviors.json",
        {
            "version": 1,
            "events": {
                "on_commit": {
                    "handler": "handlers/on_commit.json",
                    "server": "event-server",
                }
            },
        },
    )
    _write_json(
        root / "application" / "vnd.test+json" / "handlers" / "on_commit.json",
        {
            "capability": "on_commit",
            "implementation": {
                "kind": "mcp-tool",
                "tool": "on_commit",
                "server": "handler-server",
            },
        },
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")

    binding = types_service.resolve_event_binding("application/vnd.test+json", "on_commit")
    assert binding == {"tool": "on_commit", "server_artifact_id": "event-server"}


def test_resolve_event_binding_supports_direct_tool_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "types"

    _write_json(
        root / "application" / "vnd.test+json" / "type.json",
        {"content_type": "application/vnd.test+json", "version": 1},
    )
    _write_json(
        root / "application" / "vnd.test+json" / "behaviors.json",
        {
            "version": 1,
            "events": {
                "on_commit": {
                    "tool": "direct_commit_tool",
                    "server_artifact_id": "direct-server",
                }
            },
        },
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")

    binding = types_service.resolve_event_binding("application/vnd.test+json", "on_commit")
    assert binding == {"tool": "direct_commit_tool", "server_artifact_id": "direct-server"}


def test_resolve_type_definition_reports_event_validation_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "types"

    _write_json(
        root / "application" / "vnd.invalid+json" / "type.json",
        {"content_type": "application/vnd.invalid+json", "version": 1},
    )
    _write_json(
        root / "application" / "vnd.invalid+json" / "behaviors.json",
        {
            "version": 1,
            "events": {
                "on_commit": {
                    "tool": "commit_tool",
                    "handler": "handlers/on_commit.json",
                },
                "on_publish": {
                    "handler": "handlers/missing.json",
                },
            },
        },
    )
    _write_json(
        root / "application" / "vnd.invalid+json" / "handlers" / "on_commit.json",
        {
            "capability": "on_commit",
            "implementation": {"kind": "mcp-tool", "tool": "commit_tool"},
        },
    )

    monkeypatch.setenv("AGIENCE_TYPES_PATHS", str(root))
    monkeypatch.setenv("AGIENCE_TYPES_DISABLE_BUILTIN", "1")

    res = types_service.resolve_type_definition("application/vnd.invalid+json")
    assert res is not None
    assert any("only one of 'tool' or 'handler'" in msg for msg in res.validation_errors)
    assert any("references missing handler 'missing'" in msg for msg in res.validation_errors)
