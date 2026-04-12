import uuid
from unittest.mock import MagicMock, call, patch

from services import seed_content_service as svc
from services.platform_topology import clear_registry, register_id
from services.bootstrap_types import (
    INBOX_SEEDS_COLLECTION_SLUG,
    START_HERE_COLLECTION_SLUG,
    INBOX_MATERIALIZATION_SLUGS,
    AGENTS_COLLECTION_SLUG,
    USER_READABLE_SEED_SLUGS,
    ALL_PLATFORM_COLLECTION_SLUGS,
)


def test_inbox_materialization_slugs_includes_required_collections():
    """INBOX_MATERIALIZATION_SLUGS should only include inbox-seeds (curated collection artifacts).

    Start Here individual docs should NOT be materialized directly into the inbox.
    Users navigate into the Start Here collection to see onboarding docs.
    """
    assert INBOX_SEEDS_COLLECTION_SLUG in INBOX_MATERIALIZATION_SLUGS
    assert START_HERE_COLLECTION_SLUG not in INBOX_MATERIALIZATION_SLUGS


def test_agents_collection_slug_in_fixture_lists():
    """AGENTS_COLLECTION_SLUG must be in readable and all-platform lists."""
    assert AGENTS_COLLECTION_SLUG in USER_READABLE_SEED_SLUGS
    assert AGENTS_COLLECTION_SLUG in ALL_PLATFORM_COLLECTION_SLUGS


def _setup_registry_for_seeds(slug_a="seed-a", slug_b="seed-b"):
    """Pre-register test seed collection slugs with UUIDs."""
    clear_registry()
    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())
    register_id(slug_a, id_a)
    register_id(slug_b, id_b)
    return id_a, id_b


@patch("services.servers_content_service.grant_servers_collection_to_user")
@patch("services.seed_content_service.grant_host_collection_to_user")
@patch("services.seed_content_service.grant_authority_collection_to_user")
@patch("services.seed_content_service.grant_resources_collection_to_user")
@patch("services.seed_content_service.db_list_collection_artifacts")
@patch("services.workspace_service.add_artifact_to_workspace")
@patch("services.seed_content_service.db_upsert_user_collection_grant")
@patch("services.seed_content_service.db_get_collection_by_id")
@patch("services.llm_connections_content_service.grant_llm_connections_to_user")
@patch("services.platform_settings_service.settings")
def test_apply_inbox_seeds_to_user_grants_seed_authority_and_host_access(
    mock_platform_settings,
    mock_grant_llm_connections,
    mock_get_by_slug,
    mock_upsert,
    mock_add_to_workspace,
    mock_get_artifacts,
    mock_grant_resources,
    mock_grant_authority,
    mock_grant_host,
    mock_grant_servers,
):
    id_a, id_b = _setup_registry_for_seeds()

    # No operator configured — host grant is read-only
    mock_platform_settings.get.return_value = None

    mock_upsert.return_value = (MagicMock(), True)

    arango_db = MagicMock()
    seed_artifact = MagicMock(root_id="root-seed-1")
    duplicate_seed_artifact = MagicMock(root_id="root-seed-1")
    mock_get_artifacts.side_effect = [
        [seed_artifact],
        [duplicate_seed_artifact],
    ]
    # _resolve_seed_collection_id will use get_id_optional from registry
    mock_get_by_slug.return_value = None  # registry has them already

    with patch.object(svc.config, "SEED_COLLECTION_SLUGS", ["seed-a", "seed-b"]):
        svc.apply_inbox_seeds_to_user(arango_db, user_id="user-123")

    assert mock_upsert.call_count == 2
    mock_upsert.assert_has_calls(
        [
            call(
                arango_db,
                user_id="user-123",
                collection_id=id_a,
                granted_by=svc.AGIENCE_PLATFORM_USER_ID,
                can_read=True,
                can_update=False,
                name="Custom seed collection (auto-granted on first login)",
            ),
            call(
                arango_db,
                user_id="user-123",
                collection_id=id_b,
                granted_by=svc.AGIENCE_PLATFORM_USER_ID,
                can_read=True,
                can_update=False,
                name="Custom seed collection (auto-granted on first login)",
            ),
        ]
    )
    # Custom seed collections are granted but NOT materialized into the inbox.
    # Only INBOX_MATERIALIZATION_SLUGS (inbox-seeds curated artifacts) trigger
    # artifact fetch, and those slugs aren't registered in this test.
    mock_get_artifacts.assert_not_called()
    mock_add_to_workspace.assert_not_called()
    mock_grant_resources.assert_called_once_with(arango_db, "user-123")
    mock_grant_authority.assert_called_once_with(arango_db, "user-123")
    mock_grant_host.assert_called_once_with(arango_db, "user-123", can_update=False)




# ---------------------------------------------------------------------------
# Inbox materialization regression tests
# ---------------------------------------------------------------------------


