"""Unit tests for services.mcp_service.

Covers the spine of the MCP proxy:
  - Server registry manifest loading and name-to-ID resolution
  - Delegation JWT injection on builtin invokes (RFC 8693 act.sub claim)
  - `agience-core` short-circuit
  - Auth header resolution: oauth2 / api_key / static / missing
  - AuthExpiredError when bearer token is past expiry or missing
  - Dispatcher targets: `dispatch_resources_read`, `dispatch_resources_import`
  - `_extract_content_type` parses dict, namespace, and json-string contexts
  - `import_resources_as_artifacts` builds correct context envelopes
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services import mcp_service
from services import server_registry
from mcp_client.contracts import (
    MCPAuthConfig as MCPServerAuth,
    MCPServerConfig,
    MCPServerTransport,
)


# ---------------------------------------------------------------------------
# _extract_content_type
# ---------------------------------------------------------------------------

class TestExtractContentType:
    def test_dict_context(self):
        art = SimpleNamespace(context={"content_type": "application/vnd.agience.mcp-server+json"})
        assert mcp_service._extract_content_type(art) == "application/vnd.agience.mcp-server+json"

    def test_json_string_context(self):
        art = SimpleNamespace(context=json.dumps({"content_type": "application/json"}))
        assert mcp_service._extract_content_type(art) == "application/json"

    def test_raw_dict_artifact(self):
        art = {"context": {"content_type": "text/plain"}}
        assert mcp_service._extract_content_type(art) == "text/plain"

    def test_missing_context(self):
        assert mcp_service._extract_content_type(SimpleNamespace(context=None)) is None

    def test_unparseable_string_context(self):
        assert mcp_service._extract_content_type(SimpleNamespace(context="not-json")) is None

    def test_context_without_content_type(self):
        art = SimpleNamespace(context={"other": "thing"})
        assert mcp_service._extract_content_type(art) is None


# ---------------------------------------------------------------------------
# Server registry
# ---------------------------------------------------------------------------

class TestServerRegistry:
    def test_manifest_loads_all_personas(self):
        names = server_registry.all_names()
        assert "aria" in names
        assert "nexus" in names
        assert len(names) == 8

    def test_get_entry_returns_manifest_data(self):
        entry = server_registry.get_entry("aria")
        assert entry is not None
        assert entry.client_id == "agience-server-aria"
        assert entry.path == "/aria/mcp"

    def test_get_entry_unknown_returns_none(self):
        assert server_registry.get_entry("not-a-persona") is None

    def test_all_client_ids_is_frozenset(self):
        cids = server_registry.all_client_ids()
        assert isinstance(cids, frozenset)
        assert "agience-server-nexus" in cids

    def test_resolve_name_to_id_before_populate_raises(self):
        # Clear any previously populated IDs
        server_registry._ID_BY_NAME.clear()
        server_registry._NAME_BY_ID.clear()
        with pytest.raises(ValueError, match="aria"):
            server_registry.resolve_name_to_id("aria")

    def test_resolve_name_to_id_after_populate(self, monkeypatch):
        monkeypatch.setattr(
            "services.platform_topology.get_id_optional",
            lambda slug: f"uuid-{slug}" if "aria" in slug else None,
        )
        server_registry._ID_BY_NAME.clear()
        server_registry._NAME_BY_ID.clear()
        server_registry.populate_ids()
        assert server_registry.resolve_name_to_id("aria") == "uuid-agience-server-aria"

    def test_build_http_config(self, monkeypatch):
        monkeypatch.setenv("AGIENCE_SERVER_HOST_URI", "https://servers.example.com")
        monkeypatch.setattr(
            "services.platform_topology.get_id_optional",
            lambda slug: f"uuid-{slug}",
        )
        server_registry._ID_BY_NAME.clear()
        server_registry._NAME_BY_ID.clear()
        server_registry.populate_ids()
        cfg = server_registry.build_http_config("nexus")
        assert cfg.id == "uuid-agience-server-nexus"
        assert cfg.transport.type == "http"
        assert "/nexus" in cfg.transport.well_known

    def test_is_builtin_id(self, monkeypatch):
        monkeypatch.setattr(
            "services.platform_topology.get_id_optional",
            lambda slug: "uuid-1" if "aria" in slug else None,
        )
        server_registry._ID_BY_NAME.clear()
        server_registry._NAME_BY_ID.clear()
        server_registry.populate_ids()
        assert server_registry.is_builtin_id("uuid-1") is True
        assert server_registry.is_builtin_id("random-uuid") is False


# ---------------------------------------------------------------------------
# invoke_tool delegation JWT injection
# ---------------------------------------------------------------------------

class TestInvokeToolBuiltin:
    def test_agience_core_short_circuits(self):
        fake_client = MagicMock()
        fake_client.call_tool.return_value = {"content": "ok"}
        with (
            patch("services.mcp_service._agience_core_id", return_value="agience-core"),
            patch(
                "services.mcp_service.create_agience_core_client", return_value=fake_client
            ) as create_local,
        ):
            result = mcp_service.invoke_tool(
                db=MagicMock(),
                user_id="user-1",
                server_artifact_id="agience-core",
                tool_name="search",
                arguments={"q": "x"},
            )
        assert result == {"content": "ok"}
        fake_client.call_tool.assert_called_once_with("search", {"q": "x"})
        fake_client.close.assert_called_once()
        create_local.assert_called_once()

    def test_builtin_persona_injects_delegation_token(self, monkeypatch):
        # Populate registry so nexus has a known bootstrap UUID
        nexus_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        monkeypatch.setattr(
            "services.platform_topology.get_id_optional",
            lambda slug: nexus_uuid if "nexus" in slug else None,
        )
        server_registry._ID_BY_NAME.clear()
        server_registry._NAME_BY_ID.clear()
        server_registry.populate_ids()
        fake_client = MagicMock()
        fake_client.call_tool.return_value = {"content": "tool-result"}
        with (
            patch("services.mcp_service._agience_core_id", return_value="core-uuid"),
            patch(
                "services.auth_service.issue_delegation_token", return_value="delegation-jwt"
            ) as issue,
            patch("mcp_client.adapter.create_client", return_value=fake_client) as create_client,
        ):
            mcp_service.invoke_tool(
                db=MagicMock(),
                user_id="user-1",
                server_artifact_id=nexus_uuid,
                tool_name="ping",
                arguments={},
            )

        # Delegation token issued for the server-prefixed client_id
        issue.assert_called_once_with("agience-server-nexus", "user-1")
        # And injected as the Authorization runtime header on the config
        cfg = create_client.call_args[0][0]
        assert cfg.runtime_headers == {"Authorization": "Bearer delegation-jwt"}
        fake_client.close.assert_called_once()

    def test_builtin_persona_uuid_normalises(self, monkeypatch):
        """If the caller passes a seeded UUID for a builtin server, it routes
        through the same builtin path as the name."""
        fake_client = MagicMock()
        fake_client.call_tool.return_value = {"ok": True}
        # Populate registry with a known UUID for aria
        monkeypatch.setattr(
            "services.platform_topology.get_id_optional",
            lambda slug: "abcd1234-aaaa-bbbb-cccc-deadbeefcafe" if "aria" in slug else None,
        )
        server_registry._ID_BY_NAME.clear()
        server_registry._NAME_BY_ID.clear()
        server_registry.populate_ids()
        with (
            patch("services.mcp_service._agience_core_id", return_value="core-uuid"),
            patch(
                "services.auth_service.issue_delegation_token", return_value="dt"
            ) as issue,
            patch("mcp_client.adapter.create_client", return_value=fake_client),
        ):
            mcp_service.invoke_tool(
                db=MagicMock(),
                user_id="user-2",
                server_artifact_id="abcd1234-aaaa-bbbb-cccc-deadbeefcafe",
                tool_name="speak",
                arguments={"text": "hi"},
            )
        issue.assert_called_once_with("agience-server-aria", "user-2")

    def test_builtin_with_no_user_skips_delegation(self, monkeypatch):
        nexus_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        monkeypatch.setattr(
            "services.platform_topology.get_id_optional",
            lambda slug: nexus_uuid if "nexus" in slug else None,
        )
        server_registry._ID_BY_NAME.clear()
        server_registry._NAME_BY_ID.clear()
        server_registry.populate_ids()
        fake_client = MagicMock()
        fake_client.call_tool.return_value = {}
        with (
            patch("services.mcp_service._agience_core_id", return_value="core-uuid"),
            patch("services.auth_service.issue_delegation_token") as issue,
            patch("mcp_client.adapter.create_client", return_value=fake_client) as create_client,
        ):
            mcp_service.invoke_tool(
                db=MagicMock(),
                user_id="",
                server_artifact_id=nexus_uuid,
                tool_name="ping",
                arguments={},
            )
        issue.assert_not_called()
        cfg = create_client.call_args[0][0]
        assert cfg.runtime_headers is None or cfg.runtime_headers == {}

    def test_unknown_server_raises_value_error(self):
        with (
            patch("services.mcp_service._agience_core_id", return_value="core-uuid"),
            patch("services.mcp_service._get_server_config", side_effect=ValueError("nope")),
            patch(
                "services.mcp_service._get_server_config_from_collections", return_value=None
            ),
        ):
            with pytest.raises(ValueError, match="not found"):
                mcp_service.invoke_tool(
                    db=MagicMock(),
                    user_id="u",
                    server_artifact_id="00000000-1111-2222-3333-444444444444",
                    tool_name="x",
                    arguments={},
                    workspace_id="ws",
                )


# ---------------------------------------------------------------------------
# _resolve_auth_headers
# ---------------------------------------------------------------------------

def _cfg(auth: MCPServerAuth) -> MCPServerConfig:
    return MCPServerConfig(
        id="srv",
        label="Srv",
        transport=MCPServerTransport(type="http", well_known="https://srv.example/.well-known/mcp.json"),
        auth=auth,
    )


class TestResolveAuthHeaders:
    def test_no_auth_returns_empty(self):
        cfg = MCPServerConfig(
            id="srv",
            label="Srv",
            transport=MCPServerTransport(type="http", well_known="https://srv/x"),
        )
        assert mcp_service._resolve_auth_headers(MagicMock(), "u", cfg) == {}

    def test_static_header_passthrough(self):
        cfg = _cfg(MCPServerAuth(type="static", header="X-Token", value="hunter2"))
        assert mcp_service._resolve_auth_headers(MagicMock(), "u", cfg) == {"X-Token": "hunter2"}

    def test_api_key_decrypts_and_wraps_authorization_as_bearer(self):
        cfg = _cfg(MCPServerAuth(type="api_key", secret_id="sec-1"))
        secret = SimpleNamespace(encrypted_value="enc")
        with (
            patch("services.secrets_service.list_secrets", return_value=[secret]),
            patch("services.secrets_service.decrypt_value", return_value="raw-token"),
        ):
            headers = mcp_service._resolve_auth_headers(MagicMock(), "u", cfg)
        assert headers == {"Authorization": "Bearer raw-token"}

    def test_api_key_custom_header_no_bearer_prefix(self):
        cfg = _cfg(MCPServerAuth(type="api_key", secret_id="sec-1", header="X-API-Key"))
        secret = SimpleNamespace(encrypted_value="enc")
        with (
            patch("services.secrets_service.list_secrets", return_value=[secret]),
            patch("services.secrets_service.decrypt_value", return_value="raw-token"),
        ):
            headers = mcp_service._resolve_auth_headers(MagicMock(), "u", cfg)
        assert headers == {"X-API-Key": "raw-token"}

    def test_oauth2_no_secrets_raises_auth_expired(self):
        cfg = _cfg(MCPServerAuth(type="oauth2", authorizer_id="authz-1"))
        with patch("services.secrets_service.list_secrets", return_value=[]):
            with pytest.raises(mcp_service.AuthExpiredError, match="No stored credentials"):
                mcp_service._resolve_auth_headers(MagicMock(), "u", cfg)

    def test_oauth2_only_refresh_token_present_raises_auth_expired(self):
        cfg = _cfg(MCPServerAuth(type="oauth2", authorizer_id="authz-1"))
        refresh = SimpleNamespace(encrypted_value="enc", expires_at="")

        def fake_list(db, user_id, **kwargs):
            if kwargs.get("secret_type") == "bearer_token":
                return []
            if kwargs.get("secret_type") == "oauth_refresh_token":
                return [refresh]
            return []

        with patch("services.secrets_service.list_secrets", side_effect=fake_list):
            with pytest.raises(mcp_service.AuthExpiredError, match="provide_access_token"):
                mcp_service._resolve_auth_headers(MagicMock(), "u", cfg)

    def test_oauth2_expired_bearer_raises_auth_expired(self):
        cfg = _cfg(MCPServerAuth(type="oauth2", authorizer_id="authz-1"))
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        bearer = SimpleNamespace(encrypted_value="enc", expires_at=past)
        with patch("services.secrets_service.list_secrets", return_value=[bearer]):
            with pytest.raises(mcp_service.AuthExpiredError, match="expired"):
                mcp_service._resolve_auth_headers(MagicMock(), "u", cfg)

    def test_oauth2_valid_bearer_returns_authorization_header(self):
        cfg = _cfg(MCPServerAuth(type="oauth2", authorizer_id="authz-1"))
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        bearer = SimpleNamespace(encrypted_value="enc", expires_at=future)
        with (
            patch("services.secrets_service.list_secrets", return_value=[bearer]),
            patch("services.secrets_service.decrypt_value", return_value="real-token"),
        ):
            headers = mcp_service._resolve_auth_headers(MagicMock(), "u", cfg)
        assert headers == {"Authorization": "Bearer real-token"}


# ---------------------------------------------------------------------------
# Dispatcher native targets
# ---------------------------------------------------------------------------

class TestDispatchTargets:
    def test_dispatch_resources_read_validates_body_type(self):
        with pytest.raises(ValueError, match="JSON object body"):
            mcp_service.dispatch_resources_read({"_key": "k"}, body="oops", ctx=SimpleNamespace())

    def test_dispatch_resources_read_requires_uri(self):
        with pytest.raises(ValueError, match="body.uri"):
            mcp_service.dispatch_resources_read(
                {"root_id": "r1"}, body={}, ctx=SimpleNamespace(arango_db=MagicMock(), user_id="u")
            )

    def test_dispatch_resources_read_uses_root_id_first(self):
        artifact = {"root_id": "r1", "_key": "k1", "id": "id1"}
        ctx = SimpleNamespace(arango_db=MagicMock(), user_id="user-1")
        with patch("services.mcp_service.read_resource", return_value={"text": "x"}) as rr:
            out = mcp_service.dispatch_resources_read(
                artifact,
                body={"uri": "ui://nexus/view.html", "workspace_id": "ws-1"},
                ctx=ctx,
            )
        assert out == {"text": "x"}
        rr.assert_called_once_with(
            db=ctx.arango_db,
            user_id="user-1",
            server_artifact_id="r1",
            uri="ui://nexus/view.html",
            workspace_id="ws-1",
        )

    def test_dispatch_resources_import_validates_body_shape(self):
        ctx = SimpleNamespace(arango_db=MagicMock(), user_id="u")
        with pytest.raises(ValueError, match="JSON object body"):
            mcp_service.dispatch_resources_import({"_key": "k"}, body=None, ctx=ctx)
        with pytest.raises(ValueError, match="workspace_id"):
            mcp_service.dispatch_resources_import({"_key": "k"}, body={"resources": []}, ctx=ctx)
        with pytest.raises(ValueError, match="resources"):
            mcp_service.dispatch_resources_import(
                {"_key": "k"}, body={"workspace_id": "ws"}, ctx=ctx
            )

    def test_dispatch_resources_import_returns_count_and_ids(self):
        artifact = {"root_id": "srv-root", "_key": "k", "id": "i"}
        ctx = SimpleNamespace(arango_db=MagicMock(), user_id="user-1")
        with patch(
            "services.mcp_service.import_resources_as_artifacts",
            return_value=["a-1", "a-2", "a-3"],
        ) as imp:
            out = mcp_service.dispatch_resources_import(
                artifact,
                body={"workspace_id": "ws-1", "resources": [{"uri": "u1"}, {"uri": "u2"}]},
                ctx=ctx,
            )
        assert out == {"created_artifact_ids": ["a-1", "a-2", "a-3"], "count": 3}
        # server_artifact_id resolves to root_id
        assert imp.call_args.kwargs["server_artifact_id"] == "srv-root"
        assert imp.call_args.kwargs["workspace_id"] == "ws-1"


# ---------------------------------------------------------------------------
# import_resources_as_artifacts
# ---------------------------------------------------------------------------

class TestImportResourcesAsArtifacts:
    def test_workspace_no_create_grant_raises(self):
        db = MagicMock()
        with patch("db.arango.get_active_grants_for_principal_resource", return_value=[]):
            with pytest.raises(ValueError, match="not found"):
                mcp_service.import_resources_as_artifacts(
                    db, "user-1", "ws", "srv-1", resources=[]
                )

    def test_builds_resource_context_envelopes(self):
        db = MagicMock()
        grant = SimpleNamespace(can_create=True)
        created = [SimpleNamespace(id="a-1"), SimpleNamespace(id="a-2")]
        with (
            patch("db.arango.get_active_grants_for_principal_resource", return_value=[grant]),
            patch(
                "services.workspace_service.create_workspace_artifacts_bulk",
                return_value=created,
            ) as bulk,
        ):
            out = mcp_service.import_resources_as_artifacts(
                db,
                "user-1",
                "ws",
                "srv-1",
                resources=[
                    {"uri": "ui://x/a", "title": "A", "kind": "text", "text": "hello"},
                    {"uri": "ui://x/b", "kind": "html"},
                ],
            )
        assert out == ["a-1", "a-2"]
        items = bulk.call_args[0][3]
        assert len(items) == 2
        ctx0 = json.loads(items[0]["context"])
        assert ctx0["content_type"] == "application/vnd.agience.resource+json"
        assert ctx0["server"] == "srv-1"
        assert ctx0["uri"] == "ui://x/a"
        assert items[0]["content"] == "hello"
