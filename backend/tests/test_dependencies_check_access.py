"""Unit tests for services.dependencies.check_access and require_platform_admin.

`test_auth_dependencies.py` covers `resolve_auth` (token → AuthContext).
This file covers the access mediator (`check_access`) and the platform-admin
guard, both of which sit in front of every router and have heavy churn.

Coverage:
  - check_access action → CRUDEASIO flag mapping (every action in _ACTION_FLAG_MAP)
  - Unknown action → 400
  - artifact_id resolves to a Collection doc (workspace/regular)
  - artifact_id resolves to an Artifact doc → looks up its collection_id
  - artifact missing → 404
  - artifact with no collection_id → 404
  - collection missing → 404
  - Creator with explicit grant passes (grant-based, not created_by fast-path)
  - Active grant on the collection returns that grant
  - No matching grant → 404 (security-by-obscurity)
  - created_by alone does NOT grant access
  - require_platform_admin: missing user → 403
  - require_platform_admin: bootstrap operator fast-path
  - require_platform_admin: write grant on authority collection
  - require_platform_admin: no grant → 403
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from entities.grant import Grant as GrantEntity
from services.dependencies import (
    AuthContext,
    check_access,
    require_platform_admin,
    _ACTION_FLAG_MAP,
)


def _user(uid: str = "user-1") -> AuthContext:
    return AuthContext(user_id=uid, principal_id=uid, principal_type="user")


def _arango_with(*, collection_doc=None, artifact_doc=None, child_collection_doc=None):
    """Build a MagicMock arango_db wired up to return the given docs.

    Container-as-artifact: all lookups go through `"artifacts"`.
    - collection_doc: a container artifact (no collection_id field)
    - artifact_doc: a regular artifact (has collection_id field)
    - child_collection_doc: the parent container looked up via artifact_doc.collection_id
    """
    db = MagicMock()
    art = MagicMock()

    def art_get(key):
        if collection_doc and key == collection_doc.get("_key"):
            return collection_doc
        if artifact_doc and key == artifact_doc.get("_key"):
            return artifact_doc
        if child_collection_doc and key == child_collection_doc.get("_key"):
            return child_collection_doc
        return None

    art.get.side_effect = art_get
    db.collection.return_value = art
    return db


def _grant(**overrides) -> GrantEntity:
    return GrantEntity(
        id=overrides.get("id", "g-1"),
        resource_id=overrides.get("resource_id", "col-1"),
        grantee_type=overrides.get("grantee_type", GrantEntity.GRANTEE_USER),
        grantee_id=overrides.get("grantee_id", "user-1"),
        granted_by="user-1",
        can_create=overrides.get("can_create", False),
        can_read=overrides.get("can_read", False),
        can_update=overrides.get("can_update", False),
        can_delete=overrides.get("can_delete", False),
        can_evict=overrides.get("can_evict", False),
        can_invoke=overrides.get("can_invoke", False),
        can_add=overrides.get("can_add", False),
        can_share=overrides.get("can_share", False),
        can_admin=overrides.get("can_admin", False),
        state="active",
    )


# ---------------------------------------------------------------------------
# check_access — argument validation
# ---------------------------------------------------------------------------

class TestActionMapping:
    def test_unknown_action_returns_400(self):
        with pytest.raises(HTTPException) as ei:
            check_access(_user(), "art-1", "transmogrify", MagicMock())
        assert ei.value.status_code == 400
        assert "Unknown action" in ei.value.detail

    def test_action_map_covers_full_crudeasio(self):
        assert set(_ACTION_FLAG_MAP) == {
            "create", "read", "update", "delete", "evict",
            "invoke", "add", "share", "admin",
        }


# ---------------------------------------------------------------------------
# check_access — id resolution
# ---------------------------------------------------------------------------

class TestIdResolution:
    def test_collection_id_directly(self):
        db = _arango_with(
            collection_doc={"_key": "col-1", "created_by": "user-1"}
        )
        with patch(
            "services.dependencies.db_get_active_grants",
            return_value=[_grant(can_read=True)],
        ):
            grant = check_access(_user(), "col-1", "read", db)
        assert grant.can_read is True
        assert grant.resource_id == "col-1"

    def test_artifact_grant_cascades_via_origin_edge(self):
        db = _arango_with(
            artifact_doc={"_key": "art-1", "collection_id": "col-1"},
            child_collection_doc={"_key": "col-1"},
        )
        # No direct grant on the artifact; grant on parent reached via origin edge
        with patch(
            "services.dependencies.db_get_active_grants",
            side_effect=[
                [],                           # direct grants on art-1
                [_grant(can_read=True)],       # parent grants on col-1
            ],
        ), patch(
            "services.dependencies.db_arango.get_origin_parent",
            return_value=("col-1", None),     # origin edge: art-1 → col-1
        ):
            grant = check_access(_user(), "art-1", "read", db)
        assert grant.can_read is True

    def test_artifact_missing_returns_404(self):
        db = _arango_with()
        with pytest.raises(HTTPException) as ei:
            check_access(_user(), "missing", "read", db)
        assert ei.value.status_code == 404

    def test_artifact_with_no_collection_id_returns_404(self):
        db = _arango_with(artifact_doc={"_key": "art-1"})  # no collection_id
        with pytest.raises(HTTPException) as ei:
            check_access(_user(), "art-1", "read", db)
        assert ei.value.status_code == 404

    def test_orphan_artifact_pointing_to_missing_collection_returns_404(self):
        db = _arango_with(
            artifact_doc={"_key": "art-1", "collection_id": "ghost-col"},
            # No child_collection_doc — the parent collection doesn't exist.
        )
        with pytest.raises(HTTPException) as ei:
            check_access(_user(), "art-1", "read", db)
        assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# check_access — authorization
# ---------------------------------------------------------------------------

class TestAuthorization:
    def test_creator_with_explicit_grant_gets_full_access(self):
        """Creator has explicit grant issued at creation time — not created_by fast-path."""
        db = _arango_with(
            collection_doc={"_key": "col-1", "created_by": "user-1"}
        )
        full_grant = _grant(
            can_create=True, can_read=True, can_update=True, can_delete=True,
            can_evict=True, can_invoke=True, can_add=True, can_share=True, can_admin=True,
        )
        with patch(
            "services.dependencies.db_get_active_grants",
            return_value=[full_grant],
        ):
            grant = check_access(_user("user-1"), "col-1", "delete", db)
        assert all(
            getattr(grant, f) for f in (
                "can_create",
                "can_read",
                "can_update",
                "can_delete",
                "can_evict",
                "can_invoke",
                "can_add",
                "can_share",
                "can_admin",
            )
        )

    def test_created_by_alone_does_not_grant_access(self):
        """created_by is provenance only — without an explicit grant, access is denied."""
        db = _arango_with(
            collection_doc={"_key": "col-1", "created_by": "user-1"}
        )
        with patch(
            "services.dependencies.db_get_active_grants", return_value=[]
        ):
            with pytest.raises(HTTPException) as ei:
                check_access(_user("user-1"), "col-1", "read", db)
        assert ei.value.status_code == 404

    def test_non_owner_with_matching_grant_passes(self):
        db = _arango_with(
            collection_doc={"_key": "col-1", "created_by": "someone-else"}
        )
        with patch(
            "services.dependencies.db_get_active_grants",
            return_value=[_grant(can_read=True)],
        ):
            grant = check_access(_user("user-2"), "col-1", "read", db)
        assert grant.can_read is True

    def test_non_owner_with_grant_for_wrong_action_returns_404(self):
        db = _arango_with(
            collection_doc={"_key": "col-1", "created_by": "someone-else"}
        )
        with patch(
            "services.dependencies.db_get_active_grants",
            return_value=[_grant(can_read=True)],  # only read; we ask for delete
        ):
            with pytest.raises(HTTPException) as ei:
                check_access(_user("user-2"), "col-1", "delete", db)
        assert ei.value.status_code == 404

    def test_no_grants_returns_404(self):
        db = _arango_with(
            collection_doc={"_key": "col-1", "created_by": "someone-else"}
        )
        with patch(
            "services.dependencies.db_get_active_grants", return_value=[]
        ):
            with pytest.raises(HTTPException) as ei:
                check_access(_user("user-2"), "col-1", "read", db)
        assert ei.value.status_code == 404

    def test_anonymous_user_blocked(self):
        """`auth.user_id is None` is always denied — grants require user identity."""
        db = _arango_with(collection_doc={"_key": "col-1", "created_by": None})
        anon = AuthContext(user_id=None, principal_id=None, principal_type="anonymous")
        with pytest.raises(HTTPException) as ei:
            check_access(anon, "col-1", "read", db)
        assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# require_platform_admin
# ---------------------------------------------------------------------------

class TestRequirePlatformAdmin:
    def test_403_without_user_id(self):
        anon = AuthContext(user_id=None, principal_id=None, principal_type="anonymous")
        with pytest.raises(HTTPException) as ei:
            require_platform_admin(anon, MagicMock())
        assert ei.value.status_code == 403

    def test_bootstrap_operator_fast_path(self):
        with patch(
            "services.platform_settings_service.settings.get",
            return_value="user-1",
        ):
            uid = require_platform_admin(_user("user-1"), MagicMock())
        assert uid == "user-1"

    def test_bootstrap_operator_id_mismatch_falls_through(self):
        # Operator id is set, but caller is not the operator → must check grants.
        with (
            patch(
                "services.platform_settings_service.settings.get",
                return_value="other-user",
            ),
            patch(
                "services.dependencies.db_get_active_grants", return_value=[]
            ),
            patch("services.dependencies.get_id", return_value="authority-col"),
        ):
            with pytest.raises(HTTPException) as ei:
                require_platform_admin(_user("user-1"), MagicMock())
        assert ei.value.status_code == 403

    def test_write_grant_on_authority_grants_admin(self):
        write_grant = _grant(can_update=True)
        # Make is_active() return True (it already does for state="active").
        with (
            patch(
                "services.platform_settings_service.settings.get", return_value=None
            ),
            patch(
                "services.dependencies.db_get_active_grants",
                return_value=[write_grant],
            ),
            patch("services.dependencies.get_id", return_value="authority-col"),
        ):
            uid = require_platform_admin(_user("user-1"), MagicMock())
        assert uid == "user-1"

    def test_read_only_grant_on_authority_does_not_grant_admin(self):
        read_grant = _grant(can_read=True, can_update=False)
        with (
            patch(
                "services.platform_settings_service.settings.get", return_value=None
            ),
            patch(
                "services.dependencies.db_get_active_grants",
                return_value=[read_grant],
            ),
            patch("services.dependencies.get_id", return_value="authority-col"),
        ):
            with pytest.raises(HTTPException) as ei:
                require_platform_admin(_user("user-1"), MagicMock())
        assert ei.value.status_code == 403
