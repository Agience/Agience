"""Unit tests for the ask MCP tool.

Covers:
  - Global ask (no workspace_id) — searches globally, uses platform default LLM
  - Workspace-scoped ask — resolves ask binding, scopes search to bound collection
  - Custom system prompt — fetches prompt artifact from binding
  - No binding fallback — workspace_id provided but no ask binding, graceful global fallback
  - LLM failure — returns fallback message
  - Mode instructions — summarize, enumerate, lookup, compare
  - Content-type negotiation — accepts list, workspace renderer support, fallback
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from entities.artifact import Artifact as ArtifactEntity
from mcp_server.server import ask, _current_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call(user_id: str = "user-1", **kwargs):
    """Invoke the ask tool with the user context var set."""
    tok = _current_user_id.set(user_id)
    try:
        return ask(**kwargs)
    finally:
        _current_user_id.reset(tok)


def _make_hit(doc_id="a-1", title="Doc A", content="some content", score=0.9):
    return SimpleNamespace(doc_id=doc_id, title=title, content=content, description=None, score=score)


def _prompt_artifact(content="You are a custom expert assistant."):
    return ArtifactEntity(
        id="prompt-1",
        root_id="prompt-1",
        slug="ask-prompt",
        collection_id="col-prompts",
        context="{}",
        content=content,
        state="committed",
        created_by="user-1",
        modified_by="user-1",
        created_time="2026-04-13T00:00:00+00:00",
        modified_time="2026-04-13T00:00:00+00:00",
    )


def _mock_search_result(hits):
    return SimpleNamespace(hits=hits)


# ---------------------------------------------------------------------------
# Global ask (no workspace_id)
# ---------------------------------------------------------------------------

class TestAskGlobal:
    @patch("services.llm_service.complete", return_value=("Test answer", {}))
    @patch("mcp_server.server._get_arango")
    def test_global_ask_calls_llm_without_workspace(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="What is X?")

        assert result["content"] == "Test answer"
        assert result["content_type"] == "text/markdown"
        assert len(result["sources"]) == 1
        # workspace_id should be None in the complete() call
        call_kwargs = mock_complete.call_args
        assert call_kwargs.kwargs.get("workspace_id") is None

    @patch("mcp_server.server._get_arango")
    def test_global_ask_no_results(self, mock_arango):
        mock_arango.return_value = MagicMock()

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result([]),
        ):
            result = _call(question="What is X?")

        assert "couldn't find" in result["content"]
        assert result["content_type"] == "text/markdown"
        assert result["sources"] == []

    @patch("services.llm_service.complete", return_value=("Answer", {}))
    @patch("mcp_server.server._get_arango")
    def test_collection_ids_passed_to_search(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ) as mock_search:
            _call(question="What?", collection_ids=["col-1", "col-2"])

        search_query = mock_search.call_args[0][0]
        assert search_query.collection_ids == ["col-1", "col-2"]


# ---------------------------------------------------------------------------
# Workspace-scoped ask
# ---------------------------------------------------------------------------

class TestAskWorkspaceScoped:
    @patch("services.llm_service.complete", return_value=("Scoped answer", {}))
    @patch("services.workspace_service.get_workspace_context", return_value={
        "bindings": {"ask": {"collection_id": "col-scoped"}}
    })
    @patch("services.workspace_service.resolve_binding", return_value="col-scoped")
    @patch("mcp_server.server._get_arango")
    def test_workspace_scopes_search_to_bound_collection(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ) as mock_search:
            result = _call(question="What?", workspace_id="ws-1")

        # Search should be scoped to the bound collection
        search_query = mock_search.call_args[0][0]
        assert search_query.collection_ids == ["col-scoped"]
        # LLM call should include workspace_id
        assert mock_complete.call_args.kwargs["workspace_id"] == "ws-1"
        assert result["content"] == "Scoped answer"

    @patch("services.llm_service.complete", return_value=("Answer", {}))
    @patch("services.workspace_service.get_workspace_context", return_value={
        "bindings": {"ask": {"collection_id": "col-scoped"}}
    })
    @patch("services.workspace_service.resolve_binding", return_value="col-scoped")
    @patch("mcp_server.server._get_arango")
    def test_explicit_collection_ids_override_binding(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        """When collection_ids are explicitly provided, they take precedence over binding."""
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ) as mock_search:
            _call(question="What?", workspace_id="ws-1", collection_ids=["col-explicit"])

        search_query = mock_search.call_args[0][0]
        assert search_query.collection_ids == ["col-explicit"]


# ---------------------------------------------------------------------------
# Custom system prompt
# ---------------------------------------------------------------------------

class TestAskCustomSystemPrompt:
    @patch("services.llm_service.complete", return_value=("Custom answer", {}))
    @patch("db.arango.get_artifact")
    @patch("services.workspace_service.get_workspace_context", return_value={
        "bindings": {"ask": {"system_prompt_id": "prompt-1"}}
    })
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_custom_system_prompt_used(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_get_artifact, mock_complete
    ):
        mock_arango.return_value = MagicMock()
        mock_get_artifact.return_value = _prompt_artifact("You are a wine expert.")
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            _call(question="What wine?", workspace_id="ws-1")

        # Verify the custom prompt was passed in messages
        messages = mock_complete.call_args[0][2]
        assert messages[0]["role"] == "system"
        assert "wine expert" in messages[0]["content"]

    @patch("services.llm_service.complete", return_value=("Default answer", {}))
    @patch("services.workspace_service.get_workspace_context", return_value={
        "bindings": {"ask": {}}
    })
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_default_prompt_when_no_system_prompt_binding(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            _call(question="What?", workspace_id="ws-1")

        messages = mock_complete.call_args[0][2]
        assert "knowledge assistant" in messages[0]["content"]


# ---------------------------------------------------------------------------
# No binding fallback
# ---------------------------------------------------------------------------

class TestAskNoBinding:
    @patch("services.llm_service.complete", return_value=("Global fallback", {}))
    @patch("services.workspace_service.get_workspace_context", return_value={})
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_no_binding_falls_back_to_global_search(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ) as mock_search:
            result = _call(question="What?", workspace_id="ws-1")

        # No collection_ids constraint — global search
        search_query = mock_search.call_args[0][0]
        assert search_query.collection_ids is None
        assert result["content"] == "Global fallback"


# ---------------------------------------------------------------------------
# LLM failure
# ---------------------------------------------------------------------------

class TestAskLlmFailure:
    @patch("services.llm_service.complete", side_effect=RuntimeError("API down"))
    @patch("services.workspace_service.get_workspace_context", return_value={})
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_llm_failure_returns_fallback_message(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="What?", workspace_id="ws-1")

        assert "unable to synthesize" in result["content"]
        assert len(result["sources"]) == 1


# ---------------------------------------------------------------------------
# Mode instructions
# ---------------------------------------------------------------------------

class TestAskModes:
    @patch("services.llm_service.complete", return_value=("summary text", {}))
    @patch("mcp_server.server._get_arango")
    def test_default_mode_is_summarize(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            _call(question="What?")

        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "Synthesize a concise answer" in system_msg

    @patch("services.llm_service.complete", return_value=("| col1 | col2 |", {}))
    @patch("mcp_server.server._get_arango")
    def test_enumerate_mode(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="List items", mode="enumerate")

        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "structured list or table" in system_msg
        assert result["content"] == "| col1 | col2 |"

    @patch("services.llm_service.complete", return_value=("raw content", {}))
    @patch("mcp_server.server._get_arango")
    def test_lookup_mode(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            _call(question="What is X?", mode="lookup")

        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "directly" in system_msg
        assert "Do not rephrase" in system_msg

    @patch("services.llm_service.complete", return_value=("diff output", {}))
    @patch("mcp_server.server._get_arango")
    def test_compare_mode(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            _call(question="Compare A and B", mode="compare")

        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "Compare" in system_msg
        assert "contradictions" in system_msg

    @patch("services.llm_service.complete", return_value=("answer", {}))
    @patch("mcp_server.server._get_arango")
    def test_invalid_mode_falls_back_to_summarize(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            _call(question="What?", mode="nonexistent")

        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "Synthesize a concise answer" in system_msg


# ---------------------------------------------------------------------------
# Content-type negotiation
# ---------------------------------------------------------------------------

class TestAskContentType:
    @patch("services.llm_service.complete", return_value=("graph TD\n  A-->B", {}))
    @patch("mcp_server.server._get_arango")
    def test_mermaid_accepted(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="What depends on X?", accepts=["text/mermaid"])

        assert result["content_type"] == "text/mermaid"
        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "Mermaid diagram" in system_msg

    @patch("services.llm_service.complete", return_value=("id,name\n1,foo", {}))
    @patch("mcp_server.server._get_arango")
    def test_csv_accepted(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="List all", accepts=["text/csv"])

        assert result["content_type"] == "text/csv"
        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "CSV" in system_msg

    @patch("services.llm_service.complete", return_value=("answer", {}))
    @patch("mcp_server.server._get_arango")
    def test_unsupported_type_falls_back_to_markdown(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="What?", accepts=["image/png", "audio/mp3"])

        # Neither type is supported → falls back to text/markdown
        assert result["content_type"] == "text/markdown"

    @patch("services.llm_service.complete", return_value=("answer", {}))
    @patch("mcp_server.server._get_arango")
    def test_preference_order_respected(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="What?", accepts=["text/csv", "text/mermaid", "text/markdown"])

        # First supported type wins
        assert result["content_type"] == "text/csv"

    @patch("services.llm_service.complete", return_value=("{}", {}))
    @patch("mcp_server.server._get_arango")
    def test_json_content_type(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="What?", accepts=["application/json"])

        assert result["content_type"] == "application/json"
        system_msg = mock_complete.call_args[0][2][0]["content"]
        assert "valid JSON" in system_msg

    @patch("services.llm_service.complete", return_value=("answer", {}))
    @patch("mcp_server.server._get_arango")
    def test_default_accepts_is_markdown(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(question="What?")

        assert result["content_type"] == "text/markdown"


# ---------------------------------------------------------------------------
# Workspace renderer support
# ---------------------------------------------------------------------------

class TestAskWorkspaceRendererSupport:
    @patch("services.llm_service.complete", return_value=("answer", {}))
    @patch("services.workspace_service.get_workspace_context", return_value={
        "bindings": {"ask": {
            "supported_types": ["text/markdown", "text/plain"],
        }}
    })
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_workspace_filters_unsupported_types(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(
                question="What?",
                workspace_id="ws-1",
                accepts=["text/mermaid", "text/csv", "text/markdown"],
            )

        # Mermaid and CSV not in workspace supported_types → falls to text/markdown
        assert result["content_type"] == "text/markdown"

    @patch("services.llm_service.complete", return_value=("answer", {}))
    @patch("services.workspace_service.get_workspace_context", return_value={
        "bindings": {"ask": {
            "supported_types": ["text/markdown", "text/mermaid", "text/csv"],
        }}
    })
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_workspace_allows_supported_type(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(
                question="What?",
                workspace_id="ws-1",
                accepts=["text/mermaid"],
            )

        assert result["content_type"] == "text/mermaid"

    @patch("services.llm_service.complete", return_value=("answer", {}))
    @patch("services.workspace_service.get_workspace_context", return_value={
        "bindings": {"ask": {}}
    })
    @patch("services.workspace_service.resolve_binding", return_value=None)
    @patch("mcp_server.server._get_arango")
    def test_no_supported_types_means_all_allowed(
        self, mock_arango, mock_resolve, mock_ws_ctx, mock_complete
    ):
        """When workspace doesn't declare supported_types, all platform types are allowed."""
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(
                question="What?",
                workspace_id="ws-1",
                accepts=["text/mermaid"],
            )

        assert result["content_type"] == "text/mermaid"


# ---------------------------------------------------------------------------
# Mode + content-type combined
# ---------------------------------------------------------------------------

class TestAskModeAndContentType:
    @patch("services.llm_service.complete", return_value=("graph TD\n  A-->B", {}))
    @patch("mcp_server.server._get_arango")
    def test_enumerate_mode_with_mermaid(self, mock_arango, mock_complete):
        mock_arango.return_value = MagicMock()
        hits = [_make_hit()]

        with patch(
            "search.accessor.search_accessor.SearchAccessor.search",
            return_value=_mock_search_result(hits),
        ):
            result = _call(
                question="What depends on X?",
                mode="enumerate",
                accepts=["text/mermaid"],
            )

        assert result["content_type"] == "text/mermaid"
        system_msg = mock_complete.call_args[0][2][0]["content"]
        # System prompt should contain both mode and format instructions
        assert "structured list or table" in system_msg
        assert "Mermaid diagram" in system_msg
