"""Tests for `api_keys_router.py` covering scoped API key CRUD.

These are router-level tests: they validate status codes, response shapes,
owner checks, and that secrets are only returned on creation.

Auth/db dependencies are overridden in `backend/tests/conftest.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from core import config
from main import app
from services.dependencies import get_auth


NOW_ISO = datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()


@dataclass
class APIKeyStub:
    id: str
    user_id: str
    key_hash: str = "hash"
    name: str = "My Key"
    scopes: List[str] = None  # type: ignore[assignment]
    resource_filters: Dict[str, Any] = None  # type: ignore[assignment]
    created_time: str = NOW_ISO
    modified_time: str = NOW_ISO
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    is_active: bool = True

    def __post_init__(self):
        if self.scopes is None:
            self.scopes = ["resource:*:search"]
        if self.resource_filters is None:
            self.resource_filters = {}


class TestAPIKeys:
    def test_openapi_exchange_endpoint_is_removed(self):
        assert "/api-keys/exchange" not in app.openapi().get("paths", {})

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.auth_svc.hash_api_key")
    @patch("routers.api_keys_router.auth_svc.generate_api_key")
    @patch("routers.api_keys_router.arango_db_module.create_api_key")
    async def test_create_api_key_returns_raw_key_once(
        self,
        mock_create,
        mock_generate,
        mock_hash,
        client,
    ):
        mock_generate.return_value = "raw_key_123"
        mock_hash.return_value = "hash_123"

        def _create(_db, entity):
            return entity

        mock_create.side_effect = _create

        payload = {
            "name": "Integration Key",
            "scopes": [
                "resource:*:search",
                "resource:application/vnd.agience.collection+json:read",
            ],
            "resource_filters": {"collections": ["c1"]},
            "expires_at": None,
        }

        resp = await client.post("/api-keys", json=payload)
        assert resp.status_code == 201

        body = resp.json()
        assert body["user_id"] == "user-123"
        assert body["name"] == "Integration Key"
        assert body["scopes"] == [
            "resource:*:search",
            "resource:application/vnd.agience.collection+json:read",
        ]
        assert body["resource_filters"] == {"collections": ["c1"]}
        assert body["is_active"] is True
        assert body["key"] == "raw_key_123"  # only returned on creation

        mock_generate.assert_called_once()
        mock_hash.assert_called_once_with("raw_key_123")
        mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_api_key_rejects_api_key_jwt(self, client):
        previous = app.dependency_overrides.pop(get_auth, None)

        try:
            with patch(
                "services.dependencies.verify_token",
                return_value={
                    "sub": "user-123",
                    "aud": config.AUTHORITY_ISSUER,
                    "api_key_id": "key-123",
                    "scopes": ["resource:*:search"],
                },
            ):
                resp = await client.post(
                    "/api-keys",
                    headers={"Authorization": "Bearer exchanged-jwt"},
                    json={"name": "Integration Key", "scopes": ["resource:*:search"]},
                )
        finally:
            if previous is not None:
                app.dependency_overrides[get_auth] = previous

        assert resp.status_code == 403
        assert resp.json()["detail"] == "API-key JWT not accepted; use direct API key"

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.auth_svc.hash_api_key")
    @patch("routers.api_keys_router.auth_svc.generate_api_key")
    @patch("routers.api_keys_router.arango_db_module.create_api_key")
    async def test_create_api_key_minimal_payload_applies_defaults(
        self,
        mock_create,
        mock_generate,
        mock_hash,
        client,
    ):
        mock_generate.return_value = "raw_key_minimal"
        mock_hash.return_value = "hash_minimal"
        mock_create.side_effect = lambda _db, entity: entity

        resp = await client.post(
            "/api-keys",
            json={"name": "My Key"},
        )
        assert resp.status_code == 201

        body = resp.json()
        assert body["name"] == "My Key"
        assert body["client_id"] == "agience-frontend"
        assert body["display_label"] == "Easy MCP Key"
        assert body["scopes"] == [
            "resource:*:read",
            "resource:*:search",
            "resource:*:list",
            "resource:*:invoke",
        ]
        assert body["resource_filters"] == {
            "workspaces": "*",
            "collections": "*",
        }
        assert body["key"] == "raw_key_minimal"

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.auth_svc.hash_api_key")
    @patch("routers.api_keys_router.auth_svc.generate_api_key")
    @patch("routers.api_keys_router.arango_db_module.create_api_key")
    async def test_create_api_key_accepts_explicit_licensing_scopes(
        self,
        mock_create,
        mock_generate,
        mock_hash,
        client,
    ):
        mock_generate.return_value = "raw_key_456"
        mock_hash.return_value = "hash_456"
        mock_create.side_effect = lambda _db, entity: entity

        payload = {
            "name": "Licensing Operator",
            "scopes": [
                "licensing:entitlement:host_standard",
                "licensing:entitlement:licensing_operations",
            ],
            "resource_filters": {"workspaces": ["ws-licensing"]},
        }

        resp = await client.post("/api-keys", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["scopes"] == payload["scopes"]
        assert body["resource_filters"] == {"workspaces": ["ws-licensing"]}

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.get_api_keys_by_user")
    async def test_list_api_keys_metadata_only(self, mock_list, client):
        mock_list.return_value = [
            APIKeyStub(id="k1", user_id="user-123", name="Key 1"),
            APIKeyStub(id="k2", user_id="user-123", name="Key 2", is_active=False),
        ]

        resp = await client.get("/api-keys")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["id"] == "k1"
        assert body[0]["name"] == "Key 1"
        assert "key" not in body[0]

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_get_api_key_by_id_owner_ok(self, mock_get, client):
        mock_get.return_value = APIKeyStub(id="k1", user_id="user-123", name="Key 1")

        resp = await client.get("/api-keys/k1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "k1"
        assert body["user_id"] == "user-123"
        assert "key" not in body

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_get_api_key_by_id_owner_mismatch_404(self, mock_get, client):
        mock_get.return_value = APIKeyStub(id="k1", user_id="other-user")

        resp = await client.get("/api-keys/k1")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.update_api_key")
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_update_api_key_success(self, mock_get, mock_update, client):
        key = APIKeyStub(id="k1", user_id="user-123", name="Old", scopes=["resource:*:search"])
        mock_get.return_value = key

        def _update(_db, entity):
            return entity

        mock_update.side_effect = _update

        payload = {
            "name": "New Name",
            "scopes": ["resource:*:search", "resource:*:write"],
            "resource_filters": {"workspaces": ["w1"]},
            "is_active": True,
        }

        resp = await client.patch("/api-keys/k1", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "k1"
        assert body["name"] == "New Name"
        assert body["scopes"] == ["resource:*:search", "resource:*:write"]
        assert body["resource_filters"] == {"workspaces": ["w1"]}

        mock_update.assert_called_once()

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.update_api_key")
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_update_api_key_accepts_explicit_licensing_scopes(self, mock_get, mock_update, client):
        key = APIKeyStub(id="k1", user_id="user-123", name="Old", scopes=["resource:*:search"])
        mock_get.return_value = key
        mock_update.side_effect = lambda _db, entity: entity

        payload = {
            "scopes": [
                "licensing:entitlement:host_standard",
                "licensing:entitlement:delegated_licensing_operations",
            ],
            "resource_filters": {"workspaces": ["ws-123"]},
        }

        resp = await client.patch("/api-keys/k1", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["scopes"] == payload["scopes"]
        assert body["resource_filters"] == payload["resource_filters"]

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_update_api_key_owner_mismatch_404(self, mock_get, client):
        mock_get.return_value = APIKeyStub(id="k1", user_id="other-user")

        resp = await client.patch("/api-keys/k1", json={"name": "Nope"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.delete_api_key")
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_delete_api_key_success(self, mock_get, mock_delete, client):
        mock_get.return_value = APIKeyStub(id="k1", user_id="user-123")
        mock_delete.return_value = True

        resp = await client.delete("/api-keys/k1")
        assert resp.status_code == 204

        mock_delete.assert_called_once_with(mock_get.call_args[0][0], "k1")

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.delete_api_key")
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_delete_api_key_failure_500(self, mock_get, mock_delete, client):
        mock_get.return_value = APIKeyStub(id="k1", user_id="user-123")
        mock_delete.return_value = False

        resp = await client.delete("/api-keys/k1")
        assert resp.status_code == 500

    @pytest.mark.asyncio
    @patch("routers.api_keys_router.arango_db_module.get_api_key_by_id")
    async def test_delete_api_key_owner_mismatch_404(self, mock_get, client):
        mock_get.return_value = APIKeyStub(id="k1", user_id="other-user")

        resp = await client.delete("/api-keys/k1")
        assert resp.status_code == 404
