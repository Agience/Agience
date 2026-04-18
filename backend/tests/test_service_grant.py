"""Unit tests for services.grant_service.

Covers:
- create_invite: role-preset mapping, token generation, target-email storage
- claim_invite: identity match rules, max_claims, auto-revoke of single-use
- get_invite_context: pre-auth (no PII) shape
- get_invite_details: post-auth; target mismatch leaks no PII
- typed exceptions (InviteNotFound / InviteExhausted / InviteIdentityMismatch)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from entities.grant import Grant as GrantEntity
from services import grant_service
from services.grant_service import (
    InviteExhausted,
    InviteIdentityMismatch,
    InviteNotFound,
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _invite(**overrides) -> GrantEntity:
    base = dict(
        resource_id="ws-1",
        grantee_type=GrantEntity.GRANTEE_INVITE,
        grantee_id="hash-of-token",
        granted_by="inviter",
        can_read=True,
        state=GrantEntity.STATE_ACTIVE,
        max_claims=1,
        claims_count=0,
        name="Test invite",
    )
    base.update(overrides)
    return GrantEntity(**base)


@pytest.fixture
def db() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
#  Permission helpers
# ---------------------------------------------------------------------------

class TestPermissionHelpers:
    def test_is_creator_true_when_created_by_matches(self):
        db = MagicMock()
        db.collection.return_value.get.return_value = {
            "_key": "ws-1", "created_by": "u-1",
        }
        assert grant_service.is_creator(db, "u-1", "ws-1") is True

    def test_is_creator_false_when_created_by_differs(self):
        db = MagicMock()
        db.collection.return_value.get.return_value = {
            "_key": "ws-1", "created_by": "u-2",
        }
        assert grant_service.is_creator(db, "u-1", "ws-1") is False

    def test_is_creator_false_when_doc_missing(self):
        db = MagicMock()
        db.collection.return_value.get.return_value = None
        assert grant_service.is_creator(db, "u-1", "ws-1") is False

    def test_is_creator_false_when_user_id_empty(self):
        assert grant_service.is_creator(MagicMock(), "", "ws-1") is False

    def test_can_share_creator_shortcircuits(self):
        db = MagicMock()
        db.collection.return_value.get.return_value = {
            "_key": "ws-1", "created_by": "u-1",
        }
        # No grants needed; creator always wins.
        with patch(
            "services.grant_service.get_active_grants_for_principal_resource",
            return_value=[],
        ):
            assert grant_service.can_share(db, "u-1", "ws-1") is True

    def test_can_share_with_share_grant(self):
        db = MagicMock()
        db.collection.return_value.get.return_value = {"created_by": "other"}
        share_grant = _invite(
            grantee_type=GrantEntity.GRANTEE_USER,
            grantee_id="u-1",
            can_share=True,
        )
        with patch(
            "services.grant_service.get_active_grants_for_principal_resource",
            return_value=[share_grant],
        ):
            assert grant_service.can_share(db, "u-1", "ws-1") is True

    def test_can_share_with_admin_grant(self):
        db = MagicMock()
        db.collection.return_value.get.return_value = {"created_by": "other"}
        admin_grant = _invite(
            grantee_type=GrantEntity.GRANTEE_USER,
            grantee_id="u-1",
            can_admin=True,
        )
        with patch(
            "services.grant_service.get_active_grants_for_principal_resource",
            return_value=[admin_grant],
        ):
            assert grant_service.can_share(db, "u-1", "ws-1") is True

    def test_can_share_false_without_any_grant(self):
        db = MagicMock()
        db.collection.return_value.get.return_value = {"created_by": "other"}
        reader_grant = _invite(
            grantee_type=GrantEntity.GRANTEE_USER,
            grantee_id="u-1",
            can_read=True,
        )
        with patch(
            "services.grant_service.get_active_grants_for_principal_resource",
            return_value=[reader_grant],
        ):
            assert grant_service.can_share(db, "u-1", "ws-1") is False

    def test_can_admin_does_not_accept_share_grant(self):
        """can_admin is stricter than can_share --- share alone doesn't qualify."""
        db = MagicMock()
        db.collection.return_value.get.return_value = {"created_by": "other"}
        share_only = _invite(
            grantee_type=GrantEntity.GRANTEE_USER,
            grantee_id="u-1",
            can_share=True,
            can_admin=False,
        )
        with patch(
            "services.grant_service.get_active_grants_for_principal_resource",
            return_value=[share_only],
        ):
            assert grant_service.can_admin(db, "u-1", "ws-1") is False


