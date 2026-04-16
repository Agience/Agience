"""Tests for workspace binding set/clear/resolve_multi operations."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from entities.collection import Collection as CollectionEntity, WORKSPACE_CONTENT_TYPE
from services import workspace_service as ws_svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col(owner: str = "user-1", cid: str = "col-1") -> CollectionEntity:
    return CollectionEntity(id=cid, name="Bound Col", created_by=owner)


def _ws_with_bindings(bindings: dict, owner: str = "user-1", wid: str = "ws-1") -> CollectionEntity:
    ctx = json.dumps({"collections": [], "bindings": bindings})
    return CollectionEntity(id=wid, name="My WS", created_by=owner, content_type=WORKSPACE_CONTENT_TYPE, context=ctx)


def _ws(owner: str = "user-1", wid: str = "ws-1") -> CollectionEntity:
    ctx = json.dumps({"collections": []})
    return CollectionEntity(id=wid, name="My WS", created_by=owner, content_type=WORKSPACE_CONTENT_TYPE, context=ctx)


# ---------------------------------------------------------------------------
# set_binding
# ---------------------------------------------------------------------------

class TestSetBinding:
    """Tests for set_binding() happy path and validation."""

    def test_set_single_valued_binding(self):
        db = MagicMock()
        ws = _ws()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=ws),
            patch("services.workspace_service.arango.update_collection", return_value=None),
            patch("services.workspace_service.event_bus") as mock_bus,
        ):
            result = ws_svc.set_binding(db, "user-1", "ws-1", "memory", artifact_id="col-abc")
        assert result == {"artifact_id": "col-abc"}
        mock_bus.emit.assert_called_once()
        call_args = mock_bus.emit.call_args
        assert call_args[0][0] == "workspace.binding.set"

    def test_set_multi_valued_binding(self):
        db = MagicMock()
        ws = _ws()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=ws),
            patch("services.workspace_service.arango.update_collection", return_value=None),
            patch("services.workspace_service.event_bus"),
        ):
            result = ws_svc.set_binding(db, "user-1", "ws-1", "target_collections", artifact_ids=["c1", "c2"])
        assert result == {"artifact_ids": ["c1", "c2"]}

    def test_unknown_role_raises(self):
        db = MagicMock()
        with pytest.raises(ValueError, match="Unknown binding role"):
            ws_svc.set_binding(db, "user-1", "ws-1", "nonexistent_role", artifact_id="x")

    def test_single_role_with_artifact_ids_raises(self):
        db = MagicMock()
        with pytest.raises(ValueError, match="requires artifact_id"):
            ws_svc.set_binding(db, "user-1", "ws-1", "memory", artifact_ids=["c1"])

    def test_multi_role_with_artifact_id_raises(self):
        db = MagicMock()
        with pytest.raises(ValueError, match="requires artifact_ids"):
            ws_svc.set_binding(db, "user-1", "ws-1", "target_collections", artifact_id="c1")


# ---------------------------------------------------------------------------
# clear_binding
# ---------------------------------------------------------------------------

class TestClearBinding:
    """Tests for clear_binding()."""

    def test_clear_existing_binding(self):
        db = MagicMock()
        ws = _ws_with_bindings({"memory": {"artifact_id": "col-1"}})
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=ws),
            patch("services.workspace_service.arango.update_collection") as mock_update,
            patch("services.workspace_service.event_bus") as mock_bus,
        ):
            ws_svc.clear_binding(db, "user-1", "ws-1", "memory")
        # Should have written back without the memory binding
        mock_update.assert_called_once()
        mock_bus.emit.assert_called_once()
        assert mock_bus.emit.call_args[0][0] == "workspace.binding.cleared"

    def test_clear_missing_binding_is_noop(self):
        db = MagicMock()
        ws = _ws()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=ws),
            patch("services.workspace_service.arango.update_collection") as mock_update,
            patch("services.workspace_service.event_bus") as mock_bus,
        ):
            ws_svc.clear_binding(db, "user-1", "ws-1", "memory")
        # No bindings to clear — should not call update
        mock_update.assert_not_called()
        # Event still emitted (idempotent clear)
        mock_bus.emit.assert_called_once()


# ---------------------------------------------------------------------------
# resolve_binding with artifact_id key (new canonical key)
# ---------------------------------------------------------------------------

class TestResolveBindingArtifactIdKey:
    """Tests for resolve_binding() with the new artifact_id key."""

    def test_resolve_with_artifact_id_key(self):
        db = MagicMock()
        ws = _ws_with_bindings({"memory": {"artifact_id": "col-new"}})
        col = _col(cid="col-new")
        with (
            patch("services.workspace_service.arango.get_collection_by_id", side_effect=lambda _db, cid: ws if cid == "ws-1" else col),
            patch("services.workspace_service.arango.get_active_grants_for_principal_resource", return_value=[]),
        ):
            result = ws_svc.resolve_binding(db, "user-1", "ws-1", "memory")
        assert result == "col-new"

    def test_collection_id_key_is_not_resolved(self):
        """collection_id is not a valid binding key — only artifact_id is canonical."""
        db = MagicMock()
        ws = _ws_with_bindings({"memory": {"collection_id": "col-legacy"}})
        with patch("services.workspace_service.arango.get_collection_by_id", return_value=ws):
            result = ws_svc.resolve_binding(db, "user-1", "ws-1", "memory")
        assert result is None


# ---------------------------------------------------------------------------
# resolve_binding_multi
# ---------------------------------------------------------------------------

class TestResolveBindingMulti:
    """Tests for resolve_binding_multi()."""

    def test_resolve_multi_workspace_level(self):
        db = MagicMock()
        ws = _ws_with_bindings({"target_collections": {"artifact_ids": ["c1", "c2"]}})
        with (
            patch("services.workspace_service.arango.get_collection_by_id", side_effect=lambda _db, cid: ws if cid == "ws-1" else _col(cid=cid)),
            patch("services.workspace_service.arango.get_active_grants_for_principal_resource", return_value=[]),
        ):
            result = ws_svc.resolve_binding_multi(db, "user-1", "ws-1", "target_collections")
        assert result == ["c1", "c2"]

    def test_resolve_multi_filters_inaccessible(self):
        db = MagicMock()
        ws = _ws_with_bindings({"target_collections": {"artifact_ids": ["c1", "c2"]}})
        c1 = _col(owner="other-user", cid="c1")  # inaccessible
        c2 = _col(owner="user-1", cid="c2")       # accessible

        def _get_col(_db, cid):
            if cid == "ws-1":
                return ws
            if cid == "c1":
                return c1
            if cid == "c2":
                return c2
            return None

        with (
            patch("services.workspace_service.arango.get_collection_by_id", side_effect=_get_col),
            patch("services.workspace_service.arango.get_active_grants_for_principal_resource", return_value=[]),
        ):
            result = ws_svc.resolve_binding_multi(db, "user-1", "ws-1", "target_collections")
        assert result == ["c2"]

    def test_resolve_multi_missing_role_returns_empty(self):
        db = MagicMock()
        ws = _ws()
        with patch("services.workspace_service.arango.get_collection_by_id", return_value=ws):
            result = ws_svc.resolve_binding_multi(db, "user-1", "ws-1", "target_collections")
        assert result == []

    def test_resolve_multi_step_overrides(self):
        db = MagicMock()
        ws = _ws_with_bindings({"target_collections": {"artifact_ids": ["ws-c1"]}})
        step_ctx = {"bindings": {"target_collections": {"artifact_ids": ["step-c1"]}}}

        def _get_col(_db, cid):
            if cid == "ws-1":
                return ws
            return _col(cid=cid)

        with (
            patch("services.workspace_service.arango.get_collection_by_id", side_effect=_get_col),
            patch("services.workspace_service.arango.get_active_grants_for_principal_resource", return_value=[]),
        ):
            result = ws_svc.resolve_binding_multi(db, "user-1", "ws-1", "target_collections", step_context=step_ctx)
        assert result == ["step-c1"]
