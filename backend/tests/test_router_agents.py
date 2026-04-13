# tests/test_router_agents.py
"""Tests for POST /agents/invoke — named operator dispatch."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.agents_router import router
from services.dependencies import AuthContext, get_auth
from core.dependencies import get_arango_db


# ── Fixtures ──────────────────────────────────────────────────────────────────

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
    """Client with a valid user context."""
    ctx = AuthContext(user_id="u-1", principal_type="user", grants=[])

    app.dependency_overrides[get_auth] = lambda: ctx
    app.dependency_overrides[get_arango_db] = lambda: mock_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client(app, mock_db):
    """Client without a user_id."""
    ctx = AuthContext(user_id=None, principal_type="grant_key", grants=[])

    app.dependency_overrides[get_auth] = lambda: ctx
    app.dependency_overrides[get_arango_db] = lambda: mock_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_anon_returns_401(anon_client):
    resp = anon_client.post("/agents/invoke", json={"operator": "demo_data"})
    assert resp.status_code == 401


def test_unknown_operator_no_input_returns_400(authed_client):
    resp = authed_client.post("/agents/invoke", json={"operator": "nonexistent"})
    assert resp.status_code == 400
    assert "nonexistent" in resp.json()["detail"]


@patch("core.event_dispatcher.resolve_operator_server", return_value=("aria", "extract_units"))
@patch("services.server_registry.resolve_name_to_id", return_value="aria-artifact-uuid")
@patch("services.mcp_service.invoke_tool", return_value={"ok": True})
def test_mcp_dispatch(mock_invoke, mock_resolve_id, mock_resolve, authed_client, mock_db):
    resp = authed_client.post("/agents/invoke", json={
        "operator": "extract_units",
        "workspace_id": "ws-1",
        "params": {"source_artifact_id": "a-1"},
    })

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_invoke.assert_called_once()
    call_kwargs = mock_invoke.call_args[1]
    assert call_kwargs["server_artifact_id"] == "aria-artifact-uuid"
    assert call_kwargs["tool_name"] == "extract_units"
    assert call_kwargs["arguments"]["source_artifact_id"] == "a-1"
    assert call_kwargs["arguments"]["workspace_id"] == "ws-1"


@patch("core.event_dispatcher.resolve_operator_server", return_value=None)
@patch("agents.get_agent_callable")
@patch("services.agent_service.invoke")
def test_agent_plugin_dispatch(mock_invoke, mock_get, mock_resolve, authed_client, mock_db):
    """Named operator resolved via agents/*.py plugin registry."""

    class _Result:
        def __init__(self):
            self.status = "ok"
            self.message = "done"

    agent_fn = MagicMock()
    mock_get.return_value = agent_fn
    mock_invoke.return_value = _Result()

    resp = authed_client.post("/agents/invoke", json={
        "operator": "complete_authorizer_oauth",
        "params": {"workspace_id": "ws-1", "code": "abc"},
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


@patch("core.event_dispatcher.resolve_operator_server", return_value=None)
@patch("services.agent_service.invoke")
def test_llm_fallback(mock_invoke, mock_resolve, authed_client, mock_db):
    """When no operator resolves, LLM fallback handles text input."""
    from agents import AgentNotFoundError

    mock_invoke.return_value = MagicMock(output="Hello from LLM")

    with patch("agents.get_agent_callable", side_effect=AgentNotFoundError("nope")):
        resp = authed_client.post("/agents/invoke", json={
            "input": "What is 2+2?",
        })

    assert resp.status_code == 200
    assert resp.json()["output"] == "Hello from LLM"


@patch("core.event_dispatcher.resolve_operator_server", return_value=("aria", "extract_units"))
@patch("services.server_registry.resolve_name_to_id", return_value="aria-artifact-uuid")
@patch("services.mcp_service.invoke_tool", return_value={"ok": True})
def test_operator_params_merged(mock_invoke, mock_resolve_id, mock_resolve, authed_client, mock_db):
    """operator_params are merged into the params dict."""
    resp = authed_client.post("/agents/invoke", json={
        "operator": "extract_units",
        "params": {"a": 1},
        "operator_params": {"b": 2},
    })

    assert resp.status_code == 200
    call_kwargs = mock_invoke.call_args[1]
    assert call_kwargs["arguments"]["a"] == 1
    assert call_kwargs["arguments"]["b"] == 2