# ---------------------------------------------------------------------------
#  Event emission
# ---------------------------------------------------------------------------

class TestInviteEvents:
    """grant_service emits telemetry events so any surface (HTTP, MCP, CLI)
    that goes through the service gets consistent event streams without
    having to remember to emit itself."""

    def test_create_invite_emits_grant_invite_created(self, db):
        emitted: dict = {}

        def fake_emit(container_id, event_name, data, actor_id=None):
            emitted["container_id"] = container_id
            emitted["event"] = event_name
            emitted["data"] = data
            emitted["actor_id"] = actor_id

        with (
            patch("services.grant_service.create_grant",
                  side_effect=lambda _d, g: g),
            patch("core.event_bus.emit_artifact_event_sync",
                  side_effect=fake_emit),
        ):
            grant_service.create_invite(
                db, user_id="u-1", resource_id="ws-1",
                role="viewer", send_email=False,
            )

        assert emitted["container_id"] == "ws-1"
        assert emitted["event"] == "grant.invite.created"
        assert emitted["data"]["role"] == "viewer"
        assert emitted["actor_id"] == "u-1"

    def test_claim_invite_emits_grant_invite_claimed(self, db):
        invite = _invite(max_claims=2, claims_count=0)
        emitted: dict = {}

        def fake_emit(container_id, event_name, data, actor_id=None):
            emitted["container_id"] = container_id
            emitted["event"] = event_name
            emitted["data"] = data

        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch("services.grant_service.create_grant",
                  side_effect=lambda _d, g: g),
            patch("services.grant_service.update_grant",
                  side_effect=lambda _d, g: g),
            patch("core.event_bus.emit_artifact_event_sync",
                  side_effect=fake_emit),
        ):
            grant_service.claim_invite(db, "u-1", "agc_xxx")

        assert emitted["event"] == "grant.invite.claimed"
        assert emitted["data"]["user_id"] == "u-1"
        assert emitted["data"]["invite_id"] == invite.id

    def test_create_invite_send_email_false_skips_delivery(self, db):
        """Unit tests shouldn't rely on email config; send_email=False
        keeps the invite creation side-effect-free."""
        with (
            patch("services.grant_service.create_grant",
                  side_effect=lambda _d, g: g),
            patch("services.email_service.send_invite") as mock_send,
            patch("core.event_bus.emit_artifact_event_sync"),
        ):
            grant_service.create_invite(
                db, user_id="u-1", resource_id="ws-1",
                role="viewer", target_email="bob@example.com",
                send_email=False,
            )

        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
#  list_invites_sent
# ---------------------------------------------------------------------------

class TestListInvitesSent:
    def test_filters_by_granted_by_and_invite_type(self, db):
        """Only returns invite-type grants created by the caller."""
        fake_invite = _invite(granted_by="u-1")

        captured: dict = {}

        def fake_query(_db, _cls, _coll, filters):
            captured["filters"] = filters
            return [fake_invite]

        with patch(
            "db.arango.query_documents", side_effect=fake_query,
        ):
            result = grant_service.list_invites_sent(db, "u-1")

        assert result == [fake_invite]
        assert captured["filters"]["grantee_type"] == GrantEntity.GRANTEE_INVITE
        assert captured["filters"]["granted_by"] == "u-1"
        assert captured["filters"]["state"] == GrantEntity.STATE_ACTIVE

    def test_include_revoked_drops_state_filter(self, db):
        captured: dict = {}

        def fake_query(_db, _cls, _coll, filters):
            captured["filters"] = filters
            return []

        with patch(
            "db.arango.query_documents", side_effect=fake_query,
        ):
            grant_service.list_invites_sent(db, "u-1", include_revoked=True)

        assert "state" not in captured["filters"]


# ---------------------------------------------------------------------------
#  create_invite
# ---------------------------------------------------------------------------

