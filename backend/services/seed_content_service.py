"""
Seed content service -- first-login platform collection grants and inbox seeding.

Design:
- The Agience Inbox Seeds collection (agience-inbox-seeds) is admin-only.  It is not
  visible to standard users.
- On first login every new user is granted READ access to the platform-managed
  sub-collections: Start Here, Platform Artifacts, Agience Servers, Agience Tools,
  and Agience Agents (defined in bootstrap_types.USER_READABLE_SEED_SLUGS).
- Curated collection artifacts from Inbox Seeds are materialized into the user's
    Inbox workspace for navigation. Individual Start Here docs stay in their collection
  and are NOT flattened into the inbox.
- config.SEED_COLLECTION_SLUGS (from config) remains for operator-defined custom seed
  collections; its default is now empty so no custom seeds are created unless
  explicitly configured.
- All operations are idempotent.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from arango.database import StandardDatabase

from core.config import AGIENCE_PLATFORM_USER_ID
from core import config
from db.arango import (
    get_collection_by_id as db_get_collection_by_id,
    get_edge as db_get_edge,
    list_collection_artifacts as db_list_collection_artifacts,
    upsert_user_collection_grant as db_upsert_user_collection_grant,
)
from services.authority_content_service import grant_authority_collection_to_user
from services.host_content_service import grant_host_collection_to_user
from services.resources_content_service import grant_resources_collection_to_user
from services.bootstrap_types import (
    INBOX_SEEDS_COLLECTION_SLUG,
    USER_READABLE_SEED_SLUGS,
    INBOX_MATERIALIZATION_SLUGS,
)
from services.platform_topology import get_id, get_id_optional

logger = logging.getLogger(__name__)

# Primary stable slug -- kept as a named export so manage_seed.py and tests
# can reference it without importing the full list.
INBOX_SEED_COLLECTION_SLUG = INBOX_SEEDS_COLLECTION_SLUG


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def ensure_inbox_seed_collection(arango_db: StandardDatabase) -> Optional[str]:
    """
    Ensure the primary platform seed collection exists (created empty if missing).
    Kept as a named function so manage_seed.py can call it directly.
    Returns the collection ID, or None on failure.
    """
    return _ensure_seed_collection(
        arango_db,
        col_slug=INBOX_SEED_COLLECTION_SLUG,
        name="Agience Inbox Seeds",
    )


def ensure_all_seed_collections(arango_db: StandardDatabase) -> List[str]:
    """
    Ensure every collection in config.SEED_COLLECTION_SLUGS exists (idempotent).
    Called from main.py at startup. Returns a list of ready collection IDs.
    """
    ready: List[str] = []
    for col_slug in config.SEED_COLLECTION_SLUGS:
        name = col_slug.replace("-", " ").title()
        result = _ensure_seed_collection(arango_db, col_slug=col_slug, name=name)
        if result:
            ready.append(result)
    return ready


def _ensure_seed_collection(
    arango_db: StandardDatabase,
    *,
    col_slug: str,
    name: str,
) -> Optional[str]:
    """Create the collection if it doesn't exist. Idempotent."""
    col_id = get_id(col_slug)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        from entities.collection import Collection as CollectionEntity, COLLECTION_CONTENT_TYPE
        from db.arango import create_collection as db_create_collection

        now = datetime.now(timezone.utc).isoformat()
        col_entity = CollectionEntity(
            id=col_id,
            name=name,
            description="Platform-curated collection. Grants are applied automatically on first login.",
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            slug=col_slug,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, col_entity)
        from services.collection_service import ensure_collection_descriptor
        ensure_collection_descriptor(arango_db, col_entity)
        logger.info("Created seed collection (id=%s, slug=%s)", col_id, col_slug)
        return col_id
    except Exception:
        logger.exception("Failed to create seed collection (slug=%s)", col_slug)
        return None


# ---------------------------------------------------------------------------
# First-login provisioning
# ---------------------------------------------------------------------------


