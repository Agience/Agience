"""Tests for package registry bootstrap + the vnd.agience.package+json type.

Covers:
- package type declares invoke + export operations dispatching to Verso
- type.json schema shape (contents[], dependencies.servers[], linking)
- ensure_package_registry creates the collection idempotently
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services import types_service
from services.bootstrap_types import (
    PACKAGE_CONTENT_TYPE,
    PACKAGE_REGISTRY_COLLECTION_SLUG,
    USER_READABLE_SEED_SLUGS,
)


# ---------------------------------------------------------------------------
#  Type definition
# ---------------------------------------------------------------------------

class TestPackageType:
    def test_invoke_operation_dispatches_to_verso_install(self):
        types_service.invalidate_type_cache()
        op = types_service.resolve_operation(PACKAGE_CONTENT_TYPE, "invoke")
        assert op is not None
        assert op.enabled is True
        assert op.requires_grant == "invoke"
        assert op.dispatch["kind"] == "mcp_tool"
        assert op.dispatch["server_ref"] == "verso"
        assert op.dispatch["tool_ref"] == "install_package"

    def test_export_operation_dispatches_to_verso_export(self):
        types_service.invalidate_type_cache()
        op = types_service.resolve_operation(PACKAGE_CONTENT_TYPE, "export")
        assert op is not None
        assert op.enabled is True
        assert op.requires_grant == "update"
        assert op.dispatch["kind"] == "mcp_tool"
        assert op.dispatch["tool_ref"] == "export_package"

    def test_invoke_emits_lifecycle_events(self):
        types_service.invalidate_type_cache()
        op = types_service.resolve_operation(PACKAGE_CONTENT_TYPE, "invoke")
        events = {e["event"] for e in op.emits}
        assert "package.install.started" in events
        assert "package.install.completed" in events
        assert "package.install.failed" in events

    def test_crud_operations_present(self):
        """Standard CRUD must be available so users can author packages."""
        types_service.invalidate_type_cache()
        for name in ("create", "read", "update", "delete"):
            op = types_service.resolve_operation(PACKAGE_CONTENT_TYPE, name)
            assert op is not None, f"missing operation {name!r}"
            assert op.dispatch["kind"] == "artifact_crud", (
                f"{name} should use artifact_crud"
            )


# ---------------------------------------------------------------------------
#  Registry bootstrap
# ---------------------------------------------------------------------------

class TestRegistryBootstrap:
    def test_registry_slug_registered_for_user_read(self):
        """New users should get auto-read on the registry so browse works."""
        assert PACKAGE_REGISTRY_COLLECTION_SLUG in USER_READABLE_SEED_SLUGS

    def test_ensure_registry_creates_when_missing(self):
        from services.package_registry_content_service import ensure_package_registry
        db = MagicMock()

        with (
            patch(
                "services.package_registry_content_service.get_id",
                return_value="col-pkg-registry",
            ),
            patch(
                "services.package_registry_content_service.db_get_collection_by_id",
                return_value=None,
            ),
            patch(
                "services.package_registry_content_service.db_create_collection",
            ) as mock_create,
        ):
            result = ensure_package_registry(db)

        assert result == "col-pkg-registry"
        mock_create.assert_called_once()
        # db_create_collection(db, collection) --- second positional arg
        call = mock_create.call_args
        coll = call.args[1] if len(call.args) > 1 else call.kwargs.get("collection")
        assert coll is not None
        assert coll.slug == PACKAGE_REGISTRY_COLLECTION_SLUG

    def test_ensure_registry_is_idempotent(self):
        from services.package_registry_content_service import ensure_package_registry
        db = MagicMock()
        existing = MagicMock()
        existing.id = "col-pkg-registry"

        with (
            patch(
                "services.package_registry_content_service.get_id",
                return_value="col-pkg-registry",
            ),
            patch(
                "services.package_registry_content_service.db_get_collection_by_id",
                return_value=existing,
            ),
            patch(
                "services.package_registry_content_service.db_create_collection",
            ) as mock_create,
        ):
            result = ensure_package_registry(db)

        assert result == "col-pkg-registry"
        mock_create.assert_not_called()

    def test_ensure_registry_returns_none_on_failure(self):
        from services.package_registry_content_service import ensure_package_registry
        db = MagicMock()

        with (
            patch(
                "services.package_registry_content_service.get_id",
                return_value="col-pkg-registry",
            ),
            patch(
                "services.package_registry_content_service.db_get_collection_by_id",
                return_value=None,
            ),
            patch(
                "services.package_registry_content_service.db_create_collection",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = ensure_package_registry(db)

        assert result is None