class TestCreateInvite:
    def test_unknown_role_raises(self, db):
        with pytest.raises(ValueError, match="Unknown role"):
            grant_service.create_invite(
                db, user_id="u-1", resource_id="ws-1", role="nope",
            )

    def test_viewer_preset_grants_only_read(self, db):
        captured = {}

        def fake_create(_db, g):
            captured["g"] = g
            return g

        with patch("services.grant_service.create_grant", side_effect=fake_create):
            grant, token = grant_service.create_invite(
                db, user_id="u-1", resource_id="ws-1", role="viewer",
            )

        assert grant.can_read is True
        assert grant.can_create is False
        assert grant.can_update is False
        assert grant.can_admin is False
        assert grant.can_share is False
        assert token.startswith("agc_")
        assert grant.grantee_type == GrantEntity.GRANTEE_INVITE
        assert grant.grantee_id != token  # stored as hash, not raw

    def test_admin_preset_grants_everything(self, db):
        with patch("services.grant_service.create_grant", side_effect=lambda _d, g: g):
            grant, _ = grant_service.create_invite(
                db, user_id="u-1", resource_id="ws-1", role="admin",
            )
        for flag in ("can_create", "can_read", "can_update", "can_delete",
                     "can_invoke", "can_add", "can_share", "can_admin"):
            assert getattr(grant, flag) is True, f"admin should grant {flag}"

    def test_target_email_stored_lowercased(self, db):
        with patch("services.grant_service.create_grant", side_effect=lambda _d, g: g):
            grant, _ = grant_service.create_invite(
                db, user_id="u-1", resource_id="ws-1",
                role="editor", target_email="Alice@Example.COM",
            )
        assert grant.target_entity == "alice@example.com"
        assert grant.target_entity_type == "email"
        assert grant.requires_identity is True

    def test_no_target_means_no_identity_requirement(self, db):
        with patch("services.grant_service.create_grant", side_effect=lambda _d, g: g):
            grant, _ = grant_service.create_invite(
                db, user_id="u-1", resource_id="ws-1",
                role="editor", target_email=None,
            )
        assert grant.target_entity is None
        assert grant.requires_identity is False


# ---------------------------------------------------------------------------
#  claim_invite
# ---------------------------------------------------------------------------

class TestClaimInvite:
    def test_no_candidates_raises_not_found(self, db):
        with patch(
            "services.grant_service.get_active_grants_for_grantee",
            return_value=[],
        ):
            with pytest.raises(InviteNotFound):
                grant_service.claim_invite(db, "u-1", "agc_xxx")

    def test_revoked_invite_raises_exhausted(self, db):
        invite = _invite(state=GrantEntity.STATE_REVOKED)
        with patch(
            "services.grant_service.get_active_grants_for_grantee",
            return_value=[invite],
        ):
            with pytest.raises(InviteExhausted):
                grant_service.claim_invite(db, "u-1", "agc_xxx")

    def test_at_max_claims_raises_exhausted(self, db):
        invite = _invite(max_claims=2, claims_count=2)
        with patch(
            "services.grant_service.get_active_grants_for_grantee",
            return_value=[invite],
        ):
            with pytest.raises(InviteExhausted):
                grant_service.claim_invite(db, "u-1", "agc_xxx")

    def test_target_email_mismatch_raises(self, db):
        invite = _invite(
            target_entity="alice@example.com",
            target_entity_type="email",
        )
        wrong_user = SimpleNamespace(id="u-1", email="bob@example.com")
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch(
                "services.person_service.get_user_by_id",
                return_value=wrong_user,
            ),
        ):
            with pytest.raises(InviteIdentityMismatch):
                grant_service.claim_invite(db, "u-1", "agc_xxx")

    def test_target_email_match_is_case_insensitive(self, db):
        invite = _invite(
            target_entity="alice@example.com",
            target_entity_type="email",
        )
        right_user = SimpleNamespace(id="u-1", email="ALICE@EXAMPLE.COM")
        captured = {}
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch(
                "services.person_service.get_user_by_id",
                return_value=right_user,
            ),
            patch("services.grant_service.create_grant",
                  side_effect=lambda _d, g: captured.setdefault("created", g) or g),
            patch("services.grant_service.update_grant",
                  side_effect=lambda _d, g: captured.setdefault("updated", g) or g),
        ):
            grant = grant_service.claim_invite(db, "u-1", "agc_xxx")
        assert grant.grantee_type == GrantEntity.GRANTEE_USER
        assert grant.grantee_id == "u-1"

    def test_domain_match(self, db):
        invite = _invite(
            target_entity="example.com",
            target_entity_type="domain",
        )
        right_user = SimpleNamespace(id="u-1", email="anyone@Example.COM")
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch(
                "services.person_service.get_user_by_id",
                return_value=right_user,
            ),
            patch("services.grant_service.create_grant",
                  side_effect=lambda _d, g: g),
            patch("services.grant_service.update_grant",
                  side_effect=lambda _d, g: g),
        ):
            grant = grant_service.claim_invite(db, "u-1", "agc_xxx")
        assert grant is not None

    def test_happy_path_copies_permissions_and_increments_claims(self, db):
        invite = _invite(
            max_claims=3, claims_count=0,
            can_read=True, can_update=True,
        )
        captured = {}
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch("services.grant_service.create_grant",
                  side_effect=lambda _d, g: captured.setdefault("new", g) or g),
            patch("services.grant_service.update_grant",
                  side_effect=lambda _d, g: captured.setdefault("updated", g) or g),
        ):
            new_grant = grant_service.claim_invite(db, "u-1", "agc_xxx")

        assert new_grant.can_read is True
        assert new_grant.can_update is True
        assert new_grant.grantee_id == "u-1"
        assert captured["updated"].claims_count == 1
        # Not single-use, so still active.
        assert captured["updated"].state == GrantEntity.STATE_ACTIVE

    def test_single_use_auto_revokes(self, db):
        invite = _invite(max_claims=1, claims_count=0)
        captured = {}
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch("services.grant_service.create_grant",
                  side_effect=lambda _d, g: g),
            patch("services.grant_service.update_grant",
                  side_effect=lambda _d, g: captured.setdefault("updated", g) or g),
        ):
            grant_service.claim_invite(db, "u-1", "agc_xxx")
        assert captured["updated"].state == GrantEntity.STATE_REVOKED
        assert captured["updated"].revoked_by == "u-1"


