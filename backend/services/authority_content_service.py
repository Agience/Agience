import json
import logging
from datetime import datetime, timezone
from typing import Optional

from arango.database import StandardDatabase

from core.config import AGIENCE_PLATFORM_USER_ID
from core import config
from db.arango import (
    add_artifact_to_collection as db_add_artifact_to_collection,
    create_artifact as db_create_artifact,
    create_collection as db_create_collection,
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
    AUTHORITY_CONTENT_TYPE,
    HOST_ARTIFACT_SLUG,
)
from services.collection_service import ensure_collection_descriptor  # noqa: F401
from services.platform_topology import get_id

logger = logging.getLogger(__name__)


def ensure_current_instance_authority(arango_db: StandardDatabase) -> Optional[str]:
    """
    Ensure the platform-owned authority collection exists and contains the
    current-instance Authority artifact. Idempotent.

    Returns the authority collection ID, or None on failure.
    """
    collection_id = _ensure_authority_collection(arango_db)
    if not collection_id:
        return None

    authority_root_id = get_id(AUTHORITY_ARTIFACT_SLUG)

    linked = db_get_artifact_by_collection_and_root(
        arango_db,
        collection_id,
        authority_root_id,
    )
    if linked:
        return collection_id

    existing_version = db_get_artifact(arango_db, authority_root_id)
    if existing_version:
        try:
            db_add_artifact_to_collection(
                arango_db,
                collection_id,
                authority_root_id,
                existing_version.id,
            )
            logger.info(
                "Linked existing current-instance authority artifact into authority collection (collection=%s, version=%s)",
                collection_id,
                existing_version.id,
            )
            return collection_id
        except Exception:
            logger.exception("Failed linking existing current-instance authority artifact into authority collection")
            return None

    try:
        now = datetime.now(timezone.utc).isoformat()
        artifact = ArtifactEntity(
            id=authority_root_id,
            root_id=authority_root_id,
            collection_id=collection_id,
            state=ArtifactEntity.STATE_COMMITTED,
            context=_build_current_instance_authority_context(),
            content=(
                "Platform-native authority artifact for the current Agience deployment. "
                "This authority defines the domain and trust surface above the current instance host."
            ),
            content_type=AUTHORITY_CONTENT_TYPE,
            created_by=AGIENCE_PLATFORM_USER_ID,
            created_time=now,
        )
        db_create_artifact(arango_db, artifact)
        db_add_artifact_to_collection(
            arango_db,
            collection_id,
            authority_root_id,
            artifact.id,
        )
        logger.info("Created current-instance authority artifact (collection=%s, version=%s)", collection_id, artifact.id)
        return collection_id
    except Exception:
        logger.exception("Failed to create current-instance authority artifact")
        return None


def grant_authority_collection_to_user(arango_db: StandardDatabase, user_id: str) -> None:
    """Grant a user read access to the platform authority collection. Idempotent."""
    if not user_id:
        logger.warning("grant_authority_collection_to_user called with empty user_id - skipping")
        return

    collection_id = get_id(AUTHORITY_COLLECTION_SLUG)
    try:
        _grant, changed = db_upsert_user_collection_grant(
            arango_db,
            user_id=user_id,
            collection_id=collection_id,
            granted_by=AGIENCE_PLATFORM_USER_ID,
            can_read=True,
            can_update=False,
            name="Platform authority collection (auto-granted on first login)",
        )
        if changed:
            logger.info("Granted user %s read access to authority collection %s", user_id, collection_id)
    except Exception:
        logger.exception(
            "Failed to grant user %s read access to authority collection %s",
            user_id,
            collection_id,
        )


def _ensure_authority_collection(arango_db: StandardDatabase) -> Optional[str]:
    col_id = get_id(AUTHORITY_COLLECTION_SLUG)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        now = datetime.now(timezone.utc).isoformat()
        from entities.collection import COLLECTION_CONTENT_TYPE
        collection = CollectionEntity(
            id=col_id,
            name="Agience Authorities",
            description=(
                "Platform-owned authority registry for this Agience deployment. "
                "Users receive read access automatically; operators manage authority and trust topology here."
            ),
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, collection)
        logger.info("Created authority collection (id=%s)", col_id)
        return col_id
    except Exception:
        logger.exception("Failed to create authority collection (id=%s)", col_id)
        return None


def _derive_authority_domain() -> str:
    return config.AUTHORITY_DOMAIN


def _build_current_instance_authority_context() -> str:
    host_root_id = get_id(HOST_ARTIFACT_SLUG)
    context = {
        "type": "authority",
        "content_type": AUTHORITY_CONTENT_TYPE,
        "title": "Current Agience Authority",
        "authority": {
            "domain": config.AUTHORITY_DOMAIN,
            "issuer": config.AUTHORITY_ISSUER,
            "trust_model": "platform-default",
            "current_host_artifact_id": host_root_id,
            "host_artifact_ids": [host_root_id],
            "frontend_uri": config.FRONTEND_URI,
            "backend_uri": config.BACKEND_URI,
        },
    }
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)