@patch("services.servers_content_service.grant_servers_collection_to_user")
@patch("services.seed_content_service.grant_host_collection_to_user")
@patch("services.seed_content_service.grant_authority_collection_to_user")
@patch("services.seed_content_service.grant_resources_collection_to_user")
@patch("services.workspace_service.add_artifact_to_workspace")
@patch("services.seed_content_service.db_list_collection_artifacts")
@patch("services.seed_content_service.db_upsert_user_collection_grant")
@patch("services.seed_content_service.db_get_collection_by_id")
@patch("services.llm_connections_content_service.grant_llm_connections_to_user")
@patch("services.platform_settings_service.settings")
def test_seed_inbox_materializes_artifacts_from_dict_results(
    mock_platform_settings,
    mock_grant_llm,
    mock_get_by_slug,
    mock_upsert,
    mock_list_artifacts,
    mock_add_to_workspace,
    mock_grant_resources,
    mock_grant_authority,
    mock_grant_host,
    mock_grant_servers,
):
    """Regression: db_list_collection_artifacts returns dicts, not objects.

    The bug was using getattr(artifact, "root_id", "") on a dict, which always
    returned "" and silently skipped every seed artifact.
    """
    clear_registry()

    # Register all materialization slugs + readable slugs so the code can
    # resolve them.
    materialization_ids = {}
    for slug in INBOX_MATERIALIZATION_SLUGS:
        cid = str(uuid.uuid4())
        register_id(slug, cid)
        materialization_ids[slug] = cid
    from services.bootstrap_types import USER_READABLE_SEED_SLUGS
    for slug in USER_READABLE_SEED_SLUGS:
        if slug not in materialization_ids:
            register_id(slug, str(uuid.uuid4()))

    mock_platform_settings.get.return_value = None
    mock_upsert.return_value = (MagicMock(), True)

    # Simulate db_list_collection_artifacts returning plain dicts (the real return type).
    dict_artifact_a = {"root_id": "root-aaa", "id": "ver-1", "content": "hello"}
    dict_artifact_b = {"root_id": "root-bbb", "id": "ver-2", "content": "world"}
    mock_list_artifacts.return_value = [dict_artifact_a, dict_artifact_b]

    arango_db = MagicMock()
    with patch.object(svc.config, "SEED_COLLECTION_SLUGS", []):
        svc.apply_inbox_seeds_to_user(arango_db, user_id="user-456")

    # Both dict artifacts should have been materialized into the inbox workspace.
    assert mock_add_to_workspace.call_count >= 2
    materialized_root_ids = [
        c.kwargs.get("root_id") or c[0][4]  # positional arg index 4
        for c in mock_add_to_workspace.call_args_list
    ]
    assert "root-aaa" in materialized_root_ids
    assert "root-bbb" in materialized_root_ids


@patch("services.servers_content_service.grant_servers_collection_to_user")
@patch("services.seed_content_service.grant_host_collection_to_user")
@patch("services.seed_content_service.grant_authority_collection_to_user")
@patch("services.seed_content_service.grant_resources_collection_to_user")
@patch("services.workspace_service.add_artifact_to_workspace")
@patch("services.seed_content_service.db_list_collection_artifacts")
@patch("services.seed_content_service.db_upsert_user_collection_grant")
@patch("services.seed_content_service.db_get_collection_by_id")
@patch("services.llm_connections_content_service.grant_llm_connections_to_user")
@patch("services.platform_settings_service.settings")
def test_seed_inbox_deduplicates_across_collections(
    mock_platform_settings,
    mock_grant_llm,
    mock_get_by_slug,
    mock_upsert,
    mock_list_artifacts,
    mock_add_to_workspace,
    mock_grant_resources,
    mock_grant_authority,
    mock_grant_host,
    mock_grant_servers,
):
    """Artifacts with the same root_id across materialization collections are deduplicated."""
    clear_registry()

    for slug in INBOX_MATERIALIZATION_SLUGS:
        register_id(slug, str(uuid.uuid4()))
    from services.bootstrap_types import USER_READABLE_SEED_SLUGS
    for slug in USER_READABLE_SEED_SLUGS:
        if not svc.get_id_optional(slug):
            register_id(slug, str(uuid.uuid4()))

    mock_platform_settings.get.return_value = None
    mock_upsert.return_value = (MagicMock(), True)

    # Same root_id appears in multiple collections
    shared_root = {"root_id": "root-shared", "id": "ver-x", "content": "dup"}
    mock_list_artifacts.return_value = [shared_root]

    arango_db = MagicMock()
    with patch.object(svc.config, "SEED_COLLECTION_SLUGS", []):
        svc.apply_inbox_seeds_to_user(arango_db, user_id="user-789")

    # root-shared should appear exactly once despite being in multiple collections
    materialized_root_ids = [
        c[0][4] for c in mock_add_to_workspace.call_args_list
    ]
    assert materialized_root_ids.count("root-shared") == 1
