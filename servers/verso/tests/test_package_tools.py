"""Tests for install_package / export_package helpers in Verso.

Focuses on the pure-logic helpers (_resolve_rewrite_value,
_infer_package_role) which encode the interesting behavior without
needing a running platform. The async tool coroutines themselves
(install_package, export_package) are exercised end-to-end in the
backend integration tests; here we verify the glue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_shared"))
sys.path.insert(0, str(_HERE.parent))

import server as _server  # noqa: E402


# ---------------------------------------------------------------------------
#  _resolve_rewrite_value
# ---------------------------------------------------------------------------

class TestResolveRewriteValue:
    def test_workspace_artifact_id_token_resolves(self):
        ref_map = {
            "agience://packages/foo/prompts/order": "new-artifact-123",
        }
        result = _server._resolve_rewrite_value(
            "${workspace_artifact_id(agience://packages/foo/prompts/order)}",
            ref_map,
        )
        assert result == "new-artifact-123"

    def test_unknown_ref_returns_none(self):
        result = _server._resolve_rewrite_value(
            "${workspace_artifact_id(agience://packages/foo/prompts/unknown)}",
            {},
        )
        assert result is None

    def test_literal_value_passes_through(self):
        result = _server._resolve_rewrite_value("some literal string", {})
        assert result == "some literal string"

    def test_malformed_token_passes_through(self):
        """No-match on the regex means we treat it as a literal."""
        result = _server._resolve_rewrite_value(
            "${workspace_artifact_id(no closing",
            {"something": "else"},
        )
        assert result == "${workspace_artifact_id(no closing"

    def test_quoted_ref_unquoted(self):
        """Tolerate quoting inside the parens."""
        ref_map = {"agience://packages/foo/prompts/order": "new-id"}
        result = _server._resolve_rewrite_value(
            '${workspace_artifact_id("agience://packages/foo/prompts/order")}',
            ref_map,
        )
        assert result == "new-id"

    def test_whitespace_tolerated(self):
        ref_map = {"ref-1": "id-1"}
        result = _server._resolve_rewrite_value(
            "  ${workspace_artifact_id(ref-1)}  ",
            ref_map,
        )
        assert result == "id-1"


# ---------------------------------------------------------------------------
#  _infer_package_role
# ---------------------------------------------------------------------------

class TestInferPackageRole:
    @pytest.mark.parametrize("content_type,expected", [
        ("application/vnd.agience.transform+json", "transform"),
        ("application/vnd.agience.mcp-server+json", "server"),
        ("application/vnd.agience.prompts+json",   "prompt"),
        ("text/markdown",                          "docs"),
        ("text/markdown; charset=utf-8",           "docs"),
        ("application/json",                       "artifact"),
        ("image/png",                              "artifact"),
    ])
    def test_inferred_from_content_type(self, content_type, expected):
        assert _server._infer_package_role(content_type, {}) == expected

    def test_context_type_prompt_overrides_default(self):
        assert _server._infer_package_role(
            "application/json", {"type": "prompt"},
        ) == "prompt"

    def test_context_type_docs_overrides_default(self):
        assert _server._infer_package_role(
            "application/json", {"type": "docs"},
        ) == "docs"

    def test_unknown_context_type_falls_back(self):
        assert _server._infer_package_role(
            "application/json", {"type": "random-thing"},
        ) == "artifact"

    def test_content_type_beats_context_type(self):
        """Content type wins when it's a known package-role MIME."""
        assert _server._infer_package_role(
            "application/vnd.agience.transform+json",
            {"type": "docs"},  # should be ignored
        ) == "transform"
