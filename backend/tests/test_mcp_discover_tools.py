"""Unit tests for discover_tools binding awareness (Phase 3).

Covers:
  - Tools binding scopes discovery to the bound collection's servers
  - No binding falls back to workspace or global server enumeration
  - Query filtering works with binding-scoped servers
  - Empty bound collection returns empty tool list
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mcp_server.server import discover_tools, _current_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call(user_id: str = "user-1", **kwargs):
    """Invoke discover_tools with the user context var set."""
    tok = _current_user_id.set(user_id)
    try:
        return discover_tools(**kwargs)
    finally:
        _current_user_id.reset(tok)


def _server_info(name: str, tools: list) -> SimpleNamespace:
    return SimpleNamespace(
        server=name,
        name=name,
        id=f"id-{name}",
        server_name=name,
        server_id=f"id-{name}",
        tools=[SimpleNamespace(name=t, description=f"{t} desc") for t in tools],
        status="ok",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDiscoverToolsBindings:

    @patch("mcp_server.server._get_arango")
    @patch("services.mcp_service.list_servers_from_collection")
    @patch("services.workspace_service.resolve_binding", return_value="col-tools")
    def test_tools_binding_scopes_discovery(self, mock_resolve, mock_list_col, mock_arango):
        mock_arango.return_value = MagicMock()
        mock_list_col.return_value = [
            _server_info("my-server", ["tool_a", "tool_b"]),
        ]
        result = _call(workspace_id="ws-1")
        assert result["count"] == 2
        tool_names = {t["tool"] for t in result["tools"]}
        assert tool_names == {"tool_a", "tool_b"}
        # Verify it used the binding path, not the workspace path
        mock_list_col.assert_called_once()

    @patch("mcp_server.server._get_arango")
    @patch("services.mcp_service.list_servers_for_workspace")
    @patch("services.workspace_service.resolve_binding", return_value=None)
    def test_no_binding_falls_back(self, mock_resolve, mock_list_ws, mock_arango):
        mock_arango.return_value = MagicMock()
        mock_list_ws.return_value = [
            _server_info("all-server", ["search", "browse"]),
        ]
        result = _call(workspace_id="ws-1")
        assert result["count"] == 2
        mock_list_ws.assert_called_once()

    @patch("mcp_server.server._get_arango")
    @patch("services.mcp_service.list_servers_from_collection")
    @patch("services.workspace_service.resolve_binding", return_value="col-tools")
    def test_query_filter_with_binding(self, mock_resolve, mock_list_col, mock_arango):
        mock_arango.return_value = MagicMock()
        mock_list_col.return_value = [
            _server_info("srv", ["search", "ingest", "extract"]),
        ]
        result = _call(workspace_id="ws-1", query="search")
        assert result["count"] == 1
        assert result["tools"][0]["tool"] == "search"

    @patch("mcp_server.server._get_arango")
    @patch("services.mcp_service.list_servers_from_collection")
    @patch("services.workspace_service.resolve_binding", return_value="col-tools")
    def test_empty_bound_collection(self, mock_resolve, mock_list_col, mock_arango):
        mock_arango.return_value = MagicMock()
        mock_list_col.return_value = []
        result = _call(workspace_id="ws-1")
        assert result["count"] == 0
        assert result["tools"] == []
