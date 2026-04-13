"""Unit tests for services.platform_topology.

The runtime slug → UUID registry that the entire platform depends on for
collection lookups during request handling. Strict get_id() must raise on a
missing slug; get_id_optional() must return None. pre_resolve_platform_ids()
must register every category of slug (collections, singleton artifacts, agents,
LLM connections, MCP servers).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from services import platform_topology as pt
from services.bootstrap_types import (
    AGENT_ARTIFACT_SLUG_PREFIX,
    ALL_PLATFORM_COLLECTION_SLUGS,
    AUTHORITY_ARTIFACT_SLUG,
    LLM_CONNECTION_SLUG_PREFIX,
    PLATFORM_AGENT_SLUGS,
    PLATFORM_LLM_CONNECTION_SLUGS,
    SERVER_ARTIFACT_SLUG_PREFIX,
)
from services import server_registry


@pytest.fixture
def empty_registry():
    """Snapshot + clear before, restore after — keeps tests hermetic without
    breaking other tests that rely on the autouse conftest seeding."""
    snap = dict(pt._registry)
    pt.clear_registry()
    yield
    pt._registry.clear()
    pt._registry.update(snap)


class TestRegistryBasics:
    def test_register_and_get(self, empty_registry):
        pt.register_id("slug-a", "uuid-a")
        assert pt.get_id("slug-a") == "uuid-a"
        assert pt.get_id_optional("slug-a") == "uuid-a"

    def test_get_strict_raises_on_missing(self, empty_registry):
        with pytest.raises(RuntimeError, match="not registered"):
            pt.get_id("missing")

    def test_get_optional_returns_none_on_missing(self, empty_registry):
        assert pt.get_id_optional("missing") is None

    def test_clear_registry_empties(self, empty_registry):
        pt.register_id("a", "1")
        pt.clear_registry()
        assert pt.get_id_optional("a") is None

    def test_register_overwrites(self, empty_registry):
        pt.register_id("slug-a", "uuid-1")
        pt.register_id("slug-a", "uuid-2")
        assert pt.get_id("slug-a") == "uuid-2"

    def test_get_all_platform_collection_ids_returns_one_per_slug(self, empty_registry):
        for slug in ALL_PLATFORM_COLLECTION_SLUGS:
            pt.register_id(slug, str(uuid.uuid4()))
        ids = pt.get_all_platform_collection_ids()
        assert len(ids) == len(ALL_PLATFORM_COLLECTION_SLUGS)
        # All IDs distinct
        assert len(set(ids)) == len(ids)


class TestPreResolvePlatformIds:
    def test_uses_existing_settings_ids_when_present(self, empty_registry):
        """When platform_settings has persisted slug→UUID mappings,
        pre_resolve picks them up without generating new IDs."""
        existing_id = "existing-persisted-id"

        mock_settings = MagicMock()
        mock_settings.get.return_value = existing_id
        mock_settings.set_many = MagicMock()

        with patch("services.platform_settings_service.settings", mock_settings):
            pt.pre_resolve_platform_ids(MagicMock())

        # All collection slugs picked up the persisted ID.
        for slug in ALL_PLATFORM_COLLECTION_SLUGS:
            assert pt.get_id(slug) == existing_id

        # The authority/host artifact slugs picked up the persisted ID.
        assert pt.get_id(AUTHORITY_ARTIFACT_SLUG) == existing_id

        # No new settings were written since all were found.
        mock_settings.set_many.assert_not_called()

    def test_generates_new_uuids_for_missing(self, empty_registry):
        mock_settings = MagicMock()
        mock_settings.get.return_value = None
        mock_settings.set_many = MagicMock()

        with patch("services.platform_settings_service.settings", mock_settings):
            pt.pre_resolve_platform_ids(MagicMock())

        # Each slug got a generated UUID.
        for slug in ALL_PLATFORM_COLLECTION_SLUGS:
            v = pt.get_id(slug)
            uuid.UUID(v)  # raises if not a valid UUID

        for agent_slug in PLATFORM_AGENT_SLUGS:
            assert pt.get_id_optional(f"{AGENT_ARTIFACT_SLUG_PREFIX}{agent_slug}") is not None

        for llm_slug in PLATFORM_LLM_CONNECTION_SLUGS:
            assert pt.get_id_optional(f"{LLM_CONNECTION_SLUG_PREFIX}{llm_slug}") is not None

        for srv_slug in server_registry.all_names():
            assert pt.get_id_optional(f"{SERVER_ARTIFACT_SLUG_PREFIX}{srv_slug}") is not None

        # New IDs were persisted.
        mock_settings.set_many.assert_called_once()

    def test_generates_distinct_uuids_per_slug(self, empty_registry):
        mock_settings = MagicMock()
        mock_settings.get.return_value = None
        mock_settings.set_many = MagicMock()

        with patch("services.platform_settings_service.settings", mock_settings):
            pt.pre_resolve_platform_ids(MagicMock())

        # Distinct UUIDs across all categories.
        all_ids = list(pt._registry.values())
        assert len(set(all_ids)) == len(all_ids), "Slug UUIDs must be unique"
