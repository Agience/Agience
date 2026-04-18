"""Unit tests for services.llm_service.

Covers the workspace → user → environment fallback chain for LLM API key
resolution, plus workspace-context CRUD wrappers.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services import llm_service


class TestGetLlmKeyForWorkspace:
    def _patch_arango(self):
        return patch("services.llm_service.get_arango_db", return_value=iter([MagicMock()]))

    def test_workspace_specific_key_wins(self):
        with (
            self._patch_arango(),
            patch(
                "services.workspace_service.get_workspace_context",
                return_value={"llm": {"key_id": "secret-1"}},
            ),
            patch(
                "services.secrets_service.get_secret_value",
                return_value="ws-key",
            ) as get_secret,
        ):
            out = llm_service.get_llm_key_for_workspace(
                MagicMock(), "user-1", "ws-1", provider="openai"
            )
        assert out == "ws-key"
        # Called with the secret_id from the workspace context.
        assert get_secret.call_args.kwargs["secret_id"] == "secret-1"

    def test_user_default_when_no_workspace_key(self):
        """Workspace has no llm.key_id → router skips workspace lookup and
        falls through to the user-default secret."""
        with (
            self._patch_arango(),
            patch(
                "services.workspace_service.get_workspace_context",
                return_value={},
            ),
            patch(
                "services.secrets_service.get_secret_value",
                return_value="user-default",
            ) as get_secret,
        ):
            out = llm_service.get_llm_key_for_workspace(
                MagicMock(), "user-1", "ws-1", provider="openai"
            )
        assert out == "user-default"
        # Only the user-default lookup ran (no secret_id passed).
        assert get_secret.call_count == 1
        assert get_secret.call_args.kwargs.get("secret_id") is None

    def test_falls_back_to_env_openai_key(self):
        with (
            self._patch_arango(),
            patch(
                "services.workspace_service.get_workspace_context", return_value={}
            ),
            patch(
                "services.secrets_service.get_secret_value", return_value=None
            ),
            patch("core.config.OPENAI_API_KEY", "sk-env-default"),
        ):
            out = llm_service.get_llm_key_for_workspace(
                MagicMock(), "user-1", "ws-1", provider="openai"
            )
        assert out == "sk-env-default"

    def test_falls_back_to_env_anthropic_key(self):
        with (
            self._patch_arango(),
            patch(
                "services.workspace_service.get_workspace_context", return_value={}
            ),
            patch(
                "services.secrets_service.get_secret_value", return_value=None
            ),
            patch("core.config.ANTHROPIC_API_KEY", "sk-ant-env"),
        ):
            out = llm_service.get_llm_key_for_workspace(
                MagicMock(), "user-1", "ws-1", provider="anthropic"
            )
        assert out == "sk-ant-env"

    def test_returns_none_for_unknown_provider(self):
        with (
            self._patch_arango(),
            patch(
                "services.workspace_service.get_workspace_context", return_value={}
            ),
            patch(
                "services.secrets_service.get_secret_value", return_value=None
            ),
        ):
            out = llm_service.get_llm_key_for_workspace(
                MagicMock(), "user-1", "ws-1", provider="custom-provider"
            )
        assert out is None

    def test_workspace_context_exception_falls_through(self):
        with (
            self._patch_arango(),
            patch(
                "services.workspace_service.get_workspace_context",
                side_effect=RuntimeError("ws lookup failed"),
            ),
            patch(
                "services.secrets_service.get_secret_value",
                return_value="user-default",
            ),
        ):
            out = llm_service.get_llm_key_for_workspace(
                MagicMock(), "user-1", "ws-1", provider="openai"
            )
        # Exception in workspace lookup is swallowed; user-default still resolved.
        assert out == "user-default"


class TestSetWorkspaceLlm:
    def test_writes_llm_block_into_context(self):
        captured = {}

        def fake_update(db, user_id, ws_id, ctx):
            captured["context"] = ctx

        with (
            patch(
                "services.workspace_service.get_workspace_context", return_value={}
            ),
            patch(
                "services.workspace_service.update_workspace_context",
                side_effect=fake_update,
            ),
        ):
            llm_service.set_workspace_llm(
                MagicMock(),
                "user-1",
                "ws-1",
                provider="openai",
                model="gpt-4o",
                key_id="secret-1",
            )
        assert captured["context"]["llm"] == {
            "provider": "openai",
            "model": "gpt-4o",
            "key_id": "secret-1",
        }

    def test_handles_non_dict_context(self):
        with (
            patch(
                "services.workspace_service.get_workspace_context", return_value=None
            ),
            patch(
                "services.workspace_service.update_workspace_context"
            ) as upd,
        ):
            llm_service.set_workspace_llm(
                MagicMock(), "user-1", "ws-1", provider="openai", model="gpt-4o"
            )
        # Falls back to {} and writes the llm block.
        ctx = upd.call_args[0][3]
        assert ctx["llm"]["model"] == "gpt-4o"


class TestClearWorkspaceLlm:
    def test_removes_llm_block(self):
        captured = {}

        def fake_update(db, user_id, ws_id, ctx):
            captured["context"] = ctx

        with (
            patch(
                "services.workspace_service.get_workspace_context",
                return_value={"llm": {"provider": "openai"}, "other": "stuff"},
            ),
            patch(
                "services.workspace_service.update_workspace_context",
                side_effect=fake_update,
            ),
        ):
            llm_service.clear_workspace_llm(MagicMock(), "user-1", "ws-1")
        assert "llm" not in captured["context"]
        assert captured["context"]["other"] == "stuff"

    def test_noop_when_no_llm_block(self):
        with (
            patch(
                "services.workspace_service.get_workspace_context",
                return_value={"other": "stuff"},
            ),
            patch(
                "services.workspace_service.update_workspace_context"
            ) as upd,
        ):
            llm_service.clear_workspace_llm(MagicMock(), "user-1", "ws-1")
        upd.assert_not_called()

    def test_noop_when_context_is_none(self):
        with (
            patch(
                "services.workspace_service.get_workspace_context", return_value=None
            ),
            patch(
                "services.workspace_service.update_workspace_context"
            ) as upd,
        ):
            llm_service.clear_workspace_llm(MagicMock(), "user-1", "ws-1")
        upd.assert_not_called()


class TestDispatchAnthropicMessageConversion:
    """Verify that _dispatch_anthropic extracts system messages correctly."""

    def _inject_mock_anthropic(self, mock_client):
        """Inject a mock anthropic module into sys.modules (mirrors Google test pattern)."""
        import sys

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = mock_anthropic
        return saved

    def _restore_anthropic(self, saved):
        import sys
        if saved is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = saved

    def test_system_messages_extracted_to_system_param(self):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(type="text", text="reply")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        saved = self._inject_mock_anthropic(mock_client)
        try:
            text, _ = llm_service._dispatch_anthropic(
                "claude-sonnet-4-20250514",
                [
                    {"role": "system", "content": "You are a helper."},
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "Hello"},
                ],
                "sk-test",
                0.5,
                1024,
            )
        finally:
            self._restore_anthropic(saved)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        # System messages joined and passed as `system` param
        assert "You are a helper." in call_kwargs["system"]
        assert "Be concise." in call_kwargs["system"]
        # Messages should only contain non-system messages
        assert all(m["role"] != "system" for m in call_kwargs["messages"])
        assert call_kwargs["messages"][0]["content"] == "Hello"
        assert text == "reply"

    def test_no_system_messages_omits_system_param(self):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(type="text", text="ok")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        saved = self._inject_mock_anthropic(mock_client)
        try:
            llm_service._dispatch_anthropic(
                "claude-sonnet-4-20250514",
                [{"role": "user", "content": "Hello"}],
                "sk-test",
                0.5,
                1024,
            )
        finally:
            self._restore_anthropic(saved)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert "system" not in call_kwargs


class TestDispatchGoogleMessageConversion:
    """Verify that _dispatch_google converts message format correctly."""

    def test_system_messages_become_system_instruction(self):
        import sys

        mock_resp = MagicMock()
        mock_resp.text = "google reply"
        mock_client_cls = MagicMock()
        mock_client_inst = MagicMock()
        mock_client_inst.models.generate_content.return_value = mock_resp
        mock_client_cls.return_value = mock_client_inst

        # Create mock google.genai module with real-enough types
        mock_types = MagicMock()
        # Make Content and Part just store their kwargs
        mock_types.Content = lambda **kw: kw
        mock_types.Part = lambda **kw: kw
        mock_types.GenerateContentConfig = MagicMock

        mock_genai = MagicMock()
        mock_genai.Client = mock_client_cls
        mock_genai.types = mock_types

        mock_google = MagicMock()
        mock_google.genai = mock_genai

        # Temporarily inject mock modules
        saved = {}
        for mod_name in ("google", "google.genai", "google.genai.types"):
            saved[mod_name] = sys.modules.get(mod_name)

        sys.modules["google"] = mock_google
        sys.modules["google.genai"] = mock_genai
        sys.modules["google.genai.types"] = mock_types

        try:
            # Re-import to pick up mock
            import importlib
            importlib.reload(llm_service)

            text, _ = llm_service._dispatch_google(
                "gemini-2.0-flash",
                [
                    {"role": "system", "content": "Be helpful."},
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi there"},
                    {"role": "user", "content": "Thanks"},
                ],
                "goog-key",
                0.5,
                1024,
            )

            call_kwargs = mock_client_inst.models.generate_content.call_args.kwargs
            # Should have 3 content items (user, assistant->model, user) — system extracted
            contents = call_kwargs["contents"]
            assert len(contents) == 3
            # Verify role mapping: assistant -> model
            assert contents[1]["role"] == "model"
            assert text == "google reply"
        finally:
            # Restore original module state
            for mod_name, original in saved.items():
                if original is None:
                    sys.modules.pop(mod_name, None)
                else:
                    sys.modules[mod_name] = original
            importlib.reload(llm_service)


class TestComplete:
    """Tests for the multi-provider ``complete()`` dispatch entry point."""

    def _patch_arango(self):
        return patch("services.llm_service.get_arango_db", return_value=iter([MagicMock()]))

    def test_openai_dispatch(self):
        """Default provider dispatches to openai_helpers."""
        with (
            self._patch_arango(),
            patch("core.config.AI_DEFAULT_PROVIDER", "openai"),
            patch("core.config.AI_DEFAULT_MODEL", "gpt-4o-mini"),
            patch("core.config.OPENAI_API_KEY", "sk-test"),
            patch(
                "services.llm_service._dispatch_openai",
                return_value=("hello", {}),
            ) as dispatch,
        ):
            text, _ = llm_service.complete(
                MagicMock(), "user-1",
                [{"role": "user", "content": "hi"}],
                temperature=0.5,
            )
        assert text == "hello"
        dispatch.assert_called_once()
        args = dispatch.call_args
        assert args[0][0] == "gpt-4o-mini"  # model
        assert args[0][2] == "sk-test"  # api_key

    def test_anthropic_dispatch(self):
        """Provider 'anthropic' dispatches to _dispatch_anthropic."""
        with (
            self._patch_arango(),
            patch("core.config.AI_DEFAULT_PROVIDER", "openai"),
            patch("core.config.AI_DEFAULT_MODEL", "gpt-4o-mini"),
            patch("core.config.ANTHROPIC_API_KEY", "sk-ant-test"),
            patch(
                "services.llm_service._dispatch_anthropic",
                return_value=("bonjour", {}),
            ) as dispatch,
        ):
            text, _ = llm_service.complete(
                MagicMock(), "user-1",
                [{"role": "user", "content": "hi"}],
                provider="anthropic",
                model="claude-sonnet-4-20250514",
            )
        assert text == "bonjour"
        dispatch.assert_called_once()
        assert dispatch.call_args[0][0] == "claude-sonnet-4-20250514"

    def test_google_dispatch(self):
        """Provider 'google' dispatches to _dispatch_google."""
        with (
            self._patch_arango(),
            patch("core.config.AI_DEFAULT_PROVIDER", "openai"),
            patch("core.config.AI_DEFAULT_MODEL", "gpt-4o-mini"),
            patch("core.config.GOOGLE_API_KEY", "goog-key"),
            patch(
                "services.llm_service._dispatch_google",
                return_value=("hola", {}),
            ) as dispatch,
        ):
            text, _ = llm_service.complete(
                MagicMock(), "user-1",
                [{"role": "user", "content": "hi"}],
                provider="google",
                model="gemini-2.0-flash",
            )
        assert text == "hola"
        dispatch.assert_called_once()
        assert dispatch.call_args[0][0] == "gemini-2.0-flash"

    def test_ollama_dispatch(self):
        """Provider 'ollama' dispatches to _dispatch_ollama (no API key)."""
        with (
            self._patch_arango(),
            patch("core.config.AI_DEFAULT_PROVIDER", "openai"),
            patch("core.config.AI_DEFAULT_MODEL", "gpt-4o-mini"),
            patch(
                "services.llm_service._dispatch_ollama",
                return_value=("local", {}),
            ) as dispatch,
        ):
            text, _ = llm_service.complete(
                MagicMock(), "user-1",
                [{"role": "user", "content": "hi"}],
                provider="ollama",
                model="llama3",
            )
        assert text == "local"
        dispatch.assert_called_once()
        # Ollama dispatch receives no api_key arg
        assert dispatch.call_args[0][0] == "llama3"

    def test_workspace_provider_overrides_platform_default(self):
        """Workspace llm.provider wins over platform default."""
        with (
            self._patch_arango(),
            patch("core.config.AI_DEFAULT_PROVIDER", "openai"),
            patch("core.config.AI_DEFAULT_MODEL", "gpt-4o-mini"),
            patch("core.config.ANTHROPIC_API_KEY", "sk-ant"),
            patch(
                "services.workspace_service.get_workspace_context",
                return_value={"llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}},
            ),
            patch(
                "services.llm_service._dispatch_anthropic",
                return_value=("ws-answer", {}),
            ) as dispatch,
        ):
            text, _ = llm_service.complete(
                MagicMock(), "user-1",
                [{"role": "user", "content": "hi"}],
                workspace_id="ws-1",
            )
        assert text == "ws-answer"
        dispatch.assert_called_once()
        assert dispatch.call_args[0][0] == "claude-sonnet-4-20250514"

    def test_explicit_provider_overrides_workspace(self):
        """Explicit provider/model params override workspace config."""
        with (
            self._patch_arango(),
            patch("core.config.AI_DEFAULT_PROVIDER", "openai"),
            patch("core.config.AI_DEFAULT_MODEL", "gpt-4o-mini"),
            patch("core.config.GOOGLE_API_KEY", "goog-key"),
            patch(
                "services.workspace_service.get_workspace_context",
                return_value={"llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}},
            ),
            patch(
                "services.llm_service._dispatch_google",
                return_value=("explicit-win", {}),
            ) as dispatch,
        ):
            text, _ = llm_service.complete(
                MagicMock(), "user-1",
                [{"role": "user", "content": "hi"}],
                workspace_id="ws-1",
                provider="google",
                model="gemini-2.0-flash",
            )
        assert text == "explicit-win"
        dispatch.assert_called_once()
        assert dispatch.call_args[0][0] == "gemini-2.0-flash"

    def test_platform_default_fallback(self):
        """When no workspace and no explicit params, uses platform defaults."""
        with (
            self._patch_arango(),
            patch("core.config.AI_DEFAULT_PROVIDER", "anthropic"),
            patch("core.config.AI_DEFAULT_MODEL", "claude-sonnet-4-20250514"),
            patch("core.config.ANTHROPIC_API_KEY", "sk-ant"),
            patch(
                "services.llm_service._dispatch_anthropic",
                return_value=("platform-default", {}),
            ) as dispatch,
        ):
            text, _ = llm_service.complete(
                MagicMock(), "user-1",
                [{"role": "user", "content": "hi"}],
            )
        assert text == "platform-default"
        dispatch.assert_called_once()
