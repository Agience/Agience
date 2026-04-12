"""Unit tests for operation dispatcher primitives.

Covers:
- event_bus Event / EventFilter matching
- event_bus unified publish
- types_service.resolve_operation normalization
- handler_registry ref resolution
- operation_dispatcher emit envelope (before/after/error ordering,
  OperationNotDeclared → 404, grant check)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `backend/` importable when running pytest from repo root or backend/
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core import event_bus  # noqa: E402
from services import handler_registry, types_service  # noqa: E402
from services.operation_dispatcher import (  # noqa: E402
    DispatchContext,
    OperationNotDeclared,
    dispatch,
)


# ---------------------------------------------------------------------------
# event_bus
# ---------------------------------------------------------------------------

def test_event_filter_matches_by_container_and_name():
    f = event_bus.EventFilter(container_id="w1", event_names=["artifact.invoke.*"])
    e1 = event_bus.Event(name="artifact.invoke.started", payload={}, container_id="w1")
    e2 = event_bus.Event(name="artifact.invoke.started", payload={}, container_id="w2")
    e3 = event_bus.Event(name="artifact.created", payload={}, container_id="w1")
    assert f.matches(e1)
    assert not f.matches(e2)
    assert not f.matches(e3)


def test_event_filter_empty_matches_all():
    f = event_bus.EventFilter()
    e = event_bus.Event(name="x", payload={}, container_id="anything")
    assert f.matches(e)


@pytest.mark.asyncio
async def test_publish_event_fans_out_to_matching_subscribers():
    event_bus._filtered_subscribers.clear()
    q_all = await event_bus.subscribe_filtered(event_bus.EventFilter())
    q_w1 = await event_bus.subscribe_filtered(
        event_bus.EventFilter(container_id="w1")
    )
    q_invoke = await event_bus.subscribe_filtered(
        event_bus.EventFilter(event_names=["artifact.invoke.*"])
    )

    await event_bus.publish_event(event_bus.Event(
        name="artifact.invoke.completed", payload={"ok": True}, container_id="w1"
    ))

    # q_all and q_w1 and q_invoke all match
    assert q_all.qsize() == 1
    assert q_w1.qsize() == 1
    assert q_invoke.qsize() == 1

    await event_bus.publish_event(event_bus.Event(
        name="artifact.created", payload={}, container_id="w2"
    ))
    assert q_all.qsize() == 2
    assert q_w1.qsize() == 1  # container mismatch
    assert q_invoke.qsize() == 1  # name mismatch

    await event_bus.unsubscribe_filtered(q_all)
    await event_bus.unsubscribe_filtered(q_w1)
    await event_bus.unsubscribe_filtered(q_invoke)


@pytest.mark.asyncio
async def test_emit_artifact_event_extracts_fields_from_payload():
    """The service-layer convenience helper takes a container_id, event name,
    and `{artifact: {...}}` dict and publishes an Event with artifact_id /
    content_type extracted from the payload.

    This test exercises the field-extraction logic directly by building an
    Event via `_extract_artifact_fields` + `publish_event`, avoiding the
    `run_coroutine_threadsafe` path used by the sync variant (which is
    non-deterministic to wait on from inside the same loop).
    """
    event_bus._filtered_subscribers.clear()

    q = await event_bus.subscribe_filtered(event_bus.EventFilter(container_id="ws-1"))

    data = {
        "artifact": {
            "id": "a1",
            "context": {"content_type": "application/vnd.agience.workspace+json"},
        }
    }
    artifact_id, content_type = event_bus._extract_artifact_fields(data)
    assert artifact_id == "a1"
    assert content_type == "application/vnd.agience.workspace+json"

    await event_bus.publish_event(event_bus.Event(
        name="artifact.created",
        payload=data,
        container_id="ws-1",
        artifact_id=artifact_id,
        content_type=content_type,
    ))

    assert q.qsize() == 1
    evt = q.get_nowait()
    assert evt.name == "artifact.created"
    assert evt.container_id == "ws-1"
    assert evt.artifact_id == "a1"
    assert evt.content_type == "application/vnd.agience.workspace+json"

    await event_bus.unsubscribe_filtered(q)


def test_emit_artifact_event_sync_noop_when_loop_missing():
    """`emit_artifact_event_sync` is a no-op (rather than raising) when the
    event loop has not been captured yet — e.g. during early bootstrap before
    `set_event_loop` has been called."""
    original_loop = event_bus._loop
    event_bus._loop = None
    try:
        # Should not raise even though no loop is registered.
        event_bus.emit_artifact_event_sync(
            "ws-1",
            "artifact.created",
            {"artifact": {"id": "a1"}},
        )
    finally:
        event_bus._loop = original_loop


# ---------------------------------------------------------------------------
# types_service.resolve_operation
# ---------------------------------------------------------------------------

def test_resolve_operation_returns_none_when_not_declared(monkeypatch):
    fake_def = types_service.TypeResolutionResult(
        content_type="application/vnd.test+json",
        definition={"type": {}},
        sources=[],
        validation_errors=[],
    )
    monkeypatch.setattr(
        types_service,
        "resolve_type_definition",
        lambda content_type, roots=None: fake_def,
    )
    types_service.invalidate_type_cache()
    assert types_service.resolve_operation("application/vnd.test+json", "invoke") is None


def test_resolve_operation_normalizes_missing_fields(monkeypatch):
    fake_def = types_service.TypeResolutionResult(
        content_type="application/vnd.test+json",
        definition={
            "operations": {
                "invoke": {
                    "enabled": True,
                    "dispatch": {"kind": "mcp_tool", "server_ref": "S", "tool_ref": "T"},
                    "emits": [
                        {"event": "x.started", "phase": "before"},
                        {"event": "x.completed"},  # default phase = after
                        "bogus",  # ignored
                    ],
                }
            }
        },
        sources=[],
        validation_errors=[],
    )
    monkeypatch.setattr(
        types_service,
        "resolve_type_definition",
        lambda content_type, roots=None: fake_def,
    )
    types_service.invalidate_type_cache()

    op = types_service.resolve_operation("application/vnd.test+json", "invoke")
    assert op is not None
    assert op.enabled is True
    assert op.requires_grant == "invoke"  # defaulted from op name
    assert op.dispatch["kind"] == "mcp_tool"
    assert len(op.emits) == 2
    assert op.emits[0]["event"] == "x.started"
    assert op.emits[0]["phase"] == "before"
    assert op.emits[1]["phase"] == "after"


# ---------------------------------------------------------------------------
# handler_registry.resolve_ref
# ---------------------------------------------------------------------------

def test_resolve_ref_literal_and_path():
    artifact = {
        "context": {"server_artifact_id": "srv-1", "tool_name": "do_thing"},
        "_key": "art-1",
    }
    assert handler_registry.resolve_ref("$.context.server_artifact_id", artifact) == "srv-1"
    assert handler_registry.resolve_ref("$._key", artifact) == "art-1"
    assert handler_registry.resolve_ref("literal", artifact) == "literal"
    assert handler_registry.resolve_ref("$.nope.missing", artifact) is None


def test_resolve_ref_decodes_stringified_context():
    artifact = {"context": '{"server_artifact_id": "srv-2"}'}
    assert handler_registry.resolve_ref("$.context.server_artifact_id", artifact) == "srv-2"


# ---------------------------------------------------------------------------
# Phase 7A: body refs + ctx refs
# ---------------------------------------------------------------------------

def test_resolve_ref_body_root_walks_request_body():
    body = {"name": "search", "arguments": {"query": "hello"}}
    assert handler_registry.resolve_ref("$.body.name", {}, body=body) == "search"
    assert handler_registry.resolve_ref("$.body.arguments.query", {}, body=body) == "hello"
    assert handler_registry.resolve_ref("$.body.missing", {}, body=body) is None


def test_resolve_ref_body_returns_none_when_body_missing():
    assert handler_registry.resolve_ref("$.body.name", {}) is None
    assert handler_registry.resolve_ref("$.body.name", {}, body=None) is None


def test_resolve_ref_ctx_reads_dispatch_context_attribute():
    class _Ctx:
        user_id = "u1"
        actor_id = "u1"
    assert handler_registry.resolve_ref("$.ctx.user_id", {}, ctx=_Ctx()) == "u1"
    assert handler_registry.resolve_ref("$.ctx.missing", {}, ctx=_Ctx()) is None
    assert handler_registry.resolve_ref("$.ctx.user_id", {}) is None


def test_resolve_ref_artifact_root_still_works():
    """Existing artifact-root behavior must not regress."""
    artifact = {"_key": "art-1", "context": {"run": {"server": "astra", "tool": "ingest_text"}}}
    assert handler_registry.resolve_ref("$._key", artifact) == "art-1"
    assert handler_registry.resolve_ref("$.context.run.server", artifact) == "astra"
    assert handler_registry.resolve_ref("$.context.run.tool", artifact) == "ingest_text"


# ---------------------------------------------------------------------------
# operation_dispatcher emit envelope
# ---------------------------------------------------------------------------

class _RecordingHandler:
    kind = "recording"

    def __init__(self, result=None, raise_exc=None):
        self.result = result or {"ok": True}
        self.raise_exc = raise_exc
        self.called = False

    async def run(self, artifact, op_spec, body, ctx):
        self.called = True
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class _FakeGrant:
    def __init__(self, **flags):
        self.can_read = flags.get("read", False)
        self.can_update = flags.get("update", False)
        self.can_invoke = flags.get("invoke", False)
        self.can_create = flags.get("create", False)
        self.can_delete = flags.get("delete", False)
        self.can_add = flags.get("add", False)
        self.can_search = flags.get("search", False)
        self.can_own = flags.get("own", False)
        self.resource_id = flags.get("resource_id")
        self.resource_type = flags.get("resource_type")


def _make_op_spec(emits=None, dispatch_kind="recording"):
    return types_service.OperationSpec(
        name="invoke",
        enabled=True,
        requires_grant="invoke",
        dispatch={"kind": dispatch_kind},
        input_schema={},
        output_schema={},
        emits=emits or [
            {"event": "artifact.invoke.started", "phase": "before", "optional": False},
            {"event": "artifact.invoke.completed", "phase": "after", "optional": False},
            {"event": "artifact.invoke.failed", "phase": "error", "optional": False},
        ],
        observe=None,
        audit=False,
    )


@pytest.mark.asyncio
async def test_dispatch_raises_not_declared_when_type_missing(monkeypatch):
    monkeypatch.setattr(types_service, "resolve_operation", lambda m, o: None)
    artifact = {"context": {"content_type": "application/vnd.foo+json"}}
    ctx = DispatchContext(user_id="u1", actor_id="u1", grants=[], arango_db=None)
    with pytest.raises(OperationNotDeclared):
        await dispatch("invoke", artifact, {}, ctx)


@pytest.mark.asyncio
async def test_dispatch_emits_before_and_after_on_success(monkeypatch):
    handler_registry.clear()
    handler = _RecordingHandler(result={"value": 42})
    handler_registry.register("recording", handler)

    monkeypatch.setattr(
        types_service, "resolve_operation",
        lambda m, o: _make_op_spec(),
    )

    event_bus._filtered_subscribers.clear()
    q = await event_bus.subscribe_filtered(
        event_bus.EventFilter(event_names=["artifact.invoke.*"])
    )

    artifact = {
        "_key": "a1",
        "workspace_id": "w1",
        "context": {"content_type": "application/vnd.agience.operator+json"},
    }
    ctx = DispatchContext(
        user_id="u1",
        actor_id="u1",
        grants=[_FakeGrant(invoke=True)],
        arango_db=None,
    )

    result = await dispatch("invoke", artifact, {"input": "hi"}, ctx)
    assert result == {"value": 42}
    assert handler.called

    # before + after (no error)
    assert q.qsize() == 2
    started = q.get_nowait()
    completed = q.get_nowait()
    assert started.name == "artifact.invoke.started"
    assert started.payload["phase"] == "before"
    assert completed.name == "artifact.invoke.completed"
    assert completed.payload["phase"] == "after"
    assert completed.payload["result"] == {"value": 42}

    await event_bus.unsubscribe_filtered(q)


@pytest.mark.asyncio
async def test_dispatch_emits_error_on_handler_failure(monkeypatch):
    handler_registry.clear()
    handler = _RecordingHandler(raise_exc=RuntimeError("boom"))
    handler_registry.register("recording", handler)

    monkeypatch.setattr(
        types_service, "resolve_operation",
        lambda m, o: _make_op_spec(),
    )

    event_bus._filtered_subscribers.clear()
    q = await event_bus.subscribe_filtered(
        event_bus.EventFilter(event_names=["artifact.invoke.*"])
    )

    artifact = {
        "_key": "a1",
        "workspace_id": "w1",
        "context": {"content_type": "application/vnd.agience.operator+json"},
    }
    ctx = DispatchContext(
        user_id="u1",
        actor_id="u1",
        grants=[_FakeGrant(invoke=True)],
        arango_db=None,
    )

    with pytest.raises(RuntimeError):
        await dispatch("invoke", artifact, {}, ctx)

    # before + error
    assert q.qsize() == 2
    q.get_nowait()  # started
    failed = q.get_nowait()
    assert failed.name == "artifact.invoke.failed"
    assert failed.payload["phase"] == "error"
    assert failed.payload["error"]["type"] == "RuntimeError"
    assert failed.payload["error"]["message"] == "boom"

    await event_bus.unsubscribe_filtered(q)


# ---------------------------------------------------------------------------
# Phase 1 end-to-end: vnd.agience.transform+json declares operations.invoke
# ---------------------------------------------------------------------------

def test_transform_type_declares_invoke_operation():
    """The Phase 1 pilot type must declare operations.invoke with mcp_tool
    dispatch and the three lifecycle emits."""
    types_service.invalidate_type_cache()
    op = types_service.resolve_operation(
        "application/vnd.agience.transform+json", "invoke"
    )
    assert op is not None, "transform type must declare operations.invoke"
    assert op.enabled is True
    assert op.requires_grant == "invoke"
    assert op.dispatch["kind"] == "mcp_tool"
    assert op.dispatch["server_ref"] == "$.context.run.server"
    assert op.dispatch["tool_ref"] == "$.context.run.tool"
    event_names = {e["event"] for e in op.emits}
    assert "artifact.invoke.started" in event_names
    assert "artifact.invoke.completed" in event_names
    assert "artifact.invoke.failed" in event_names
    phases = {e["phase"] for e in op.emits}
    assert phases == {"before", "after", "error"}


@pytest.mark.asyncio
async def test_dispatch_resolves_mcp_tool_refs_from_transform_artifact(monkeypatch):
    """Dispatching invoke on a transform artifact should resolve server/tool
    from context.run.{server,tool} and call mcp_service.invoke_tool with them."""
    types_service.invalidate_type_cache()
    handler_registry.clear()
    handler_registry.register_builtin_handlers()

    captured = {}

    async def fake_invoke_tool(db, user_id, workspace_id, server_artifact_id, tool_name, arguments):
        captured["server"] = server_artifact_id
        captured["tool"] = tool_name
        captured["arguments"] = arguments
        captured["workspace_id"] = workspace_id
        return {"ok": True, "echoed": arguments}

    import services.mcp_service as mcp_service
    monkeypatch.setattr(mcp_service, "invoke_tool", fake_invoke_tool)

    artifact = {
        "_key": "art-xform-1",
        "workspace_id": "ws-1",
        "context": {
            "content_type": "application/vnd.agience.transform+json",
            "run": {"server": "astra", "tool": "ingest_text"},
        },
    }
    ctx = DispatchContext(
        user_id="u1",
        actor_id="u1",
        grants=[_FakeGrant(invoke=True)],
        arango_db=None,
    )

    result = await dispatch(
        "invoke",
        artifact,
        {"workspace_id": "ws-1", "arguments": {"text": "hello"}},
        ctx,
    )

    assert result == {"ok": True, "echoed": {"text": "hello"}}
    assert captured["server"] == "astra"
    assert captured["tool"] == "ingest_text"
    assert captured["workspace_id"] == "ws-1"
    assert captured["arguments"] == {"text": "hello"}


# ---------------------------------------------------------------------------
# Phase 4: custom operations on new artifact types
# ---------------------------------------------------------------------------

def test_api_key_type_declares_crud_operations():
    types_service.invalidate_type_cache()
    for op_name in ("create", "read", "update", "delete"):
        op = types_service.resolve_operation(
            "application/vnd.agience.api-key+json", op_name
        )
        assert op is not None, f"api-key type must declare {op_name}"
        assert op.enabled is True
    # create is native-dispatched so it can capture the plaintext key
    create = types_service.resolve_operation(
        "application/vnd.agience.api-key+json", "create"
    )
    assert create.dispatch["kind"] == "native"
    assert "api_key_service.create_api_key" in create.dispatch["target"]


def test_server_credential_type_declares_rotate_and_publish_jwk():
    types_service.invalidate_type_cache()
    rotate = types_service.resolve_operation(
        "application/vnd.agience.server-credential+json", "rotate"
    )
    assert rotate is not None
    assert rotate.enabled is True
    assert rotate.requires_grant == "own"
    assert rotate.dispatch["kind"] == "native"
    assert rotate.audit is True
    assert any(e["event"] == "server_credential.rotated" for e in rotate.emits)

    jwk = types_service.resolve_operation(
        "application/vnd.agience.server-credential+json", "publish_jwk"
    )
    assert jwk is not None
    assert jwk.dispatch["kind"] == "native"


def test_secret_type_declares_fetch_operation_with_audit():
    types_service.invalidate_type_cache()
    fetch = types_service.resolve_operation(
        "application/vnd.agience.secret+json", "fetch"
    )
    assert fetch is not None
    assert fetch.enabled is True
    assert fetch.requires_grant == "read"
    assert fetch.dispatch["kind"] == "native"
    assert fetch.audit is True
    assert any(e["event"] == "secret.fetched" for e in fetch.emits)


def test_grant_invite_type_declares_claim_operation():
    types_service.invalidate_type_cache()
    claim = types_service.resolve_operation(
        "application/vnd.agience.grant-invite+json", "claim"
    )
    assert claim is not None
    assert claim.enabled is True
    assert claim.dispatch["kind"] == "native"
    assert claim.audit is True
    assert any(e["event"] == "invite.claimed" for e in claim.emits)


def test_relay_session_type_observe_only():
    types_service.invalidate_type_cache()
    read = types_service.resolve_operation(
        "application/vnd.agience.relay-session+json", "read"
    )
    delete = types_service.resolve_operation(
        "application/vnd.agience.relay-session+json", "delete"
    )
    create = types_service.resolve_operation(
        "application/vnd.agience.relay-session+json", "create"
    )
    assert read is not None
    assert delete is not None
    # relay sessions are created by the WS connect handler, never via CRUD
    assert create is None


def test_platform_server_slugs_match_builtin_paths():
    """PLATFORM_SERVER_SLUGS must stay in sync with config.BUILTIN_MCP_SERVER_PATHS.
    The seed service relies on this invariant to generate one artifact per
    built-in persona."""
    from services.bootstrap_types import PLATFORM_SERVER_SLUGS
    from core.config import BUILTIN_MCP_SERVER_PATHS
    assert set(PLATFORM_SERVER_SLUGS) == set(BUILTIN_MCP_SERVER_PATHS.keys())


def test_servers_content_service_platform_list_matches_slugs():
    """The per-server metadata list in servers_content_service must cover
    every slug in PLATFORM_SERVER_SLUGS. Guards against a silent drift where
    a new persona is added to one place but not the other."""
    from services import servers_content_service
    from services.bootstrap_types import PLATFORM_SERVER_SLUGS
    seeded = {s["slug"] for s in servers_content_service._PLATFORM_SERVERS}
    assert seeded == set(PLATFORM_SERVER_SLUGS)


def test_servers_content_service_context_shape():
    """Seed artifact context must carry the fields Phase 7B's mcp_service
    will look up: content_type (for dispatcher type resolution), transport
    (builtin routing), client_id (delegation JWT audience), slug."""
    import json
    from services import servers_content_service
    nexus = next(s for s in servers_content_service._PLATFORM_SERVERS if s["slug"] == "nexus")
    context_json = servers_content_service._build_server_context(nexus)
    context = json.loads(context_json)
    assert context["content_type"] == "application/vnd.agience.mcp-server+json"
    assert context["mcp_server"]["transport"] == "builtin"
    assert context["mcp_server"]["client_id"] == "agience-server-nexus"
    assert context["mcp_server"]["slug"] == "nexus"
    assert context["mcp_server"]["kind"] == "platform-builtin"


def test_mcp_server_type_declares_invoke_with_body_refs():
    """Phase 7A: vnd.agience.mcp-server+json declares operations.invoke with
    mcp_tool dispatch that pulls server_ref from the artifact's own _key and
    tool_ref from the request body. This is the foundation for Phase 7B
    where POST /artifacts/{server_id}/invoke routes through the dispatcher."""
    types_service.invalidate_type_cache()
    op = types_service.resolve_operation(
        "application/vnd.agience.mcp-server+json", "invoke"
    )
    assert op is not None, "mcp-server type must declare operations.invoke"
    assert op.enabled is True
    assert op.requires_grant == "invoke"
    assert op.dispatch["kind"] == "mcp_tool"
    assert op.dispatch["server_ref"] == "$.root_id"
    assert op.dispatch["tool_ref"] == "$.body.name"
    event_names = {e["event"] for e in op.emits}
    assert {"artifact.invoke.started", "artifact.invoke.completed", "artifact.invoke.failed"} <= event_names


def test_mcp_server_type_declares_resources_read_and_import():
    types_service.invalidate_type_cache()
    read = types_service.resolve_operation(
        "application/vnd.agience.mcp-server+json", "resources_read"
    )
    assert read is not None
    assert read.dispatch["kind"] == "native"
    assert "dispatch_resources_read" in read.dispatch["target"]

    imp = types_service.resolve_operation(
        "application/vnd.agience.mcp-server+json", "resources_import"
    )
    assert imp is not None
    assert imp.dispatch["kind"] == "native"
    assert imp.requires_grant == "add"


@pytest.mark.asyncio
async def test_mcp_tool_handler_resolves_server_from_key_and_tool_from_body():
    """End-to-end check of the Phase 7B dispatch shape: given a server
    artifact with a `root_id`, and a body containing `{name, arguments}`,
    the mcp_tool handler should resolve server_ref=$.root_id and tool_ref=$.body.name
    and invoke mcp_service.invoke_tool with the resolved values."""
    types_service.invalidate_type_cache()
    handler_registry.clear()
    handler_registry.register_builtin_handlers()

    captured = {}

    def fake_invoke_tool(db, user_id, workspace_id, server_artifact_id, tool_name, arguments):
        captured["server"] = server_artifact_id
        captured["tool"] = tool_name
        captured["arguments"] = arguments
        return {"content": [{"type": "text", "text": "ok"}]}

    import services.mcp_service as mcp_service
    monkeypatch_ctx = pytest.MonkeyPatch()
    monkeypatch_ctx.setattr(mcp_service, "invoke_tool", fake_invoke_tool)
    try:
        artifact = {
            "_key": "srv-aria-uuid",
            "root_id": "srv-aria-uuid",
            "workspace_id": None,
            "context": {"content_type": "application/vnd.agience.mcp-server+json"},
        }
        ctx = DispatchContext(
            user_id="u1", actor_id="u1",
            grants=[_FakeGrant(invoke=True)],
            arango_db=None,
        )

        result = await dispatch(
            "invoke",
            artifact,
            {"name": "run_chat_turn", "arguments": {"prompt": "hi"}},
            ctx,
        )
        assert result == {"content": [{"type": "text", "text": "ok"}]}
        assert captured["server"] == "srv-aria-uuid"
        assert captured["tool"] == "run_chat_turn"
        assert captured["arguments"] == {"prompt": "hi"}
    finally:
        monkeypatch_ctx.undo()


@pytest.mark.asyncio
async def test_custom_op_dispatches_native_target(monkeypatch):
    """End-to-end: custom op on server-credential artifact resolves dispatch
    kind=native, invokes the registered target with (artifact, body, ctx),
    and emits the declared rotate event."""
    types_service.invalidate_type_cache()
    handler_registry.clear()
    handler_registry.register_builtin_handlers()

    captured = {}

    async def fake_rotate(artifact, body, ctx):
        captured["artifact_id"] = artifact.get("_key")
        captured["body"] = body
        captured["user_id"] = ctx.user_id
        return {"client_secret": "new-secret-shown-once"}

    handler_registry.register_native_target(
        "server_credential_service.rotate_credential", fake_rotate
    )

    event_bus._filtered_subscribers.clear()
    q = await event_bus.subscribe_filtered(
        event_bus.EventFilter(event_names=["server_credential.*"])
    )

    artifact = {
        "_key": "cred-1",
        "workspace_id": None,
        "context": {"content_type": "application/vnd.agience.server-credential+json"},
    }
    ctx = DispatchContext(
        user_id="u1",
        actor_id="u1",
        grants=[_FakeGrant(own=True)],
        arango_db=None,
    )

    result = await dispatch("rotate", artifact, {"reason": "scheduled"}, ctx)
    assert result == {"client_secret": "new-secret-shown-once"}
    assert captured["artifact_id"] == "cred-1"
    assert captured["body"] == {"reason": "scheduled"}
    assert captured["user_id"] == "u1"

    # rotate is audit:true, so the after-event carries the full request/response
    assert q.qsize() == 1
    event = q.get_nowait()
    assert event.name == "server_credential.rotated"
    assert event.payload["phase"] == "after"
    assert event.payload["request"] == {"reason": "scheduled"}
    assert event.payload["response"] == {"client_secret": "new-secret-shown-once"}

    await event_bus.unsubscribe_filtered(q)


# ---------------------------------------------------------------------------
# Phase 7C: slug<->UUID resolution helpers + invoke_tool normalization
# ---------------------------------------------------------------------------

def test_resolve_builtin_server_id_returns_registered_uuid(monkeypatch):
    """When a slug is registered in the platform topology, the helper
    returns the seeded artifact UUID."""
    from services import mcp_service
    from services import platform_topology

    monkeypatch.setattr(
        platform_topology, "get_id_optional",
        lambda slug: "uuid-aria-1234" if slug == "agience-server-aria" else None,
    )
    assert mcp_service.resolve_builtin_server_id("aria") == "uuid-aria-1234"


def test_resolve_builtin_server_id_raises_when_not_registered(monkeypatch):
    """When the topology registry has not been populated, the helper raises
    ValueError — callers must ensure bootstrap has completed."""
    from services import mcp_service
    from services import platform_topology

    monkeypatch.setattr(platform_topology, "get_id_optional", lambda slug: None)
    with pytest.raises(ValueError, match="aria"):
        mcp_service.resolve_builtin_server_id("aria")


def test_resolve_builtin_server_id_handles_empty_input():
    from services import mcp_service
    assert mcp_service.resolve_builtin_server_id("") == ""
    assert mcp_service.resolve_builtin_server_id(None) is None


def test_lookup_builtin_slug_for_artifact_id_finds_registered(monkeypatch):
    """Reverse lookup: given a UUID, find the persona slug if it matches
    a registered seeded server artifact."""
    from services import mcp_service
    from services import platform_topology

    fake_registry = {
        "agience-server-aria": "uuid-aria-1234",
        "agience-server-verso": "uuid-verso-5678",
    }
    monkeypatch.setattr(
        platform_topology, "get_id_optional",
        lambda slug: fake_registry.get(slug),
    )
    assert mcp_service._lookup_builtin_slug_for_artifact_id("uuid-aria-1234") == "aria"
    assert mcp_service._lookup_builtin_slug_for_artifact_id("uuid-verso-5678") == "verso"
    assert mcp_service._lookup_builtin_slug_for_artifact_id("uuid-unknown") is None


def test_lookup_builtin_slug_returns_none_for_non_registered_inputs():
    """The reverse lookup returns None when the input doesn't match any
    registered seeded server artifact UUID. This includes:
    - Bare persona slugs (`aria` — no dash, short-circuits early)
    - The literal special-cased dispatch tokens (`agience-core`)
    - Path-shaped strings (`foo/bar` — short-circuits on `/`)
    - Empty input
    With an empty topology registry (the default in unit tests), even
    well-formed UUIDs return None.
    """
    from services import mcp_service
    assert mcp_service._lookup_builtin_slug_for_artifact_id("aria") is None
    assert mcp_service._lookup_builtin_slug_for_artifact_id("agience-core") is None
    assert mcp_service._lookup_builtin_slug_for_artifact_id("foo/bar") is None
    assert mcp_service._lookup_builtin_slug_for_artifact_id("") is None


# ---------------------------------------------------------------------------
# Phase 7B: dispatch_resources_{read,import} native targets + end-to-end
# custom-op dispatch on a mcp-server artifact
# ---------------------------------------------------------------------------

def test_dispatch_resources_read_extracts_uri_and_server_id_from_artifact(monkeypatch):
    from services import mcp_service

    captured = {}

    def fake_read_resource(db, user_id, server_artifact_id, uri, workspace_id=None):
        captured["server"] = server_artifact_id
        captured["uri"] = uri
        captured["workspace_id"] = workspace_id
        captured["user_id"] = user_id
        return {"contents": [{"uri": uri, "text": "hello"}]}

    monkeypatch.setattr(mcp_service, "read_resource", fake_read_resource)

    artifact = {
        "_key": "srv-aria-uuid",
        "context": {"content_type": "application/vnd.agience.mcp-server+json"},
    }
    ctx = DispatchContext(
        user_id="u1", actor_id="u1",
        grants=[_FakeGrant(read=True)],
        arango_db=None,
    )
    result = mcp_service.dispatch_resources_read(
        artifact,
        {"uri": "ui://aria/chat", "workspace_id": "ws-1"},
        ctx,
    )
    assert result == {"contents": [{"uri": "ui://aria/chat", "text": "hello"}]}
    assert captured["server"] == "srv-aria-uuid"
    assert captured["uri"] == "ui://aria/chat"
    assert captured["workspace_id"] == "ws-1"
    assert captured["user_id"] == "u1"


def test_dispatch_resources_read_raises_on_missing_uri():
    from services import mcp_service
    artifact = {"_key": "srv-1", "context": {}}
    ctx = DispatchContext(user_id="u1", actor_id="u1", grants=[], arango_db=None)
    with pytest.raises(ValueError, match="body.uri"):
        mcp_service.dispatch_resources_read(artifact, {}, ctx)


def test_dispatch_resources_import_wraps_existing_service(monkeypatch):
    from services import mcp_service

    captured = {}

    def fake_import(db, user_id, workspace_id, server_artifact_id, resources):
        captured["workspace_id"] = workspace_id
        captured["server"] = server_artifact_id
        captured["resources"] = resources
        return ["art-1", "art-2"]

    monkeypatch.setattr(mcp_service, "import_resources_as_artifacts", fake_import)

    artifact = {
        "_key": "srv-aria-uuid",
        "context": {"content_type": "application/vnd.agience.mcp-server+json"},
    }
    ctx = DispatchContext(user_id="u1", actor_id="u1", grants=[], arango_db=None)
    result = mcp_service.dispatch_resources_import(
        artifact,
        {"workspace_id": "ws-42", "resources": [{"uri": "a"}, {"uri": "b"}]},
        ctx,
    )
    assert result == {"created_artifact_ids": ["art-1", "art-2"], "count": 2}
    assert captured["server"] == "srv-aria-uuid"
    assert captured["workspace_id"] == "ws-42"
    assert len(captured["resources"]) == 2


def test_dispatch_resources_import_raises_on_missing_workspace():
    from services import mcp_service
    artifact = {"_key": "srv-1"}
    ctx = DispatchContext(user_id="u1", actor_id="u1", grants=[], arango_db=None)
    with pytest.raises(ValueError, match="workspace_id"):
        mcp_service.dispatch_resources_import(artifact, {"resources": []}, ctx)


@pytest.mark.asyncio
async def test_end_to_end_resources_read_via_dispatcher(monkeypatch):
    """Full dispatch path: operation dispatcher resolves the native target
    by dotted name (`mcp_service.dispatch_resources_read`), importlib
    fallback imports `services.mcp_service`, dispatcher invokes it with
    `(artifact, body, ctx)`, result comes back through the emit envelope
    with mcp.resource.read event fired."""
    types_service.invalidate_type_cache()
    handler_registry.clear()
    handler_registry.register_builtin_handlers()

    from services import mcp_service

    def fake_read_resource(db, user_id, server_artifact_id, uri, workspace_id=None):
        return {"contents": [{"uri": uri, "text": "from-aria"}]}

    monkeypatch.setattr(mcp_service, "read_resource", fake_read_resource)

    event_bus._filtered_subscribers.clear()
    q = await event_bus.subscribe_filtered(
        event_bus.EventFilter(event_names=["mcp.resource.*"])
    )

    artifact = {
        "_key": "srv-aria-uuid",
        "workspace_id": None,
        "context": {"content_type": "application/vnd.agience.mcp-server+json"},
    }
    ctx = DispatchContext(
        user_id="u1", actor_id="u1",
        grants=[_FakeGrant(read=True)],
        arango_db=None,
    )

    result = await dispatch(
        "resources_read",
        artifact,
        {"uri": "ui://aria/chat"},
        ctx,
    )
    assert result == {"contents": [{"uri": "ui://aria/chat", "text": "from-aria"}]}

    assert q.qsize() == 1
    evt = q.get_nowait()
    assert evt.name == "mcp.resource.read"
    assert evt.payload["phase"] == "after"
    assert evt.payload["content_type"] == "application/vnd.agience.mcp-server+json"

    await event_bus.unsubscribe_filtered(q)


@pytest.mark.asyncio
async def test_dispatch_grant_forbidden(monkeypatch):
    handler_registry.clear()
    handler_registry.register("recording", _RecordingHandler())

    monkeypatch.setattr(
        types_service, "resolve_operation",
        lambda m, o: _make_op_spec(),
    )

    artifact = {
        "_key": "a1",
        "workspace_id": "w1",
        "context": {"content_type": "application/vnd.agience.operator+json"},
    }
    # Grants exist but not can_invoke
    ctx = DispatchContext(
        user_id="u1",
        actor_id="u1",
        grants=[_FakeGrant(read=True)],
        arango_db=None,
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        await dispatch("invoke", artifact, {}, ctx)
    assert excinfo.value.status_code == 403
