"""Tests for POST /artifacts/{artifact_id}/op/{op_name} — custom operation route.

Covers:
- Builtin server slug → UUID resolution (e.g. "nexus" → registered artifact UUID)
- UUID passthrough when caller already has a UUID
- Unknown slug (non-server-slug string) falls through to normal 404 path
- Reserved op names are rejected with 400
- OperationNotDeclared raises 404
- Happy-path dispatch via mocked operation_dispatcher
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from routers.artifacts_router import router  # noqa: E402
from services.dependencies import AuthContext, get_auth  # noqa: E402
from core.dependencies import get_arango_db  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def mock_db():
    return MagicMock()


@pytest.fixture()
def authed_client(app, mock_db):
    # Pre-populate a synthetic grant so the check_access DB call is skipped.
    # Tests here exercise routing/dispatch logic, not the grant-loading path.
    synthetic_grant = MagicMock()
    synthetic_grant.can_read = True
    synthetic_grant.can_update = True
    synthetic_grant.can_invoke = True
    synthetic_grant.can_add = True
    synthetic_grant.can_share = True
    synthetic_grant.resource_id = None
    synthetic_grant.resource_type = None
    ctx = AuthContext(user_id="u-1", principal_type="user", grants=[synthetic_grant])
    app.dependency_overrides[get_auth] = lambda: ctx
    app.dependency_overrides[get_arango_db] = lambda: mock_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _make_server_doc(key: str, name: str) -> dict:
    """Minimal mcp-server artifact document as it comes back from ArangoDB."""
    return {
        "_key": key,
        "id": key,
        "workspace_id": None,
        "context": {
            "content_type": "application/vnd.agience.mcp-server+json",
            "mcp_server": {"name": name, "transport": "builtin"},
        },
    }


# ---------------------------------------------------------------------------
# Slug → UUID resolution
# ---------------------------------------------------------------------------

class TestSlugResolution:
    """The op route resolves builtin server names to their registered artifact UUID
    via server_registry before calling _find_artifact."""

    def test_slug_resolves_to_uuid_before_db_lookup(self, authed_client, mock_db):
        """When 'nexus' is passed as artifact_id, the registry resolves it
        to a UUID which is used for _find_artifact."""
        nexus_key = "aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb"
        nexus_doc = _make_server_doc(nexus_key, "nexus")

        # Mock DB: returns document when asked for the _key, not the slug
        workspace_coll = MagicMock()
        workspace_coll.get.side_effect = lambda key: nexus_doc if key == nexus_key else None
        mock_db.collection.return_value = workspace_coll

        with (
            patch("services.server_registry.resolve_name_to_id", return_value=nexus_key),
            patch("services.operation_dispatcher.dispatch", new=AsyncMock(return_value={"text": "hello"})),
        ):
            resp = authed_client.post(
                "/artifacts/nexus/op/resources_read",
                json={"uri": "ui://nexus/vnd.agience.mcp-server.html", "workspace_id": "ws-1"},
            )

        assert resp.status_code == 200
        # Confirm DB was called with the resolved _key, not the slug
        workspace_coll.get.assert_called_with(nexus_key)

    def test_slug_uses_root_id_lookup_when_version_key_differs(self, authed_client, mock_db):
        """If the resolved UUID is a root_id (not the current version _key),
        the route should still find the artifact and dispatch the operation."""
        nexus_root_id = "aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb"
        nexus_doc = {
            "_key": "version-1",
            "id": "version-1",
            "root_id": nexus_root_id,
            "context": {
                "content_type": "application/vnd.agience.mcp-server+json",
                "mcp_server": {"name": "nexus", "transport": "builtin"},
            },
        }

        workspace_coll = MagicMock()
        workspace_coll.get.return_value = None
        mock_db.collection.return_value = workspace_coll
        mock_db.aql.execute.return_value = iter([nexus_doc])

        with (
            patch("services.server_registry.resolve_name_to_id", return_value=nexus_root_id),
            patch("services.operation_dispatcher.dispatch", new=AsyncMock(return_value={"text": "hello"})),
        ):
            resp = authed_client.post(
                "/artifacts/nexus/op/resources_read",
                json={"uri": "ui://nexus/vnd.agience.mcp-server.html", "workspace_id": "ws-1"},
            )

        assert resp.status_code == 200
        workspace_coll.get.assert_called_with(nexus_root_id)
        mock_db.aql.execute.assert_called_once()

    def test_check_access_uses_doc_key_not_root_id(self, app, mock_db):
        """When grants are not pre-loaded (live JWT path), check_access must be
        called with doc._key, not the root_id.  Using root_id caused a DB key-miss
        inside check_access which swallowed the 404 and left grants empty → 403."""
        nexus_root_id = "aaaabbbb-cccc-dddd-eeee-ffffddddeeee"
        version_key = "nexus-version-key-202604"
        version_doc = {
            "_key": version_key,
            "id": version_key,
            "root_id": nexus_root_id,
            "context": {
                "content_type": "application/vnd.agience.mcp-server+json",
                "mcp_server": {"name": "nexus", "transport": "builtin"},
            },
        }

        # Auth context with NO pre-loaded grants — triggers the check_access path.
        ctx = AuthContext(user_id="u-live", principal_type="user", grants=[])
        app.dependency_overrides[get_auth] = lambda: ctx
        app.dependency_overrides[get_arango_db] = lambda: mock_db

        workspace_coll = MagicMock()
        workspace_coll.get.return_value = None
        mock_db.collection.return_value = workspace_coll
        mock_db.aql.execute.return_value = iter([version_doc])

        synthetic_grant = MagicMock()
        synthetic_grant.can_read = True
        synthetic_grant.effect = "allow"

        checked_with: list[str] = []

        def fake_check_access(auth_ctx, doc_key, action, db):
            checked_with.append(doc_key)
            return synthetic_grant

        client = TestClient(app)
        with (
            patch("services.server_registry.resolve_name_to_id", return_value=nexus_root_id),
            # Patch the source module — the router does a local import
            # `from services.dependencies import check_access as _check_access`,
            # so patching routers.artifacts_router.check_access has no effect.
            patch("services.dependencies.check_access", side_effect=fake_check_access),
            patch("services.operation_dispatcher.dispatch", new=AsyncMock(return_value={"ok": True})),
        ):
            resp = client.post(
                "/artifacts/nexus/op/resources_read",
                json={"uri": "ui://nexus/vnd.agience.mcp-server.html", "workspace_id": "ws-live"},
            )

        app.dependency_overrides.clear()

        assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}: {resp.json()}"
        # The critical assertion: check_access was called with the doc _key, not the root_id.
        assert checked_with, "check_access was never called (grants were pre-loaded unexpectedly)"
        assert checked_with[0] == version_key, (
            f"check_access received '{checked_with[0]}' but expected doc._key='{version_key}'; "
            f"using root_id '{nexus_root_id}' causes a 404 inside check_access → 403 from dispatcher"
        )

    def test_all_platform_slugs_use_root_id_lookup_when_version_key_differs(self, authed_client, mock_db):
        """Every built-in server name should dispatch when the resolved id is a
        root_id and the current artifact row has a different version _key."""
        from services import server_registry

        for name in server_registry.all_names():
            mock_db.aql.execute.reset_mock()
            root_id = f"{name[:4]}-root-0000-0000-0000-000000000000"
            version_doc = {
                "_key": f"{name}-version-1",
                "id": f"{name}-version-1",
                "root_id": root_id,
                "context": {
                    "content_type": "application/vnd.agience.mcp-server+json",
                    "mcp_server": {"name": name, "transport": "builtin"},
                },
            }

            workspace_coll = MagicMock()
            workspace_coll.get.return_value = None
            mock_db.collection.return_value = workspace_coll
            mock_db.aql.execute.return_value = iter([version_doc])

            with (
                patch("services.server_registry.resolve_name_to_id", return_value=root_id),
                patch("services.operation_dispatcher.dispatch", new=AsyncMock(return_value={"ok": True})),
            ):
                resp = authed_client.post(
                    f"/artifacts/{name}/op/resources_read",
                    json={"uri": f"ui://{name}/vnd.agience.mcp-server.html", "workspace_id": "ws-1"},
                )

            assert resp.status_code == 200
            workspace_coll.get.assert_called_with(root_id)
            mock_db.aql.execute.assert_called_once()

    def test_all_platform_names_resolve(self, authed_client, mock_db):
        """Every server name in the manifest should trigger resolution."""
        from services import server_registry

        for name in server_registry.all_names():
            test_key = f"{name[:4]}-uuid-0000-0000-0000-000000000000"
            doc = _make_server_doc(test_key, name)

            workspace_coll = MagicMock()
            workspace_coll.get.side_effect = lambda key, u=test_key, d=doc: d if key == u else None
            mock_db.collection.return_value = workspace_coll

            with (
                patch("services.server_registry.resolve_name_to_id", return_value=test_key),
                patch("services.operation_dispatcher.dispatch", new=AsyncMock(return_value={"ok": True})),
            ):
                authed_client.post(
                    f"/artifacts/{name}/op/resources_read",
                    json={"uri": "ui://test/view.html"},
                )

            workspace_coll.get.assert_called_with(test_key), (
                f"Name '{name}' was not resolved to _key before DB lookup"
            )

    def test_uuid_passthrough_unchanged(self, authed_client, mock_db):
        """When a full UUID is passed (not a server slug), it is passed directly
        to _find_artifact without modification."""
        artifact_uuid = "12345678-1234-1234-1234-1234567890ab"
        doc = {
            "_key": artifact_uuid,
            "context": {"content_type": "application/vnd.agience.mcp-server+json"},
        }

        workspace_coll = MagicMock()
        workspace_coll.get.side_effect = lambda key: doc if key == artifact_uuid else None
        mock_db.collection.return_value = workspace_coll

        with patch("services.operation_dispatcher.dispatch", new=AsyncMock(return_value={"result": "ok"})):
            resp = authed_client.post(
                f"/artifacts/{artifact_uuid}/op/resources_read",
                json={"uri": "ui://nexus/vnd.agience.mcp-server.html"},
            )

        assert resp.status_code == 200
        workspace_coll.get.assert_called_with(artifact_uuid)

    def test_unknown_string_not_in_server_slugs_returns_404(self, authed_client, mock_db):
        """A string that isn't a server slug and isn't a valid DB key gets a 404."""
        workspace_coll = MagicMock()
        workspace_coll.get.return_value = None
        collection_coll = MagicMock()
        collection_coll.get.return_value = None
        mock_db.collection.side_effect = lambda name: (
            workspace_coll if "workspace" in name else collection_coll
        )

        resp = authed_client.post(
            "/artifacts/totally-unknown-slug/op/resources_read",
            json={"uri": "ui://nexus/view.html"},
        )

        assert resp.status_code == 404

# ---------------------------------------------------------------------------
# Reserved op names
# ---------------------------------------------------------------------------

class TestReservedOpNames:
    """Reserved op names must be rejected with 400 before any DB lookup."""

    @pytest.mark.parametrize("op_name", ["create", "read", "update", "delete", "invoke", "add", "search"])
    def test_reserved_op_names_return_400(self, op_name, authed_client, mock_db):
        resp = authed_client.post(
            f"/artifacts/some-artifact-id/op/{op_name}",
            json={},
        )
        assert resp.status_code == 400
        assert op_name in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestOpAuthGuard:
    def test_unauthenticated_user_returns_401(self, app, mock_db):
        ctx = AuthContext(user_id=None, principal_type="anonymous", grants=[])
        app.dependency_overrides[get_auth] = lambda: ctx
        app.dependency_overrides[get_arango_db] = lambda: mock_db
        client = TestClient(app)

        resp = client.post(
            "/artifacts/some-id/op/resources_read",
            json={"uri": "ui://nexus/view.html"},
        )

        app.dependency_overrides.clear()
        assert resp.status_code == 401

    def test_server_principal_without_user_id_is_rejected(self, app, mock_db):
        """Server-only principals (no user_id) must be rejected by the auth guard —
        all custom operations require a user identity (via delegation token)."""
        ctx = AuthContext(user_id=None, principal_type="server", grants=[])
        app.dependency_overrides[get_auth] = lambda: ctx
        app.dependency_overrides[get_arango_db] = lambda: mock_db

        client = TestClient(app)
        resp = client.post(
            "/artifacts/some-artifact-id/op/resources_read",
            json={"uri": "ui://nexus/view.html"},
        )

        app.dependency_overrides.clear()
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# OperationNotDeclared → 404
# ---------------------------------------------------------------------------

class TestOperationNotDeclared:
    def test_undeclared_operation_returns_404(self, authed_client, mock_db):
        """If the artifact's type doesn't declare the requested op, the dispatcher
        raises OperationNotDeclared which the route converts to 404."""
        from services.operation_dispatcher import OperationNotDeclared

        doc = {
            "_key": "art-1",
            "context": {"content_type": "application/json"},
        }
        workspace_coll = MagicMock()
        workspace_coll.get.return_value = doc
        mock_db.collection.return_value = workspace_coll

        with patch(
            "services.operation_dispatcher.dispatch",
            new=AsyncMock(side_effect=OperationNotDeclared("operation not declared: custom_op")),
        ):
            resp = authed_client.post(
                "/artifacts/art-1/op/custom_op",
                json={},
            )

        assert resp.status_code == 404
        assert "not declared" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Happy-path dispatch
# ---------------------------------------------------------------------------

class TestDispatchHappyPath:
    def test_op_dispatch_returns_dispatcher_result(self, authed_client, mock_db):
        """Successful dispatch returns whatever the operation_dispatcher produces."""
        doc = {
            "_key": "srv-uuid-1",
            "context": {"content_type": "application/vnd.agience.mcp-server+json"},
        }
        workspace_coll = MagicMock()
        workspace_coll.get.return_value = doc
        mock_db.collection.return_value = workspace_coll

        expected_result = {"uri": "ui://nexus/view.html", "text": "<html>viewer</html>"}
        with patch("services.operation_dispatcher.dispatch", new=AsyncMock(return_value=expected_result)):
            resp = authed_client.post(
                "/artifacts/srv-uuid-1/op/resources_read",
                json={"uri": "ui://nexus/view.html", "workspace_id": "ws-1"},
            )

        assert resp.status_code == 200
        assert resp.json() == expected_result

    def test_op_dispatch_passes_body_and_context_to_dispatcher(self, authed_client, mock_db):
        """Body and DispatchContext are forwarded to operation_dispatcher.dispatch."""
        doc = {
            "_key": "srv-uuid-2",
            "context": {"content_type": "application/vnd.agience.mcp-server+json"},
        }
        workspace_coll = MagicMock()
        workspace_coll.get.return_value = doc
        mock_db.collection.return_value = workspace_coll

        mock_dispatch = AsyncMock(return_value={"ok": True})
        with patch("services.operation_dispatcher.dispatch", new=mock_dispatch):
            resp = authed_client.post(
                "/artifacts/srv-uuid-2/op/resources_import",
                json={"workspace_id": "ws-1", "resources": [{"uri": "r://a"}]},
            )

        assert resp.status_code == 200
        call_args = mock_dispatch.call_args
        # positional: (op_name, doc, body, dispatch_ctx)
        assert call_args[0][0] == "resources_import"
        body = call_args[0][2]
        assert body["workspace_id"] == "ws-1"
        assert body["resources"] == [{"uri": "r://a"}]
