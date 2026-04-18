import json
import logging
from datetime import datetime, timezone
from typing import Optional

from arango.database import StandardDatabase

from core.config import AGIENCE_PLATFORM_USER_ID
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
    AGENCY_ARTIFACT_SLUG,
    AGENCY_CONTENT_TYPE,
    AGENT_ARTIFACT_SLUG_PREFIX,
    AGENT_CONTENT_TYPE,
    AUTHORITY_ARTIFACT_SLUG,
    HOST_ARTIFACT_SLUG,
    RESOURCES_COLLECTION_SLUG,
    SERVER_ARTIFACT_SLUG_PREFIX,
)
from services.platform_topology import get_id

logger = logging.getLogger(__name__)

_PLATFORM_AGENTS = [
    {
        "slug": "aria",
        "title": "Aria",
        "role": "Output and presentation",
        "summary": "Formats and presents final responses for humans.",
    },
    {
        "slug": "astra",
        "title": "Astra",
        "role": "Ingestion and indexing",
        "summary": "Handles ingestion, extraction, and indexing workflows.",
    },
    {
        "slug": "atlas",
        "title": "Atlas",
        "role": "Governance and coherence",
        "summary": "Tracks decisions, constraints, and policy coherence.",
    },
    {
        "slug": "sage",
        "title": "Sage",
        "role": "Research and retrieval",
        "summary": "Performs grounded retrieval and evidence synthesis.",
    },
    {
        "slug": "nexus",
        "title": "Nexus",
        "role": "Routing and communication",
        "summary": "Coordinates routing and communication planes.",
    },
    {
        "slug": "ophan",
        "title": "Ophan",
        "role": "Finance and licensing",
        "summary": "Supports financial and licensing workflows.",
    },
    {
        "slug": "seraph",
        "title": "Seraph",
        "role": "Security and trust",
        "summary": "Enforces trust, guardrails, and security policies.",
    },
    {
        "slug": "verso",
        "title": "Verso",
        "role": "Reasoning and transforms",
        "summary": "Drives synthesis and transform-style reasoning flows.",
    },
]


def ensure_platform_resources(arango_db: StandardDatabase) -> Optional[str]:
    """Ensure the platform resources collection with Agency/Agent artifacts exists."""
    collection_id = _ensure_resources_collection(arango_db)
    if not collection_id:
        return None

    agent_root_ids: list[str] = []
    for agent in _PLATFORM_AGENTS:
        artifact_slug = f"{AGENT_ARTIFACT_SLUG_PREFIX}{agent['slug']}"
        root_id = get_id(artifact_slug)
        if _ensure_artifact_linked(
            arango_db,
            collection_id=collection_id,
            root_id=root_id,
            context=_build_platform_agent_context(agent),
            content=_build_platform_agent_content(agent),
            content_type=AGENT_CONTENT_TYPE,
        ):
            agent_root_ids.append(root_id)

    agency_root_id = get_id(AGENCY_ARTIFACT_SLUG)
    _ensure_artifact_linked(
        arango_db,
        collection_id=collection_id,
        root_id=agency_root_id,
        context=_build_platform_agency_context(agent_root_ids),
        content=_build_platform_agency_content(),
        content_type=AGENCY_CONTENT_TYPE,
    )
    return collection_id


def grant_resources_collection_to_user(arango_db: StandardDatabase, user_id: str) -> None:
    """Grant a user read access to the platform resources collection. Idempotent."""
    if not user_id:
        logger.warning("grant_resources_collection_to_user called with empty user_id - skipping")
        return

    collection_id = get_id(RESOURCES_COLLECTION_SLUG)
    try:
        _grant, changed = db_upsert_user_collection_grant(
            arango_db,
            user_id=user_id,
            collection_id=collection_id,
            granted_by=AGIENCE_PLATFORM_USER_ID,
            can_read=True,
            can_update=False,
            name="Platform resources collection (auto-granted on first login)",
        )
        if changed:
            logger.info("Granted user %s read access to resources collection %s", user_id, collection_id)
    except Exception:
        logger.exception(
            "Failed to grant user %s read access to resources collection %s",
            user_id,
            collection_id,
        )


def _ensure_resources_collection(arango_db: StandardDatabase) -> Optional[str]:
    col_id = get_id(RESOURCES_COLLECTION_SLUG)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        now = datetime.now(timezone.utc).isoformat()
        from entities.collection import COLLECTION_CONTENT_TYPE
        collection = CollectionEntity(
            id=col_id,
            name="Agience Resources",
            description=(
                "Platform-owned resource catalog. Includes canonical agency and "
                "agent artifacts that can be referenced from transforms and search."
            ),
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, collection)
        logger.info("Created resources collection (id=%s)", col_id)
        return col_id
    except Exception:
        logger.exception("Failed to create resources collection (id=%s)", col_id)
        return None


def _ensure_artifact_linked(
    arango_db: StandardDatabase,
    *,
    collection_id: str,
    root_id: str,
    context: str,
    content: str,
    content_type: Optional[str] = None,
) -> bool:
    linked = db_get_artifact_by_collection_and_root(
        arango_db,
        collection_id,
        root_id,
    )
    if linked:
        return True

    existing_version = db_get_artifact(arango_db, root_id)
    if existing_version:
        try:
            db_add_artifact_to_collection(
                arango_db,
                collection_id,
                root_id,
                existing_version.id,
            )
            return True
        except Exception:
            logger.exception("Failed linking existing platform artifact root %s", root_id)
            return False

    try:
        now = datetime.now(timezone.utc).isoformat()
        artifact = ArtifactEntity(
            id=root_id,
            root_id=root_id,
            collection_id=collection_id,
            state=ArtifactEntity.STATE_COMMITTED,
            context=context,
            content=content,
            content_type=content_type,
            created_by=AGIENCE_PLATFORM_USER_ID,
            created_time=now,
        )
        db_create_artifact(arango_db, artifact)
        db_add_artifact_to_collection(
            arango_db,
            collection_id,
            root_id,
            artifact.id,
        )
        return True
    except Exception:
        logger.exception("Failed to create platform artifact root %s", root_id)
        return False


def _build_platform_agent_context(agent: dict) -> str:
    host_root_id = get_id(HOST_ARTIFACT_SLUG)
    authority_root_id = get_id(AUTHORITY_ARTIFACT_SLUG)
    context = {
        "type": "agent",
        "content_type": AGENT_CONTENT_TYPE,
        "title": agent["title"],
        "agent": {
            "version": 1,
            "kind": "platform-server",
            "role": agent["role"],
            "server_id": get_id(f"{SERVER_ARTIFACT_SLUG_PREFIX}{agent['slug']}"),
            "host_artifact_id": host_root_id,
            "authority_artifact_id": authority_root_id,
        },
    }
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)


def _build_platform_agent_content(agent: dict) -> str:
    return (
        f"{agent['title']} is a platform agent. "
        f"Domain: {agent['role']}. {agent['summary']}"
    )


def _build_platform_agency_context(agent_root_ids: list[str]) -> str:
    # Lead agent is Aria -- resolve by slug
    lead_agent_id = get_id(f"{AGENT_ARTIFACT_SLUG_PREFIX}aria")
    context = {
        "type": "agency",
        "content_type": AGENCY_CONTENT_TYPE,
        "title": "Agience Platform Agency",
        "agency": {
            "version": 1,
            "lead_agent": lead_agent_id,
            "agents": agent_root_ids,
        },
    }
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)


def _build_platform_agency_content() -> str:
    return (
        "Platform-managed agency artifact that groups canonical Agience platform agents. "
        "Users can reference these agents from collections, search, and transforms."
    )
