"""
ID invariant tests -- codifies the platform's identity model rules.

These tests make the following design decisions explicit and regression-proof:

1. Personal collection ID == user UUID (intentional convenience)
2. Inbox workspace ID == user UUID (intentional convenience)
3. All platform bootstrap IDs are UUIDs (not readable strings)
4. Platform topology registry maps slugs to UUIDs
"""

import re
import uuid
from unittest.mock import MagicMock, patch

import pytest

from services.bootstrap_types import (
    ALL_PLATFORM_COLLECTION_SLUGS,
    AUTHORITY_ARTIFACT_SLUG,
    AUTHORITY_COLLECTION_SLUG,
    HOST_ARTIFACT_SLUG,
    HOST_COLLECTION_SLUG,
)
from services.platform_topology import (
    clear_registry,
    get_all_platform_collection_ids,
    get_id,
    register_id,
)

UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Personal collection / inbox workspace: ID == user_id (intentional)
# ---------------------------------------------------------------------------


@patch("db.arango.upsert_user_collection_grant")
@patch("services.collection_service.ensure_collection_descriptor")
@patch("services.collection_service.db_create_collection")
def test_personal_collection_id_equals_user_id(mock_create, _mock_descriptor, _mock_grant):
    """
    Design decision: personal collection ID is set to the user UUID.
    This is intentional -- both the collection and the user share the same UUID.
    """
    from services.collection_service import create_new_collection

    mock_create.side_effect = lambda db, entity: entity
    user_id = str(uuid.uuid4())

    result = create_new_collection(
        db=MagicMock(),
        owner_id=user_id,
        name="Personal",
        description="Test personal collection",
        is_personal=True,
    )

    assert result.id == user_id, (
        "Personal collection ID must equal the user UUID. "
        "This is an intentional design convention, not a bug."
    )


def test_inbox_workspace_id_equals_user_id():
    """
    Design decision: inbox workspace ID is set to the user UUID.
    This is intentional -- both the workspace and the user share the same UUID.
    """
    from services.workspace_service import create_workspace

    mock_db = MagicMock()
    user_id = str(uuid.uuid4())

    with patch("services.workspace_service.arango.create_collection", return_value=None), \
         patch("services.collection_service.ensure_collection_descriptor"), \
         patch("services.workspace_service.arango.upsert_user_collection_grant"):
        result = create_workspace(
            db=mock_db,
            user_id=user_id,
            name="Inbox",
            is_inbox=True,
        )

    assert result.id == user_id, (
        "Inbox workspace ID must equal the user UUID. "
        "This is an intentional design convention, not a bug."
    )


# ---------------------------------------------------------------------------
# Platform topology registry
# ---------------------------------------------------------------------------


def test_get_id_raises_before_registration():
    """Registry must raise RuntimeError for unknown slugs."""
    clear_registry()
    with pytest.raises(RuntimeError, match="not registered"):
        get_id("nonexistent-slug")


def test_register_and_get_id_roundtrip():
    """Register a slug->UUID, then get_id returns the same UUID."""
    clear_registry()
    test_uuid = str(uuid.uuid4())
    register_id("test-slug", test_uuid)
    assert get_id("test-slug") == test_uuid


def test_get_all_platform_collection_ids_returns_uuids():
    """After registration, all platform collection IDs must be valid UUIDs."""
    clear_registry()
    for slug in ALL_PLATFORM_COLLECTION_SLUGS:
        register_id(slug, str(uuid.uuid4()))

    ids = get_all_platform_collection_ids()
    assert len(ids) == len(ALL_PLATFORM_COLLECTION_SLUGS)
    for collection_id in ids:
        assert UUID_REGEX.match(collection_id), (
            f"Platform collection ID '{collection_id}' is not a valid UUID"
        )


# ---------------------------------------------------------------------------
# Platform bootstrap creates UUID IDs with slug metadata
# ---------------------------------------------------------------------------


@patch("services.authority_content_service.ensure_collection_descriptor")
@patch("services.authority_content_service.db_add_artifact_to_collection")
@patch("services.authority_content_service.db_create_artifact")
@patch("services.authority_content_service.db_create_collection")
@patch("services.authority_content_service.db_get_artifact", return_value=None)
@patch("services.authority_content_service.db_get_artifact_by_collection_and_root", return_value=None)
@patch("services.authority_content_service.db_get_collection_by_id", return_value=None)
def test_authority_bootstrap_creates_collection_with_uuid_and_slug(
    _mock_get, _mock_linked, _mock_get_artifact, mock_create_col, mock_create_art, _mock_add, _mock_ensure_descriptor
):
    """Authority collection and artifact should have UUID IDs and root_ids."""
    clear_registry()
    col_uuid = str(uuid.uuid4())
    art_uuid = str(uuid.uuid4())
    host_uuid = str(uuid.uuid4())
    register_id(AUTHORITY_COLLECTION_SLUG, col_uuid)
    register_id(AUTHORITY_ARTIFACT_SLUG, art_uuid)
    register_id(HOST_ARTIFACT_SLUG, host_uuid)

    from services.authority_content_service import ensure_current_instance_authority
    ensure_current_instance_authority(MagicMock())

    # Verify collection was created with UUID id
    col_entity = mock_create_col.call_args[0][1]
    assert UUID_REGEX.match(col_entity.id), "Collection ID must be a UUID"

    # Verify artifact was created with UUID id and UUID root_id
    art_entity = mock_create_art.call_args[0][1]
    assert UUID_REGEX.match(art_entity.id), "Artifact version ID must be a UUID"
    assert UUID_REGEX.match(art_entity.root_id), "Artifact root_id must be a UUID"


@patch("services.host_content_service.ensure_collection_descriptor")
@patch("services.host_content_service.db_add_artifact_to_collection")
@patch("services.host_content_service.db_create_artifact")
@patch("services.host_content_service.db_create_collection")
@patch("services.host_content_service.db_get_artifact", return_value=None)
@patch("services.host_content_service.db_get_artifact_by_collection_and_root", return_value=None)
@patch("services.host_content_service.db_get_collection_by_id", return_value=None)
def test_host_bootstrap_creates_collection_with_uuid_and_slug(
    _mock_get, _mock_linked, _mock_get_artifact, mock_create_col, mock_create_art, _mock_add, _mock_ensure_descriptor
):
    """Host collection and artifact should have UUID IDs and root_ids."""
    clear_registry()
    register_id(HOST_COLLECTION_SLUG, str(uuid.uuid4()))
    register_id(HOST_ARTIFACT_SLUG, str(uuid.uuid4()))
    register_id(AUTHORITY_ARTIFACT_SLUG, str(uuid.uuid4()))
    register_id(AUTHORITY_COLLECTION_SLUG, str(uuid.uuid4()))

    from services.host_content_service import ensure_current_instance_host
    ensure_current_instance_host(MagicMock())

    col_entity = mock_create_col.call_args[0][1]
    assert UUID_REGEX.match(col_entity.id)

    art_entity = mock_create_art.call_args[0][1]
    assert UUID_REGEX.match(art_entity.id)
    assert UUID_REGEX.match(art_entity.root_id)
