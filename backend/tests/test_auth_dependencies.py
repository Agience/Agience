"""Tests for the unified auth resolution layer (resolve_auth / get_auth)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from entities.api_key import APIKey as APIKeyEntity
from entities.grant import Grant as GrantEntity
from services.dependencies import resolve_auth, AuthContext
from core.config import AUTHORITY_ISSUER


def test_resolve_auth_rejects_deprecated_api_key_jwt():
    """Deprecated API-key JWTs (with api_key_id claim) are rejected."""
    payload = {
        "sub": "user-123",
        "aud": AUTHORITY_ISSUER,
        "api_key_id": "key-123",
        "scopes": ["resource:*:search"],
    }

    with patch("services.dependencies.verify_token", return_value=payload):
        with pytest.raises(HTTPException) as exc_info:
            resolve_auth(token="jwt-token", arango_db=MagicMock())

    assert exc_info.value.status_code == 403
    assert "API-key JWT not accepted" in exc_info.value.detail


def test_resolve_auth_accepts_user_jwt():
    """Standard user JWTs return an AuthContext with principal_type=user."""
    payload = {
        "sub": "user-123",
        "aud": AUTHORITY_ISSUER,
    }

    with patch("services.dependencies.verify_token", return_value=payload):
        ctx = resolve_auth(token="jwt-token", arango_db=MagicMock())

    assert isinstance(ctx, AuthContext)
    assert ctx.principal_type == "user"
    assert ctx.user_id == "user-123"
    assert ctx.principal_id == "user-123"
    assert ctx.api_key_entity is None
    assert ctx.bearer_grant is None


def test_resolve_auth_accepts_direct_api_key():
    """Direct API keys (agc_xxx) are resolved with grants loaded."""
    api_key = APIKeyEntity(
        id="key-123",
        user_id="user-123",
        name="service-agent",
        scopes=["resource:*:search"],
        resource_filters={"workspaces": ["ws-1"]},
    )

    mock_grants = [
        GrantEntity(
            resource_id="col-1",
            grantee_type="api_key",
            grantee_id="key-123",
            granted_by="user-123",
            can_read=True,
        )
    ]

    with patch("services.dependencies.verify_api_key", return_value=api_key), \
         patch("services.dependencies.db_get_active_grants_for_grantee", return_value=mock_grants):
        ctx = resolve_auth(token="agc_test_key", arango_db=MagicMock())

    assert ctx.principal_type == "api_key"
    assert ctx.user_id == "user-123"
    assert ctx.api_key_id == "key-123"
    assert ctx.api_key_entity is not None
    assert ctx.api_key_entity.name == "service-agent"
    assert len(ctx.grants) == 1
    assert ctx.grants[0].resource_id == "col-1"


def test_resolve_auth_parses_artifact_prefix():
    """API keys with artifact prefix ({artifact_id}:agc_xxx) populate target_artifact_id."""
    api_key = APIKeyEntity(id="key-1", user_id="user-1")

    with patch("services.dependencies.verify_api_key", return_value=api_key), \
         patch("services.dependencies.db_get_active_grants_for_grantee", return_value=[]):
        ctx = resolve_auth(token="art_123:agc_test_key", arango_db=MagicMock())

    assert ctx.target_artifact_id == "art_123"
    assert ctx.principal_type == "api_key"


def test_resolve_auth_accepts_grant_key_in_bearer():
    """Grant keys in the Bearer slot return principal_type=grant_key."""
    grant = GrantEntity(
        id="grant-1",
        resource_id="col-1",
        grantee_type="grant_key",
        grantee_id="hash-1",
        granted_by="user-1",
        can_read=True,
    )

    with patch("services.dependencies.verify_token", return_value=None), \
         patch("services.dependencies.verify_api_key", return_value=None), \
         patch("services.dependencies.db_get_active_grants_by_key", return_value=[grant]):
        ctx = resolve_auth(token="grant-key-value", arango_db=MagicMock())

    assert ctx.principal_type == "grant_key"
    assert ctx.user_id is None
    assert ctx.bearer_grant is grant
    assert len(ctx.grants) == 1


def test_resolve_auth_server_jwt():
    """Server JWTs return principal_type=server with no user_id."""
    payload = {
        "sub": "server/my-server",
        "aud": "agience",
        "principal_type": "server",
        "client_id": "my-server",
        "server_id": "srv-1",
    }

    with patch("services.dependencies.verify_token", return_value=payload):
        ctx = resolve_auth(token="server-jwt", arango_db=MagicMock())

    assert ctx.principal_type == "server"
    assert ctx.user_id is None
    assert ctx.server_id == "srv-1"
    assert ctx.principal_id == "my-server"


def test_resolve_auth_invalid_token_raises_401():
    """Completely invalid tokens raise 401."""
    with patch("services.dependencies.verify_token", return_value=None), \
         patch("services.dependencies.verify_api_key", return_value=None), \
         patch("services.dependencies.db_get_active_grants_by_key", return_value=[]):
        with pytest.raises(HTTPException) as exc_info:
            resolve_auth(token="garbage", arango_db=MagicMock())

    assert exc_info.value.status_code == 401


def test_resolve_auth_missing_token_raises_401():
    """Empty/missing token raises 401."""
    with pytest.raises(HTTPException) as exc_info:
        resolve_auth(token="", arango_db=MagicMock())

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Delegation tokens — all four identity-chain entities required
# ---------------------------------------------------------------------------

def _delegation_payload(**overrides):
    """Base valid delegation JWT claims."""
    base = {
        "sub": "user-42",
        "aud": "agience-server-astra",
        "iss": AUTHORITY_ISSUER,
        "act": {"sub": "agience-server-astra"},
        "principal_type": "delegation",
        "host_id": "host-abc",
    }
    base.update(overrides)
    return base


def test_resolve_auth_delegation_all_entities():
    """Delegation tokens with all four entities produce a valid AuthContext."""
    with patch("services.dependencies.verify_token", return_value=_delegation_payload()):
        ctx = resolve_auth(token="delegation-jwt", arango_db=MagicMock())

    assert ctx.principal_type == "user"
    assert ctx.user_id == "user-42"
    assert ctx.actor == "agience-server-astra"
    assert ctx.authority == AUTHORITY_ISSUER
    assert ctx.host_id == "host-abc"


def test_resolve_auth_delegation_missing_sub():
    """Delegation tokens without sub (user) are rejected."""
    payload = _delegation_payload(sub="")
    with patch("services.dependencies.verify_token", return_value=payload):
        with pytest.raises(HTTPException) as exc_info:
            resolve_auth(token="delegation-jwt", arango_db=MagicMock())
    assert exc_info.value.status_code == 401
    assert "sub" in exc_info.value.detail


def test_resolve_auth_delegation_missing_act_sub():
    """Delegation tokens without act.sub (server) are rejected."""
    payload = _delegation_payload(act={})
    with patch("services.dependencies.verify_token", return_value=payload):
        with pytest.raises(HTTPException) as exc_info:
            resolve_auth(token="delegation-jwt", arango_db=MagicMock())
    assert exc_info.value.status_code == 401
    assert "act.sub" in exc_info.value.detail


def test_resolve_auth_delegation_missing_act_entirely():
    """Delegation tokens without act claim at all are rejected."""
    payload = _delegation_payload()
    del payload["act"]
    with patch("services.dependencies.verify_token", return_value=payload):
        with pytest.raises(HTTPException) as exc_info:
            resolve_auth(token="delegation-jwt", arango_db=MagicMock())
    assert exc_info.value.status_code == 401
    assert "act.sub" in exc_info.value.detail


def test_resolve_auth_delegation_missing_host_id():
    """Delegation tokens without host_id are rejected."""
    payload = _delegation_payload(host_id="")
    with patch("services.dependencies.verify_token", return_value=payload):
        with pytest.raises(HTTPException) as exc_info:
            resolve_auth(token="delegation-jwt", arango_db=MagicMock())
    assert exc_info.value.status_code == 401
    assert "host_id" in exc_info.value.detail


def test_resolve_auth_delegation_missing_aud():
    """Delegation tokens without aud are rejected (via _validate_aud_for_principal)."""
    payload = _delegation_payload(aud="")
    with patch("services.dependencies.verify_token", return_value=payload):
        with pytest.raises(HTTPException) as exc_info:
            resolve_auth(token="delegation-jwt", arango_db=MagicMock())
    assert exc_info.value.status_code == 401
    assert "aud" in exc_info.value.detail.lower()
