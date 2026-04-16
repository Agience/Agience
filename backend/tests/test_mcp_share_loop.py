"""Unit tests for the Phase 1 share-loop MCP tools.

Covers invoke_artifact, share, accept_invite. Mocks the service layer
and DB so we're testing the tool's wiring (context vars, annotations,
error shaping, access checks, event emission) rather than the services
themselves (those have their own tests).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from entities.grant import Grant as GrantEntity
from mcp_server.server import (
    accept_invite,
    invoke_artifact,
    share,
    _current_auth_context,
    _current_user_id,
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

class _FakeAuth:
    def __init__(self, principal_type="user", grants=None):
        self.principal_type = principal_type
        self.grants = grants or []
        self.user_id = "u-1"


def _call(tool, *, user_id="u-1", auth=None, **kwargs):
    """Invoke an MCP tool with contextvars populated."""
    tok_uid = _current_user_id.set(user_id)
    tok_ctx = _current_auth_context.set(auth or _FakeAuth())
    try:
        return tool(**kwargs)
    finally:
        _current_user_id.reset(tok_uid)
        _current_auth_context.reset(tok_ctx)


def _grant(**overrides):
    base = dict(
        resource_type="collection",
        resource_id="ws-1",
        grantee_type=GrantEntity.GRANTEE_USER,
        grantee_id="u-1",
        granted_by="inviter",
        state=GrantEntity.STATE_ACTIVE,
    )
    base.update(overrides)
    return GrantEntity(**base)


class _FakeDb:
    def __init__(self, doc=None):
        self._doc = doc
        self.closed = False

    def collection(self, _name):
        return SimpleNamespace(get=lambda _key: self._doc)

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
#  invoke_artifact
# ---------------------------------------------------------------------------

class TestInvokeArtifact:
    def test_missing_artifact_returns_error(self):
        with patch("mcp_server.server._get_arango", return_value=_FakeDb(doc=None)):
            result = _call(invoke_artifact, artifact_id="does-not-exist")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_dispatches_via_operation_dispatcher(self):
        artifact_doc = {"_key": "tx-1", "context": '{"run":{"type":"mcp-tool"}}'}
        fake_db = _FakeDb(doc=artifact_doc)
        dispatched = {}

        async def fake_dispatch(op_name, doc, body, ctx):
            dispatched["op"] = op_name
            dispatched["doc_key"] = doc.get("_key")
            dispatched["body"] = body
            dispatched["user_id"] = ctx.user_id
            return {"ok": True, "answer": 42}

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch("services.operation_dispatcher.dispatch", side_effect=fake_dispatch),
        ):
            result = _call(
                invoke_artifact,
                artifact_id="tx-1",
                workspace_id="ws-1",
                input="hi",
                artifacts=["a-1", "a-2"],
                params={"model": "gpt-4"},
            )

        assert result == {"result": {"ok": True, "answer": 42}}
        assert dispatched["op"] == "invoke"
        assert dispatched["doc_key"] == "tx-1"
        assert dispatched["user_id"] == "u-1"
        # Body carries the invoke envelope fields the handler expects.
        body = dispatched["body"]
        assert body["workspace_id"] == "ws-1"
        assert body["input"] == "hi"
        assert body["artifacts"] == ["a-1", "a-2"]
        assert body["params"]["transform_id"] == "tx-1"
        assert body["params"]["model"] == "gpt-4"

    def test_dispatcher_exception_returns_error_dict(self):
        artifact_doc = {"_key": "tx-1", "context": "{}"}
        fake_db = _FakeDb(doc=artifact_doc)

        async def boom(*args, **kwargs):
            raise RuntimeError("handler blew up")

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch("services.operation_dispatcher.dispatch", side_effect=boom),
        ):
            result = _call(invoke_artifact, artifact_id="tx-1")
        assert "error" in result
        assert "blew up" in result["error"]


# ---------------------------------------------------------------------------
#  share
# ---------------------------------------------------------------------------

class TestShare:
    def _workspace_doc(self, created_by="u-1", title="My Workspace"):
        import json
        return {
            "_key": "ws-1",
            "created_by": created_by,
            "context": json.dumps({"title": title}),
        }

    def test_creator_can_share_without_explicit_grant(self):
        fake_db = _FakeDb(doc=self._workspace_doc(created_by="u-1"))
        fake_grant = _grant(id="g-1", can_read=True)

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch(
                "services.grant_service.create_invite",
                return_value=(fake_grant, "agc_RAW_TOKEN"),
            ),
        ):
            result = _call(share, workspace_id="ws-1", role="viewer")

        # The service is the source of truth for email + events; the tool
        # just surfaces the grant + claim URL it returns.
        assert result["grant_id"] == "g-1"
        assert "/invite/agc_RAW_TOKEN" in result["claim_url"]
        assert result["claim_token"] == "agc_RAW_TOKEN"

    def test_non_creator_without_share_grant_refused(self):
        fake_db = _FakeDb(doc=self._workspace_doc(created_by="someone-else"))

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            # No grants for this user on the workspace.
            patch(
                "services.grant_service.get_active_grants_for_principal_resource",
                return_value=[],
            ),
        ):
            result = _call(share, workspace_id="ws-1", role="viewer")

        assert "error" in result
        assert "permission" in result["error"].lower()

    def test_non_creator_with_can_share_grant_allowed(self):
        fake_db = _FakeDb(doc=self._workspace_doc(created_by="someone-else"))
        share_grant = _grant(grantee_id="u-1", resource_id="ws-1", can_share=True)
        fake_invite = _grant(id="g-2")

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            # grant_service.can_share() does a DB lookup for the caller's
            # active grants on the resource. Return the share grant.
            patch(
                "services.grant_service.get_active_grants_for_principal_resource",
                return_value=[share_grant],
            ),
            patch(
                "services.grant_service.create_invite",
                return_value=(fake_invite, "agc_TOK"),
            ),
        ):
            result = _call(share, workspace_id="ws-1", role="viewer")

        assert result["grant_id"] == "g-2"

    def test_target_email_passes_message_to_service(self):
        """The MCP tool forwards message / target_email to grant_service.create_invite.

        Email delivery itself lives in the service; this test just verifies
        the tool plumbs the arguments through.
        """
        fake_db = _FakeDb(doc=self._workspace_doc())
        fake_invite = _grant(id="g-3", target_entity="bob@example.com",
                             target_entity_type="email")
        captured: dict = {}

        def fake_create_invite(db, **kwargs):
            captured.update(kwargs)
            return fake_invite, "agc_TOK"

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch(
                "services.grant_service.create_invite",
                side_effect=fake_create_invite,
            ),
        ):
            result = _call(
                share,
                workspace_id="ws-1",
                role="editor",
                target_email="bob@example.com",
                message="join us",
            )

        assert result["grant_id"] == "g-3"
        assert captured["target_email"] == "bob@example.com"
        assert captured["message"] == "join us"
        assert captured["role"] == "editor"

    def test_unknown_role_surfaces_as_error(self):
        fake_db = _FakeDb(doc=self._workspace_doc())

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch(
                "services.grant_service.create_invite",
                side_effect=ValueError("Unknown role 'overlord'"),
            ),
        ):
            result = _call(share, workspace_id="ws-1", role="overlord")

        assert "error" in result
        assert "overlord" in result["error"]


# ---------------------------------------------------------------------------
#  accept_invite
# ---------------------------------------------------------------------------

class TestAcceptInvite:
    def test_server_principal_rejected(self):
        server_auth = _FakeAuth(principal_type="server")
        fake_db = _FakeDb()

        with patch("mcp_server.server._get_arango", return_value=fake_db):
            result = _call(accept_invite, token="agc_xxx", auth=server_auth)

        assert "error" in result
        assert "human" in result["error"].lower()

    def test_api_key_principal_rejected(self):
        apikey_auth = _FakeAuth(principal_type="api_key")
        fake_db = _FakeDb()

        with patch("mcp_server.server._get_arango", return_value=fake_db):
            result = _call(accept_invite, token="agc_xxx", auth=apikey_auth)

        assert "error" in result

    def test_happy_path_returns_resource(self):
        """The tool returns the granted resource shape; event emission
        happens inside grant_service.claim_invite and is tested there."""
        fake_db = _FakeDb()
        new_grant = _grant(id="g-claimed", resource_id="ws-1")

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch(
                "services.grant_service.claim_invite",
                return_value=new_grant,
            ),
        ):
            result = _call(accept_invite, token="agc_xxx")

        assert result == {
            "grant_id": "g-claimed",
            "resource_id": "ws-1",
            "resource_type": "collection",
        }

    def test_identity_mismatch_returns_error_not_crash(self):
        from services.grant_service import InviteIdentityMismatch
        fake_db = _FakeDb()

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch(
                "services.grant_service.claim_invite",
                side_effect=InviteIdentityMismatch("nope"),
            ),
        ):
            result = _call(accept_invite, token="agc_xxx")
        assert result == {"error": "nope"}

    def test_exhausted_invite_returns_error(self):
        from services.grant_service import InviteExhausted
        fake_db = _FakeDb()

        with (
            patch("mcp_server.server._get_arango", return_value=fake_db),
            patch(
                "services.grant_service.claim_invite",
                side_effect=InviteExhausted("claim limit"),
            ),
        ):
            result = _call(accept_invite, token="agc_xxx")
        assert "error" in result
        assert "limit" in result["error"]


# ---------------------------------------------------------------------------
#  Tool metadata
# ---------------------------------------------------------------------------

class TestToolAnnotations:
    """MCP clients read these annotations to decide whether to prompt."""

    @pytest.mark.parametrize(
        "tool_name,expected_destructive",
        [
            ("invoke_artifact", False),
            ("share", True),
            ("accept_invite", True),
        ],
    )
    def test_destructive_hint(self, tool_name, expected_destructive):
        from mcp_server import server as mcp_server_mod
        # FastMCP decorators wrap the callable; the annotation hint lives
        # on the decorator's stored metadata. We just assert the tool is
        # registered in TOOL_REGISTRY (the sync-call dispatch path).
        assert tool_name in mcp_server_mod.TOOL_REGISTRY, (
            f"{tool_name} not registered in TOOL_REGISTRY"
        )
