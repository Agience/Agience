"""Unit tests for the memory MCP tool (workspace bindings Phase 2).

Covers the memory tool's five commands:
  - read: find artifact by slug in bound collection, return content
  - write: create-or-update artifact by slug
  - list: list all artifacts in bound collection
  - search: scoped search within bound collection
  - delete: archive artifact by slug
  - error paths: no binding, missing key, unknown command
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from entities.artifact import Artifact as ArtifactEntity
from mcp_server.server import memory, _current_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call(user_id: str = "user-1", **kwargs):
    """Invoke the memory tool with the user context var set."""
    tok = _current_user_id.set(user_id)
    try:
        return memory(**kwargs)
    finally:
        _current_user_id.reset(tok)


def _artifact(
    aid: str = "a-1",
    slug: str = "decisions",
    content: str = "some content",
    context: str = '{"type":"memory"}',
    state: str = "draft",
    collection_id: str = "col-mem",
) -> ArtifactEntity:
    return ArtifactEntity(
        id=aid,
        root_id=aid,
        slug=slug,
        collection_id=collection_id,
        context=context,
        content=content,
        state=state,
        created_by="user-1",
        modified_by="user-1",
        created_time="2026-04-13T00:00:00+00:00",
        modified_time="2026-04-13T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# No binding
# ---------------------------------------------------------------------------

class TestMemoryNoBinding:
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_no_binding_returns_error(self, mock_arango, mock_resolve):
        mock_arango.return_value = MagicMock()
        result = _call(workspace_id="ws-1", command="read", key="x")
        assert "error" in result
        assert "memory" in result["error"]


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

class TestMemoryRead:
    @patch("db.arango.find_artifact_by_slug_in_collection")
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_read_returns_content(self, mock_arango, mock_resolve, mock_find):
        mock_arango.return_value = MagicMock()
        art = _artifact()
        mock_find.return_value = art
        result = _call(workspace_id="ws-1", command="read", key="decisions")
        assert result["key"] == "decisions"
        assert result["artifact_id"] == "a-1"
        assert result["content"] == "some content"

    @patch("db.arango.find_artifact_by_slug_in_collection", return_value=None)
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_read_not_found(self, mock_arango, mock_resolve, mock_find):
        mock_arango.return_value = MagicMock()
        result = _call(workspace_id="ws-1", command="read", key="missing")
        assert "error" in result

    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_read_missing_key_returns_error(self, mock_arango, mock_resolve):
        mock_arango.return_value = MagicMock()
        result = _call(workspace_id="ws-1", command="read")
        assert "error" in result


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

class TestMemoryWrite:
    @patch("services.workspace_service.create_workspace_artifact")
    @patch("db.arango.find_artifact_by_slug_in_collection", return_value=None)
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_write_creates_new(self, mock_arango, mock_resolve, mock_find, mock_create):
        mock_arango.return_value = MagicMock()
        created = _artifact(aid="a-new", slug="notes")
        mock_create.return_value = created
        result = _call(
            workspace_id="ws-1", command="write",
            key="notes", content="hello world",
        )
        assert result["action"] == "created"
        assert result["key"] == "notes"
        assert result["artifact_id"] == "a-new"
        mock_create.assert_called_once()

    @patch("services.workspace_service.update_artifact")
    @patch("db.arango.find_artifact_by_slug_in_collection")
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_write_updates_existing(self, mock_arango, mock_resolve, mock_find, mock_update):
        mock_arango.return_value = MagicMock()
        existing = _artifact(aid="a-1", slug="decisions")
        mock_find.return_value = existing
        updated = _artifact(aid="a-1", slug="decisions", content="updated")
        mock_update.return_value = updated
        result = _call(
            workspace_id="ws-1", command="write",
            key="decisions", content="updated",
        )
        assert result["action"] == "updated"
        assert result["artifact_id"] == "a-1"
        mock_update.assert_called_once()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestMemoryList:
    @patch("db.arango.list_collection_artifacts")
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_list_returns_all(self, mock_arango, mock_resolve, mock_list):
        mock_arango.return_value = MagicMock()
        mock_list.return_value = [
            {"id": "a-1", "slug": "decisions", "state": "draft", "content_type": None, "created_time": ""},
            {"id": "a-2", "slug": "notes", "state": "draft", "content_type": None, "created_time": ""},
        ]
        result = _call(workspace_id="ws-1", command="list")
        assert result["collection_id"] == "col-mem"
        assert result["count"] == 2
        assert len(result["items"]) == 2


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

class TestMemoryDelete:
    @patch("services.workspace_service.update_artifact")
    @patch("db.arango.find_artifact_by_slug_in_collection")
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_delete_archives(self, mock_arango, mock_resolve, mock_find, mock_update):
        mock_arango.return_value = MagicMock()
        art = _artifact(aid="a-1", slug="old-notes")
        mock_find.return_value = art
        mock_update.return_value = art
        result = _call(workspace_id="ws-1", command="delete", key="old-notes")
        assert result["action"] == "archived"
        assert result["artifact_id"] == "a-1"

    @patch("db.arango.find_artifact_by_slug_in_collection", return_value=None)
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    @patch("mcp_server.server._get_arango")
    def test_delete_not_found(self, mock_arango, mock_resolve, mock_find):
        mock_arango.return_value = MagicMock()
        result = _call(workspace_id="ws-1", command="delete", key="gone")
        assert "error" in result


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestMemorySearch:
    @patch("mcp_server.server._get_arango")
    @patch("services.workspace_service.resolve_binding", return_value="col-mem")
    def test_search_scopes_to_collection(self, mock_resolve, mock_arango):
        mock_arango.return_value = MagicMock()
        mock_result = SimpleNamespace(
            total=1,
            hits=[
                SimpleNamespace(doc_id="a-1", title="Decisions", score=0.9, content="some text"),
            ],
        )
        with patch("search.accessor.search_accessor.SearchAccessor") as MockAccessor:
            instance = MockAccessor.return_value
            instance.search.return_value = mock_result
            result = _call(workspace_id="ws-1", command="search", query="decisions")

        assert result["collection_id"] == "col-mem"
        assert result["total"] == 1
        assert len(result["hits"]) == 1
        # Verify collection_ids was passed to scope the search
        search_call = instance.search.call_args[0][0]
        assert search_call.collection_ids == ["col-mem"]
