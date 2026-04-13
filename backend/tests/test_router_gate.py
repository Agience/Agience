"""Tests for routers/gate_router.py — internal kernel-server-only gate.

Covers:
  - Auth guard rejects missing / non-server / non-kernel tokens
  - POST /internal/gate/set-limits delegates to gate_service
  - GET /internal/gate/usage/{person_id} returns limits + usage shape
  - Issued kernel server JWT (RS256) is accepted end-to-end
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from services.server_registry import all_client_ids as _all_client_ids
from services.auth_service import create_jwt_token

_KERNEL_SERVER_IDS = _all_client_ids()


def _kernel_server_token(client_id: str | None = None) -> str:
    """Mint a kernel-server JWT shaped like handle_client_credentials_grant emits."""
    cid = client_id or sorted(_KERNEL_SERVER_IDS)[0]
    return create_jwt_token(
        {
            "sub": f"server/{cid}",
            "aud": "agience",
            "principal_type": "server",
            "client_id": cid,
            "scopes": ["tool:*:invoke"],
        },
        expires_hours=1,
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestAuthGuard:
    @pytest.mark.asyncio
    async def test_missing_bearer_returns_401(self, client: AsyncClient):
        r = await client.get("/internal/gate/usage/user-1")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_garbage_token_returns_401(self, client: AsyncClient):
        r = await client.get(
            "/internal/gate/usage/user-1", headers=_bearer("not.a.token")
        )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_user_token_rejected_with_403(self, client: AsyncClient):
        # Plain user JWT — wrong principal_type.
        user_token = create_jwt_token(
            {"sub": "user-1", "aud": "agience", "principal_type": "user"},
            expires_hours=1,
        )
        r = await client.get(
            "/internal/gate/usage/user-1", headers=_bearer(user_token)
        )
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_non_kernel_server_token_rejected(self, client: AsyncClient):
        # Server token but client_id NOT in server_registry — third-party.
        third_party = create_jwt_token(
            {
                "sub": "server/third-party",
                "aud": "agience",
                "principal_type": "server",
                "client_id": "third-party",
            },
            expires_hours=1,
        )
        r = await client.get(
            "/internal/gate/usage/user-1", headers=_bearer(third_party)
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# /internal/gate/usage
# ---------------------------------------------------------------------------

class TestGetUsage:
    @pytest.mark.asyncio
    async def test_returns_limits_and_usage_shape(self, client: AsyncClient):
        with (
            patch(
                "services.gate_service.get_or_default_limits",
                return_value={"max_workspaces": 10, "max_artifacts": 100, "vu_limit": 1000},
            ),
            patch("services.gate_service.count_workspaces", return_value=3),
            patch("services.gate_service.count_artifacts", return_value=42),
            patch("services.gate_service.get_tally", return_value=125),
        ):
            r = await client.get(
                "/internal/gate/usage/user-1", headers=_bearer(_kernel_server_token())
            )
        assert r.status_code == 200
        body = r.json()
        assert body["person_id"] == "user-1"
        assert body["limits"]["vu_limit"] == 1000
        assert body["usage"] == {"workspaces": 3, "artifacts": 42, "vu": 125}


# ---------------------------------------------------------------------------
# /internal/gate/set-limits
# ---------------------------------------------------------------------------

class TestSetLimits:
    @pytest.mark.asyncio
    async def test_delegates_to_gate_service(self, client: AsyncClient):
        with patch("services.gate_service.set_limits") as set_limits:
            r = await client.post(
                "/internal/gate/set-limits",
                json={
                    "person_id": "user-1",
                    "max_workspaces": 5,
                    "max_artifacts": 50,
                    "vu_limit": 500,
                },
                headers=_bearer(_kernel_server_token()),
            )
        assert r.status_code == 204
        set_limits.assert_called_once()
        call_kwargs = set_limits.call_args.kwargs
        assert call_kwargs["max_workspaces"] == 5
        assert call_kwargs["vu_limit"] == 500

    @pytest.mark.asyncio
    async def test_partial_limits_supported(self, client: AsyncClient):
        with patch("services.gate_service.set_limits") as set_limits:
            r = await client.post(
                "/internal/gate/set-limits",
                json={"person_id": "user-1", "vu_limit": 200},
                headers=_bearer(_kernel_server_token()),
            )
        assert r.status_code == 204
        kwargs = set_limits.call_args.kwargs
        assert kwargs["vu_limit"] == 200
        assert kwargs["max_workspaces"] is None