def apply_inbox_seeds_to_user(
    arango_db: StandardDatabase,
    user_id: str = "",
) -> None:
    """
    Grant the user read access to platform-managed seed sub-collections on
    first login, and materialize curated Inbox Seeds artifacts into their
    Inbox workspace.

    The Agience Inbox Seeds parent collection is granted READ here so users can read
    the curated artifacts that are materialized into their Inbox workspace.
    Any operator-configured custom seed collections (config.SEED_COLLECTION_SLUGS) are also granted.

    Idempotent -- upsert semantics mean re-running is a no-op if grants already exist.
    """
    if not user_id:
        logger.warning("apply_inbox_seeds_to_user called with empty user_id -- skipping")
        return

    # Grant READ to platform-managed seed sub-collections.
    for col_slug in USER_READABLE_SEED_SLUGS:
        col_id = _resolve_seed_collection_id(arango_db, col_slug)
        if not col_id:
            continue
        try:
            _grant, changed = db_upsert_user_collection_grant(
                arango_db,
                user_id=user_id,
                collection_id=col_id,
                granted_by=AGIENCE_PLATFORM_USER_ID,
                can_read=True,
                can_update=False,
                name="Platform seed collection (auto-granted on first login)",
            )
            if changed:
                logger.info(
                    "Granted user %s read access to platform seed collection %s", user_id, col_id
                )
        except Exception:
            logger.exception(
                "Failed to grant user %s read access to platform seed collection %s",
                user_id,
                col_slug,
            )

    # Also grant any operator-configured custom seed collections (empty by default).
    for col_slug in config.SEED_COLLECTION_SLUGS:
        if col_slug in USER_READABLE_SEED_SLUGS:
            continue  # already granted above
        col_id = _resolve_seed_collection_id(arango_db, col_slug)
        if not col_id:
            continue
        try:
            _grant, changed = db_upsert_user_collection_grant(
                arango_db,
                user_id=user_id,
                collection_id=col_id,
                granted_by=AGIENCE_PLATFORM_USER_ID,
                can_read=True,
                can_update=False,
                name="Custom seed collection (auto-granted on first login)",
            )
            if changed:
                logger.info(
                    "Granted user %s read access to custom seed collection %s", user_id, col_id
                )
        except Exception:
            logger.exception(
                "Failed to grant user %s read access to custom seed collection %s",
                user_id,
                col_slug,
            )

    _seed_inbox_workspace_from_platform_collections(arango_db, user_id)

    grant_authority_collection_to_user(arango_db, user_id)

    # Platform operator gets write access to the host collection so they can rename/
    # configure host artifacts (e.g. the integrated host). All other users get read-only.
    from services.platform_settings_service import settings as _platform_settings
    _operator_id = _platform_settings.get("platform.operator_id")
    _is_operator = bool(_operator_id and user_id == _operator_id)
    grant_host_collection_to_user(arango_db, user_id, can_update=_is_operator)

    grant_resources_collection_to_user(arango_db, user_id)

    from services.llm_connections_content_service import grant_llm_connections_to_user
    grant_llm_connections_to_user(arango_db, user_id)

    from services.servers_content_service import grant_servers_collection_to_user
    grant_servers_collection_to_user(arango_db, user_id)


def apply_platform_collections_to_user(
    arango_db: StandardDatabase,
    user_id: str = "",
) -> None:
    """
    Grant access to platform-owned collections on first login / migration.

    Currently includes seed collections and the platform host collection.
    """
    apply_inbox_seeds_to_user(arango_db, user_id)


def _resolve_seed_collection_id(arango_db: StandardDatabase, col_slug: str) -> Optional[str]:
    """Resolve a seed collection slug to its UUID from the registry."""
    resolved = get_id_optional(col_slug)
    if resolved:
        return resolved
    logger.warning("Seed collection slug '%s' not registered in platform topology", col_slug)
    return None


def _seed_inbox_workspace_from_platform_collections(
    arango_db: StandardDatabase,
    user_id: str,
) -> None:
    """Import curated seed artifacts into the user's Inbox workspace."""
    if not user_id:
        return

    try:
        from services import workspace_service
    except Exception:
        logger.exception("Failed loading workspace_service for inbox seed import")
        return

    seen_root_ids: set[str] = set()
    # Inbox workspace id == user_id (intentional convention)
    inbox_workspace_id = user_id

    # Materialize only curated artifacts from Inbox Seeds.
    # so users can navigate seed collections without flattening all child
    # artifacts into every inbox workspace.
    for col_slug in INBOX_MATERIALIZATION_SLUGS:
        col_id = _resolve_seed_collection_id(arango_db, col_slug)
        if not col_id:
            continue
        try:
            artifacts = db_list_collection_artifacts(arango_db, col_id)
        except Exception:
            logger.exception("Failed loading seed artifacts from collection %s", col_slug)
            continue

        for artifact in artifacts or []:
            root_id = str((artifact.get("root_id", "") if isinstance(artifact, dict) else getattr(artifact, "root_id", "")) or "").strip()
            if not root_id or root_id in seen_root_ids:
                continue
            seen_root_ids.add(root_id)

            # Skip if already linked — avoids clobbering user-set order_key on
            # every restart (add_artifact_to_collection uses overwrite=True on the
            # edge, which resets the order_key and trashes user reordering).
            existing_edge = db_get_edge(arango_db, inbox_workspace_id, root_id)
            if isinstance(existing_edge, dict) and existing_edge:
                continue

            try:
                workspace_service.add_artifact_to_workspace(
                    arango_db,
                    arango_db,
                    user_id,
                    inbox_workspace_id,
                    root_id,
                    None,
                )
            except Exception:
                logger.exception(
                    "Failed importing seed artifact root %s into inbox workspace %s",
                    root_id,
                    inbox_workspace_id,
                )
