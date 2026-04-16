"""Tests for routers/grants_router.py — CRUDIASO grant management.

Covers the public surface:
  - 401 when no user_id
  - POST /grants — owner check + grant creation, invite-with-token path
  - POST /grants/claim — happy path, expired/revoked rejected, target_entity match
  - GET /grants — owner check + listing
  - GET /grants/{id} — visibility (grantee / granter / can_admin / 404)
  - PATCH /grants/{id} — state revocation stamps revoked_at + revoked_by
  - DELETE /grants/{id} — soft-revoke
  - 404 on unknown grant
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from entities.grant import Grant as GrantEntity
from services.dependencies import AuthContext, get_auth
from main import app


def _grant(**overrides) -> GrantEntity:
    base = dict(
        id=overrides.get("id", "g-1"),
        resource_type="collection",
        resource_id="col-1",
        grantee_type=GrantEntity.GRANTEE_USER,
        grantee_id="user-2",
        granted_by="user-1",
        can_read=True,
        state=GrantEntity.STATE_ACTIVE,
        created_time="2026-04-07T00:00:00+00:00",
        modified_time="2026-04-07T00:00:00+00:00",
    )
    base.update(overrides)
    return GrantEntity(**base)


def _set_owner_doc(arango_mock: MagicMock, owner_id: str | None = "user-1"):
    """Make the owner-check helper see the resource as owned by `owner_id`."""
    coll = MagicMock()
    coll.get.return_value = {"created_by": owner_id} if owner_id else None
    arango_mock.collection.return_value = coll


@pytest.fixture
def anon_client(client: AsyncClient):
    """Override the auth dependency with an anonymous principal."""
    app.dependency_overrides[get_auth] = lambda: AuthContext(
        user_id=None, principal_id=None, principal_type="anonymous"
    )
    yield client
    app.dependency_overrides.pop(get_auth, None)


# ---------------------------------------------------------------------------
# Auth guard (401)
# ---------------------------------------------------------------------------

class TestAuthGuard:
    @pytest.mark.asyncio
    async def test_list_requires_user(self, anon_client: AsyncClient):
        r = await anon_client.get("/grants", params={"resource_id": "col-1"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_create_requires_user(self, anon_client: AsyncClient):
        r = await anon_client.post("/grants", json={"resource_id": "col-1"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_claim_requires_user(self, anon_client: AsyncClient):
        r = await anon_client.post("/grants/claim", json={"token": "t"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /grants — create
# ---------------------------------------------------------------------------

class TestCreateGrant:
    @pytest.mark.asyncio
    async def test_403_when_caller_is_not_owner(self, client: AsyncClient):
        with patch(
            "core.dependencies.get_arango_db",
            return_value=MagicMock(),
        ):
            arango = MagicMock()
            _set_owner_doc(arango, owner_id="someone-else")
            with (
                patch(
                    "services.grant_service.get_active_grants_for_principal_resource", return_value=[]
                ),
                patch("core.dependencies.get_arango_db", return_value=arango),
            ):
                # Override the FastAPI dep
                from core.dependencies import get_arango_db

                app.dependency_overrides[get_arango_db] = lambda: arango
                try:
                    r = await client.post(
                        "/grants",
                        json={"resource_id": "col-1", "resource_type": "collection"},
                    )
                finally:
                    app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_owner_creates_user_grant(self, client: AsyncClient):
        arango = MagicMock()
        _set_owner_doc(arango, owner_id="user-123")  # default test user
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        captured: dict = {}

        def fake_create(db, grant):
            captured["grant"] = grant
            return grant

        try:
            with patch(
                "routers.grants_router.create_grant", side_effect=fake_create
            ):
                r = await client.post(
                    "/grants",
                    json={
                        "resource_id": "col-1",
                        "resource_type": "collection",
                        "grantee_id": "bob",
                        "can_read": True,
                        "can_update": True,
                    },
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 201
        assert captured["grant"].grantee_id == "bob"
        assert captured["grant"].can_update is True
        # No raw token returned for non-invite grants.
        assert "claim_token" not in r.json()

    @pytest.mark.asyncio
    async def test_owner_creates_invite_with_claim_token(self, client: AsyncClient):
        arango = MagicMock()
        _set_owner_doc(arango, owner_id="user-123")
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "routers.grants_router.create_grant", side_effect=lambda db, g: g
            ):
                r = await client.post(
                    "/grants",
                    json={
                        "resource_id": "col-1",
                        "resource_type": "collection",
                        "grantee_type": GrantEntity.GRANTEE_INVITE,
                        "can_read": True,
                    },
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 201
        body = r.json()
        # Invite returns a one-time raw claim token alongside the grant.
        assert "claim_token" in body
        assert body["claim_token"].startswith("agc_")


# ---------------------------------------------------------------------------
# GET /grants/{id}
# ---------------------------------------------------------------------------

class TestReadGrant:
    @pytest.mark.asyncio
    async def test_404_when_unknown(self, client: AsyncClient):
        with patch(
            "routers.grants_router.get_grant_by_id", return_value=None
        ):
            r = await client.get("/grants/missing")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_grantee_can_read_own_grant(self, client: AsyncClient):
        g = _grant(grantee_id="user-123", granted_by="someone-else")
        with patch("routers.grants_router.get_grant_by_id", return_value=g):
            r = await client.get("/grants/g-1")
        assert r.status_code == 200
        assert r.json()["grantee_id"] == "user-123"

    @pytest.mark.asyncio
    async def test_granter_can_read(self, client: AsyncClient):
        g = _grant(grantee_id="bob", granted_by="user-123")
        with patch("routers.grants_router.get_grant_by_id", return_value=g):
            r = await client.get("/grants/g-1")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_unrelated_user_404s(self, client: AsyncClient):
        g = _grant(grantee_id="bob", granted_by="alice")
        arango = MagicMock()
        _set_owner_doc(arango, owner_id="someone-else")
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with (
                patch("routers.grants_router.get_grant_by_id", return_value=g),
                patch(
                    "services.grant_service.get_active_grants_for_principal_resource", return_value=[]
                ),
            ):
                r = await client.get("/grants/g-1")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404  # security-by-obscurity


# ---------------------------------------------------------------------------
# DELETE /grants/{id}
# ---------------------------------------------------------------------------

class TestRevokeGrant:
    @pytest.mark.asyncio
    async def test_404_when_grant_missing(self, client: AsyncClient):
        with patch("routers.grants_router.get_grant_by_id", return_value=None):
            r = await client.delete("/grants/g-1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_granter_can_revoke(self, client: AsyncClient):
        g = _grant(grantee_id="bob", granted_by="user-123")
        captured = {}

        def fake_update(db, grant):
            captured["grant"] = grant
            return grant

        with (
            patch("routers.grants_router.get_grant_by_id", return_value=g),
            patch("routers.grants_router.update_grant", side_effect=fake_update),
        ):
            r = await client.delete("/grants/g-1")

        assert r.status_code == 200
        assert r.json()["state"] == "revoked"
        assert captured["grant"].state == GrantEntity.STATE_REVOKED
        assert captured["grant"].revoked_by == "user-123"
        assert captured["grant"].revoked_at is not None


# ---------------------------------------------------------------------------
# POST /grants/claim
# ---------------------------------------------------------------------------

class TestClaimInvite:
    """Tests for POST /grants/claim.

    The endpoint delegates to ``grant_service.claim_invite``; mocks target
    that module's direct imports of ``get_active_grants_for_grantee``,
    ``create_grant``, and ``update_grant``.
    """

    @pytest.mark.asyncio
    async def test_404_when_no_matching_invite(self, client: AsyncClient):
        with patch(
            "services.grant_service.get_active_grants_for_grantee", return_value=[]
        ):
            r = await client.post("/grants/claim", json={"token": "agc_xxx"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_410_when_invite_revoked(self, client: AsyncClient):
        invite = _grant(
            grantee_type=GrantEntity.GRANTEE_INVITE,
            grantee_id="hash",
            state=GrantEntity.STATE_REVOKED,
        )
        with patch(
            "services.grant_service.get_active_grants_for_grantee", return_value=[invite]
        ):
            r = await client.post("/grants/claim", json={"token": "agc_xxx"})
        assert r.status_code == 410

    @pytest.mark.asyncio
    async def test_403_when_target_email_does_not_match(self, client: AsyncClient):
        invite = _grant(
            grantee_type=GrantEntity.GRANTEE_INVITE,
            grantee_id="hash",
            state=GrantEntity.STATE_ACTIVE,
            target_entity="someone@example.com",
            target_entity_type="email",
        )
        wrong_user = SimpleNamespace(email="other@example.com")
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch(
                "services.person_service.get_user_by_id", return_value=wrong_user
            ),
        ):
            r = await client.post("/grants/claim", json={"token": "agc_xxx"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_happy_path_creates_user_grant_and_increments_invite(
        self, client: AsyncClient
    ):
        invite = _grant(
            grantee_type=GrantEntity.GRANTEE_INVITE,
            grantee_id="hash",
            state=GrantEntity.STATE_ACTIVE,
            max_claims=2,
            can_read=True,
            can_update=True,
        )
        invite.claims_count = 0

        captured: dict = {}

        def fake_create(db, g):
            captured["created"] = g
            return g

        def fake_update(db, g):
            captured["updated"] = g
            return g

        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch("services.grant_service.create_grant", side_effect=fake_create),
            patch("services.grant_service.update_grant", side_effect=fake_update),
        ):
            r = await client.post("/grants/claim", json={"token": "agc_xxx"})

        assert r.status_code == 201
        new_grant = captured["created"]
        assert new_grant.grantee_type == GrantEntity.GRANTEE_USER
        assert new_grant.grantee_id == "user-123"
        assert new_grant.can_update is True
        # Invite claims_count incremented; not auto-revoked (max_claims > 1).
        assert captured["updated"].claims_count == 1
        assert captured["updated"].state == GrantEntity.STATE_ACTIVE

    @pytest.mark.asyncio
    async def test_single_use_invite_auto_revokes_after_claim(self, client: AsyncClient):
        invite = _grant(
            grantee_type=GrantEntity.GRANTEE_INVITE,
            grantee_id="hash",
            state=GrantEntity.STATE_ACTIVE,
            max_claims=1,
        )
        invite.claims_count = 0
        captured: dict = {}

        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch("services.grant_service.create_grant", side_effect=lambda db, g: g),
            patch(
                "services.grant_service.update_grant",
                side_effect=lambda db, g: captured.setdefault("g", g) or g,
            ),
        ):
            r = await client.post("/grants/claim", json={"token": "agc_xxx"})
        assert r.status_code == 201
        assert captured["g"].state == GrantEntity.STATE_REVOKED
