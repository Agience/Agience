"""Tests for MCP server auth resolution.

Covers:
  - MCPAuthConfig canonical type names (oauth2, api_key, static, none)
  - Config parser auth type resolution (oauth2, api_key)
  - _resolve_auth_headers() — direct secrets_service path, no Seraph MCP round-trip
  - AuthExpiredError propagation (expired token, missing token)
  - Auth injection in invoke_tool()
  - SecretConfig expires_at field
  - Chat service mcp_server_ids passthrough
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mcp_client.contracts import (
    MCPAuthConfig,
    MCPServerConfig,
    MCPServerTransport,
)


# ---------------------------------------------------------------------------
# MCPAuthConfig / MCPServerConfig contract tests
# ---------------------------------------------------------------------------

class TestMCPAuthConfig:

    def test_oauth2_type(self):
        auth = MCPAuthConfig(type="oauth2", authorizer_id="auth-123")
        assert auth.type == "oauth2"
        assert auth.authorizer_id == "auth-123"
        assert auth.secret_id is None

    def test_api_key_type(self):
        auth = MCPAuthConfig(type="api_key", secret_id="sec-456")
        assert auth.type == "api_key"
        assert auth.secret_id == "sec-456"

    def test_api_key_type_with_custom_header(self):
        auth = MCPAuthConfig(type="api_key", secret_id="sec-456", header="X-Api-Key")
        assert auth.header == "X-Api-Key"

    def test_static_type(self):
        auth = MCPAuthConfig(type="static", header="X-Api-Key", value="key123")
        assert auth.type == "static"
        assert auth.header == "X-Api-Key"
        assert auth.value == "key123"

    def test_none_type(self):
        auth = MCPAuthConfig(type="none")
        assert auth.type == "none"


class TestMCPServerConfigAuth:

    def test_config_with_no_auth(self):
        config = MCPServerConfig(
            id="srv-1", label="Test",
            transport=MCPServerTransport(type="http", well_known="https://example.com/mcp"),
        )
        assert config.auth is None
        assert config.runtime_headers == {}

    def test_config_with_oauth2_auth(self):
        config = MCPServerConfig(
            id="srv-1", label="Test",
            transport=MCPServerTransport(type="http", well_known="https://example.com/mcp"),
            auth=MCPAuthConfig(type="oauth2", authorizer_id="auth-1"),
        )
        assert config.auth is not None
        assert config.auth.type == "oauth2"
        assert config.auth.authorizer_id == "auth-1"

    def test_runtime_headers_default_empty(self):
        config = MCPServerConfig(
            id="srv-1", label="Test",
            transport=MCPServerTransport(type="http", well_known="https://example.com/mcp"),
        )
        assert config.runtime_headers == {}

    def test_runtime_headers_can_be_set(self):
        config = MCPServerConfig(
            id="srv-1", label="Test",
            transport=MCPServerTransport(type="http", well_known="https://example.com/mcp"),
        )
        config.runtime_headers = {"Authorization": "Bearer tok"}
        assert config.runtime_headers["Authorization"] == "Bearer tok"


# ---------------------------------------------------------------------------
# Config parser auth block tests
# ---------------------------------------------------------------------------

class TestConfigParserAuthBlock:

    def _make_artifact(self, auth_block: dict | None = None, artifact_id: str = "srv-1"):
        artifact = MagicMock()
        artifact.id = artifact_id
        ctx = {"transport": {"type": "http", "well_known": "https://example.com/mcp"}}
        if auth_block:
            ctx["auth"] = auth_block
        artifact.context = json.dumps(ctx)
        return artifact

    @patch("mcp_client.config_parser.validate_url", return_value="https://example.com/mcp")
    def test_parse_canonical_oauth2(self, _):
        from mcp_client.config_parser import parse_mcp_server_artifact

        config = parse_mcp_server_artifact(self._make_artifact({
            "type": "oauth2",
            "authorizer_id": "auth-uuid-456",
        }))

        assert config.auth.type == "oauth2"
        assert config.auth.authorizer_id == "auth-uuid-456"

    @patch("mcp_client.config_parser.validate_url", return_value="https://example.com/mcp")
    def test_parse_canonical_api_key(self, _):
        from mcp_client.config_parser import parse_mcp_server_artifact

        config = parse_mcp_server_artifact(self._make_artifact({
            "type": "api_key",
            "secret_id": "sec-789",
            "header": "X-Api-Key",
        }))

        assert config.auth.type == "api_key"
        assert config.auth.secret_id == "sec-789"
        assert config.auth.header == "X-Api-Key"

    @patch("mcp_client.config_parser.validate_url", return_value="https://example.com/mcp")
    def test_parse_static_auth(self, _):
        from mcp_client.config_parser import parse_mcp_server_artifact

        config = parse_mcp_server_artifact(self._make_artifact({
            "type": "static",
            "header": "X-Api-Key",
            "value": "mykey",
        }))

        assert config.auth.type == "static"
        assert config.auth.header == "X-Api-Key"
        assert config.auth.value == "mykey"

    @patch("mcp_client.config_parser.validate_url", return_value="https://example.com/mcp")
    def test_parse_without_auth_block(self, _):
        from mcp_client.config_parser import parse_mcp_server_artifact

        config = parse_mcp_server_artifact(self._make_artifact())
        assert config is not None
        assert config.auth is None

    @patch("mcp_client.config_parser.validate_url", return_value="https://example.com/mcp")
    def test_parse_with_invalid_auth_type_ignored(self, _):
        from mcp_client.config_parser import parse_mcp_server_artifact

        config = parse_mcp_server_artifact(self._make_artifact({"type": "invalid_type"}))
        assert config is not None
        assert config.auth is None


# ---------------------------------------------------------------------------
# _resolve_auth_headers tests
# ---------------------------------------------------------------------------

class TestResolveAuthHeaders:

    def _make_config(self, auth_type: str, **auth_kwargs) -> MCPServerConfig:
        return MCPServerConfig(
            id="test-server", label="Test",
            transport=MCPServerTransport(type="http", well_known="https://example.com/mcp"),
            auth=MCPAuthConfig(type=auth_type, **auth_kwargs),
        )

    def test_no_auth_returns_empty(self):
        from services.mcp_service import _resolve_auth_headers

        config = MCPServerConfig(
            id="srv", label="Test",
            transport=MCPServerTransport(type="http", well_known="https://example.com/mcp"),
        )
        result = _resolve_auth_headers(MagicMock(), "user-1", config)
        assert result == {}

    def test_static_auth_returns_header(self):
        from services.mcp_service import _resolve_auth_headers

        config = self._make_config("static", header="X-Api-Key", value="mykey")
        result = _resolve_auth_headers(MagicMock(), "user-1", config)
        assert result == {"X-Api-Key": "mykey"}

    # --- api_key ---

    @patch("services.secrets_service.list_secrets")
    @patch("services.secrets_service.decrypt_value")
    def test_api_key_auth_returns_bearer(self, mock_decrypt, mock_list):
        from services.mcp_service import _resolve_auth_headers

        secret = MagicMock()
        secret.encrypted_value = "enc_token"
        mock_list.return_value = [secret]
        mock_decrypt.return_value = "decrypted_token"

        config = self._make_config("api_key", secret_id="sec-123")
        result = _resolve_auth_headers(MagicMock(), "user-1", config)
        assert result == {"Authorization": "Bearer decrypted_token"}

    @patch("services.secrets_service.list_secrets")
    @patch("services.secrets_service.decrypt_value")
    def test_api_key_auth_custom_header(self, mock_decrypt, mock_list):
        from services.mcp_service import _resolve_auth_headers

        secret = MagicMock()
        secret.encrypted_value = "enc_token"
        mock_list.return_value = [secret]
        mock_decrypt.return_value = "raw_key"

        config = self._make_config("api_key", secret_id="sec-123", header="X-Api-Key")
        result = _resolve_auth_headers(MagicMock(), "user-1", config)
        assert result == {"X-Api-Key": "raw_key"}

    @patch("services.secrets_service.list_secrets")
    def test_api_key_auth_not_found_returns_empty(self, mock_list):
        from services.mcp_service import _resolve_auth_headers

        mock_list.return_value = []

        config = self._make_config("api_key", secret_id="sec-missing")
        result = _resolve_auth_headers(MagicMock(), "user-1", config)
        assert result == {}

    # --- oauth2 ---

    @patch("services.secrets_service.list_secrets")
    @patch("services.secrets_service.decrypt_value")
    def test_oauth2_valid_bearer_token(self, mock_decrypt, mock_list):
        from services.mcp_service import _resolve_auth_headers

        secret = MagicMock()
        secret.encrypted_value = "enc_tok"
        secret.expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        mock_list.side_effect = lambda db, uid, **kw: (
            [secret] if kw.get("secret_type") == "bearer_token" else []
        )
        mock_decrypt.return_value = "access_tok"

        config = self._make_config("oauth2", authorizer_id="auth-1")
        result = _resolve_auth_headers(MagicMock(), "user-1", config)
        assert result == {"Authorization": "Bearer access_tok"}

    @patch("services.secrets_service.list_secrets")
    def test_oauth2_expired_bearer_raises(self, mock_list):
        from services.mcp_service import _resolve_auth_headers, AuthExpiredError

        secret = MagicMock()
        secret.encrypted_value = "enc_tok"
        secret.expires_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_list.side_effect = lambda db, uid, **kw: (
            [secret] if kw.get("secret_type") == "bearer_token" else []
        )

        config = self._make_config("oauth2", authorizer_id="auth-1")
        with pytest.raises(AuthExpiredError, match="expired"):
            _resolve_auth_headers(MagicMock(), "user-1", config)

    @patch("services.secrets_service.list_secrets")
    def test_oauth2_bearer_token_no_expiry_passes(self, mock_list):
        """Token with no expires_at is treated as non-expiring (e.g. API key stored via oauth2)."""
        from services.mcp_service import _resolve_auth_headers

        secret = MagicMock()
        secret.encrypted_value = "enc_tok"
        secret.expires_at = ""

        with patch("services.secrets_service.decrypt_value", return_value="tok"):
            mock_list.side_effect = lambda db, uid, **kw: (
                [secret] if kw.get("secret_type") == "bearer_token" else []
            )
            config = self._make_config("oauth2", authorizer_id="auth-1")
            result = _resolve_auth_headers(MagicMock(), "user-1", config)
            assert result == {"Authorization": "Bearer tok"}

    @patch("services.secrets_service.list_secrets")
    def test_oauth2_refresh_token_only_raises(self, mock_list):
        """Has a refresh token but no bearer token — signals re-auth needed."""
        from services.mcp_service import _resolve_auth_headers, AuthExpiredError

        refresh_secret = MagicMock()

        def _list(db, uid, **kw):
            if kw.get("secret_type") == "bearer_token":
                return []
            if kw.get("secret_type") == "oauth_refresh_token":
                return [refresh_secret]
            return []

        mock_list.side_effect = _list

        config = self._make_config("oauth2", authorizer_id="auth-1")
        with pytest.raises(AuthExpiredError):
            _resolve_auth_headers(MagicMock(), "user-1", config)

    @patch("services.secrets_service.list_secrets")
    def test_oauth2_no_token_at_all_raises(self, mock_list):
        """No bearer token and no refresh token — account not connected."""
        from services.mcp_service import _resolve_auth_headers, AuthExpiredError

        mock_list.return_value = []

        config = self._make_config("oauth2", authorizer_id="auth-1")
        with pytest.raises(AuthExpiredError, match="Connect the account"):
            _resolve_auth_headers(MagicMock(), "user-1", config)


# ---------------------------------------------------------------------------
# AuthExpiredError tests
#
# Phase 7D — Server Artifact Proxy: the dedicated `/mcp/servers/{id}/tools/call`
# router endpoint that translated AuthExpiredError into a 401 response has been
# deleted. Tool invocation now flows through `POST /artifacts/{server_id}/invoke`
# via the operation dispatcher, which surfaces the error through the dispatcher
# emit envelope (artifact.invoke.failed event with error.type=AuthExpiredError)
# and re-raises. The router-level 401 mapping is no longer relevant — the
# unit tests above (TestAuthHeaderResolution) cover the AuthExpiredError raise
# path directly.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# invoke_tool auth injection tests
# ---------------------------------------------------------------------------

class TestInvokeToolAuthInjection:

    @patch("mcp_client.adapter.create_client")
    @patch("services.mcp_service._resolve_auth_headers")
    @patch("services.mcp_service._get_server_config_from_collections")
    @patch("services.mcp_service._agience_core_id", return_value="core-uuid")
    def test_invoke_tool_injects_auth_headers(
        self, _mock_core_id, mock_col_config, mock_resolve, mock_create_client
    ):
        from services import mcp_service

        config = MCPServerConfig(
            id="ext-srv", label="Ext",
            transport=MCPServerTransport(type="http", well_known="https://ext.com/mcp"),
            auth=MCPAuthConfig(type="oauth2", authorizer_id="auth-1"),
        )
        mock_col_config.return_value = config
        mock_resolve.return_value = {"Authorization": "Bearer resolved-tok"}

        client = MagicMock()
        client.call_tool.return_value = {"ok": True}
        mock_create_client.return_value = client

        mcp_service.invoke_tool(
            db=MagicMock(), user_id="user-1",
            server_artifact_id="ext-srv", tool_name="do_thing",
            arguments={"x": 1}, workspace_id=None,
        )

        created_config = mock_create_client.call_args[0][0]
        assert created_config.runtime_headers == {"Authorization": "Bearer resolved-tok"}


# ---------------------------------------------------------------------------
# SecretConfig expires_at tests
# ---------------------------------------------------------------------------

class TestSecretConfigExpiresAt:

    def test_expires_at_in_to_dict_when_set(self):
        from services.secrets_service import SecretConfig

        sec = SecretConfig(
            id="s1", type="bearer_token", provider="ext",
            label="test", encrypted_value="enc",
            created_time="2026-03-31T00:00:00Z",
            authorizer_id="auth-1",
            expires_at="2026-03-31T01:00:00Z",
        )
        d = sec.to_dict()
        assert d["expires_at"] == "2026-03-31T01:00:00Z"

    def test_expires_at_omitted_from_to_dict_when_empty(self):
        from services.secrets_service import SecretConfig

        sec = SecretConfig(
            id="s1", type="llm_key", provider="openai",
            label="test", encrypted_value="enc",
            created_time="2026-03-31T00:00:00Z",
        )
        d = sec.to_dict()
        assert "expires_at" not in d

    def test_from_dict_with_expires_at(self):
        from services.secrets_service import SecretConfig

        data = {
            "id": "s1", "type": "bearer_token", "provider": "ext",
            "label": "tok", "encrypted_value": "enc",
            "created_time": "2026-03-31T00:00:00Z",
            "expires_at": "2026-03-31T01:00:00Z",
        }
        sec = SecretConfig.from_dict(data)
        assert sec.expires_at == "2026-03-31T01:00:00Z"

    def test_from_dict_without_expires_at(self):
        from services.secrets_service import SecretConfig

        data = {
            "id": "s1", "type": "llm_key", "provider": "openai",
            "label": "key", "encrypted_value": "enc",
            "created_time": "2026-03-31T00:00:00Z",
        }
        sec = SecretConfig.from_dict(data)
        assert sec.expires_at == ""

    @patch("services.secrets_service.arango_ws.update_person_preferences")
    @patch("services.secrets_service._load_prefs")
    def test_add_secret_passes_expires_at(self, mock_load, _mock_update):
        from services.secrets_service import add_secret

        mock_load.return_value = {"secrets": []}

        result = add_secret(
            db=MagicMock(), user_id="u1",
            secret_type="bearer_token", provider="ext",
            label="tok", value="plaintext",
            authorizer_id="auth-1",
            expires_at="2026-03-31T01:00:00Z",
        )

        assert any(s.expires_at == "2026-03-31T01:00:00Z" for s in result)

# NOTE: The TestChatServiceMCPServerIds suite was removed when routers/llm_router.py
# was deleted (H7 resolution, audit 2026-04-06). The agentic chat turn is now
# invoked as Aria's `run_chat_turn` tool via the generic `POST /artifacts/{aria_id}/invoke`
# dispatch path; its auth resolution is covered by the mcp_tool dispatcher tests and
# by Aria's own test suite.