# ---------------------------------------------------------------------------
#  get_invite_context (pre-auth)
# ---------------------------------------------------------------------------

class TestInviteContext:
    def test_unknown_token_returns_none(self, db):
        with patch(
            "services.grant_service.get_active_grants_for_grantee",
            return_value=[],
        ):
            assert grant_service.get_invite_context(db, "agc_xxx") is None

    def test_no_target_returns_has_target_false(self, db):
        invite = _invite(target_entity=None, target_entity_type=None)
        with patch(
            "services.grant_service.get_active_grants_for_grantee",
            return_value=[invite],
        ):
            ctx = grant_service.get_invite_context(db, "agc_xxx")
        assert ctx == {"valid": True, "has_target": False, "target_type": None}

    def test_with_email_target_returns_minimal_info(self, db):
        invite = _invite(
            target_entity="alice@example.com",
            target_entity_type="email",
        )
        with patch(
            "services.grant_service.get_active_grants_for_grantee",
            return_value=[invite],
        ):
            ctx = grant_service.get_invite_context(db, "agc_xxx")
        # No resource_id, no inviter --- PII-safe.
        assert ctx == {"valid": True, "has_target": True, "target_type": "email"}
        assert "resource_id" not in ctx
        assert "granted_by" not in ctx


# ---------------------------------------------------------------------------
#  get_invite_details (post-auth)
# ---------------------------------------------------------------------------

class TestInviteDetails:
    def test_mismatch_returns_flag_only(self, db):
        invite = _invite(
            target_entity="alice@example.com",
            target_entity_type="email",
        )
        wrong_user = SimpleNamespace(id="u-1", email="bob@example.com")
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch(
                "services.person_service.get_user_by_id",
                return_value=wrong_user,
            ),
        ):
            details = grant_service.get_invite_details(db, "agc_xxx", "u-1")
        assert details == {"valid": True, "identity_mismatch": True}
        assert "resource_id" not in details
        assert "granted_by" not in details

    def test_match_returns_full_context(self, db):
        invite = _invite(
            target_entity="alice@example.com",
            target_entity_type="email",
        )
        right_user = SimpleNamespace(id="u-1", email="alice@example.com")
        with (
            patch(
                "services.grant_service.get_active_grants_for_grantee",
                return_value=[invite],
            ),
            patch(
                "services.person_service.get_user_by_id",
                return_value=right_user,
            ),
        ):
            details = grant_service.get_invite_details(db, "agc_xxx", "u-1")
        assert details["resource_id"] == "ws-1"
        assert details["granted_by"] == "inviter"

    def test_open_invite_returns_details_without_match_check(self, db):
        invite = _invite(target_entity=None, target_entity_type=None)
        with patch(
            "services.grant_service.get_active_grants_for_grantee",
            return_value=[invite],
        ):
            details = grant_service.get_invite_details(db, "agc_xxx", "u-1")
        assert details["resource_id"] == "ws-1"
