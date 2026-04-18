import json
import logging
from datetime import datetime, timezone
from typing import Optional

from arango.database import StandardDatabase

from core.config import AGIENCE_PLATFORM_USER_ID
from db.arango import (
    create_artifact as db_create_artifact,
    create_collection as db_create_collection,
    add_artifact_to_collection as db_add_artifact_to_collection,
    get_artifact as db_get_artifact,
    get_current_in_collection as db_get_artifact_by_collection_and_root,
    get_collection_by_id as db_get_collection_by_id,
    upsert_user_collection_grant as db_upsert_user_collection_grant,
)
from entities.collection import Collection as CollectionEntity
from entities.artifact import Artifact as ArtifactEntity
from services.bootstrap_types import (
    AUTHORITY_ARTIFACT_SLUG,
    AUTHORITY_COLLECTION_SLUG,
    HOST_ARTIFACT_SLUG,
    HOST_COLLECTION_SLUG,
    HOST_CONTENT_TYPE,
)
from services.collection_service import ensure_collection_descriptor  # noqa: F401
from services.platform_topology import get_id

logger = logging.getLogger(__name__)


def ensure_current_instance_host(arango_db: StandardDatabase) -> Optional[str]:
    """
    Ensure the platform-owned host collection exists and contains the current-instance
    Host artifact. Idempotent.

    Returns the host collection ID, or None on failure.
    """
    collection_id = _ensure_host_collection(arango_db)
    if not collection_id:
        return None

    host_root_id = get_id(HOST_ARTIFACT_SLUG)

    linked = db_get_artifact_by_collection_and_root(
        arango_db,
        collection_id,
        host_root_id,
    )
    if linked:
        return collection_id

    existing_version = db_get_artifact(arango_db, host_root_id)
    if existing_version:
        try:
            db_add_artifact_to_collection(
                arango_db,
                collection_id,
                host_root_id,
                existing_version.id,
            )
            logger.info(
                "Linked existing current-instance host artifact into host collection (collection=%s, version=%s)",
                collection_id,
                existing_version.id,
            )
            return collection_id
        except Exception:
            logger.exception("Failed linking existing current-instance host artifact into host collection")
            return None

    try:
        now = datetime.now(timezone.utc).isoformat()
        artifact = ArtifactEntity(
            id=host_root_id,
            root_id=host_root_id,
            collection_id=collection_id,
            state=ArtifactEntity.STATE_COMMITTED,
            context=_build_current_instance_host_context(),
            content=(
                "Integrated host for this Agience Core deployment. "
                "Runs all first-party MCP servers in-process. Operates under the deployment authority."
            ),
            content_type=HOST_CONTENT_TYPE,
            created_by=AGIENCE_PLATFORM_USER_ID,
            created_time=now,
        )
        db_create_artifact(arango_db, artifact)
        db_add_artifact_to_collection(
            arango_db,
            collection_id,
            host_root_id,
            artifact.id,
        )
        logger.info("Created current-instance host artifact (collection=%s, version=%s)", collection_id, artifact.id)
        return collection_id
    except Exception:
        logger.exception("Failed to create current-instance host artifact")
        return None


def grant_host_collection_to_user(arango_db: StandardDatabase, user_id: str, can_update: bool = False) -> None:
    """Grant a user access to the platform host collection. Idempotent.

    By default grants read-only access. Pass ``can_update=True`` to give the
    platform operator write access so they can update (rename) host artifacts.
    """
    if not user_id:
        logger.warning("grant_host_collection_to_user called with empty user_id - skipping")
        return

    collection_id = get_id(HOST_COLLECTION_SLUG)
    try:
        _grant, changed = db_upsert_user_collection_grant(
            arango_db,
            user_id=user_id,
            collection_id=collection_id,
            granted_by=AGIENCE_PLATFORM_USER_ID,
            can_read=True,
            can_update=can_update,
            name="Platform host collection (auto-granted on first login)",
        )
        if changed:
            logger.info("Granted user %s read access to host collection %s", user_id, collection_id)
    except Exception:
        logger.exception(
            "Failed to grant user %s read access to host collection %s",
            user_id,
            collection_id,
        )


def _ensure_host_collection(arango_db: StandardDatabase) -> Optional[str]:
    col_id = get_id(HOST_COLLECTION_SLUG)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        now = datetime.now(timezone.utc).isoformat()
        from entities.collection import COLLECTION_CONTENT_TYPE
        collection = CollectionEntity(
            id=col_id,
            name="Agience Hosts",
            description=(
                "Platform-owned host registry for this Agience instance. "
                "Users receive read access automatically; operators manage host-backed server topology here."
            ),
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, collection)
        logger.info("Created host collection (id=%s)", col_id)
        return col_id
    except Exception:
        logger.exception("Failed to create host collection (id=%s)", col_id)
        return None


def _build_current_instance_host_context() -> str:
    authority_root_id = get_id(AUTHORITY_ARTIFACT_SLUG)
    authority_collection_id = get_id(AUTHORITY_COLLECTION_SLUG)
    context = {
        "type": "host",
        "content_type": HOST_CONTENT_TYPE,
        "title": "Agience Core",
        "platform": "integrated",
        "authority": {
            "artifact_id": authority_root_id,
            "collection_id": authority_collection_id,
        },
        "servers": ["aria", "astra", "atlas", "sage", "nexus", "ophan", "seraph", "verso"],
        "host": {
            "scope": "current-instance",
            "install": {
                "owner_repo": True,
            },
        },
    }
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)
