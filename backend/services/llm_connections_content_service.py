"""
LLM Connections content service -- platform-default LLM connection artifacts.

Creates the agience-llm-connections collection at startup and populates it
with default connection artifacts for platform-supported LLM providers.

Follows the same pattern as resources_content_service.py:
  - ensure_llm_connections_collection() called at startup from main.py
  - grant_llm_connections_to_user() called on first login from seed_content_service.py

The content type string appears here because this is platform bootstrap (same precedent
as AGENT_CONTENT_TYPE in resources_content_service.py). The type handler lives on Verso.
"""

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
    LLM_CONNECTIONS_COLLECTION_SLUG,
    LLM_CONNECTION_CONTENT_TYPE,
    LLM_CONNECTION_SLUG_PREFIX,
)
from services.platform_topology import get_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default LLM connection definitions
# ---------------------------------------------------------------------------

_DEFAULT_CONNECTIONS = [
    {
        "slug_suffix": "anthropic-sonnet",
        "title": "Claude Sonnet 4",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "tier": "free",
        "description": "Fast, intelligent model for everyday tasks. Included in free tier.",
        "rate_limits": {"requests_per_minute": 10, "tokens_per_minute": 10000, "tokens_per_day": 100000},
        "capabilities": {"chat": True, "reasoning": True, "vision": True, "function_calling": True, "max_context_tokens": 200000},
    },
    {
        "slug_suffix": "anthropic-opus",
        "title": "Claude Opus 4",
        "provider": "anthropic",
        "model": "claude-opus-4-20250514",
        "tier": "pro",
        "description": "Most capable reasoning model. Pro tier.",
        "rate_limits": {"requests_per_minute": 60, "tokens_per_minute": 100000, "tokens_per_day": 2000000},
        "capabilities": {"chat": True, "reasoning": True, "vision": True, "function_calling": True, "max_context_tokens": 200000},
    },
    {
        "slug_suffix": "openai-gpt4o",
        "title": "GPT-4o",
        "provider": "openai",
        "model": "gpt-4o",
        "tier": "free",
        "description": "Fast multimodal model. Included in free tier.",
        "rate_limits": {"requests_per_minute": 10, "tokens_per_minute": 10000, "tokens_per_day": 100000},
        "capabilities": {"chat": True, "reasoning": False, "vision": True, "function_calling": True, "max_context_tokens": 128000},
    },
    {
        "slug_suffix": "openai-gpt5-nano",
        "title": "GPT-5 Nano",
        "provider": "openai",
        "model": "gpt-5-nano",
        "tier": "pro",
        "description": "Efficient next-gen reasoning model. Pro tier.",
        "rate_limits": {"requests_per_minute": 60, "tokens_per_minute": 100000, "tokens_per_day": 2000000},
        "capabilities": {"chat": True, "reasoning": True, "vision": True, "function_calling": True, "max_context_tokens": 256000},
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_llm_connections_collection(arango_db: StandardDatabase) -> Optional[str]:
    """Ensure the LLM connections collection exists with default connection artifacts.

    Called from main.py at startup. Idempotent.
    """
    collection_id = _ensure_collection(arango_db)
    if not collection_id:
        return None

    for conn in _DEFAULT_CONNECTIONS:
        slug = f"{LLM_CONNECTION_SLUG_PREFIX}{conn['slug_suffix']}"
        root_id = get_id(slug)
        _ensure_artifact_linked(
            arango_db,
            collection_id=collection_id,
            root_id=root_id,
            context=_build_connection_context(conn),
            content=_build_connection_content(conn),
            content_type=LLM_CONNECTION_CONTENT_TYPE,
        )

    return collection_id


def grant_llm_connections_to_user(arango_db: StandardDatabase, user_id: str) -> None:
    """Grant a user read access to the LLM connections collection. Idempotent."""
    if not user_id:
        logger.warning("grant_llm_connections_to_user called with empty user_id — skipping")
        return

    collection_id = get_id(LLM_CONNECTIONS_COLLECTION_SLUG)
    try:
        _grant, changed = db_upsert_user_collection_grant(
            arango_db,
            user_id=user_id,
            collection_id=collection_id,
            granted_by=AGIENCE_PLATFORM_USER_ID,
            can_read=True,
            can_update=False,
            name="Platform LLM connections (auto-granted on first login)",
        )
        if changed:
            logger.info("Granted user %s read access to LLM connections collection %s", user_id, collection_id)
    except Exception:
        logger.exception(
            "Failed to grant user %s read access to LLM connections collection %s",
            user_id,
            collection_id,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_collection(arango_db: StandardDatabase) -> Optional[str]:
    """Ensure the ArangoDB collection document exists."""
    col_id = get_id(LLM_CONNECTIONS_COLLECTION_SLUG)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        now = datetime.now(timezone.utc).isoformat()
        from entities.collection import COLLECTION_CONTENT_TYPE
        collection = CollectionEntity(
            id=col_id,
            name="LLM Connections",
            description=(
                "Platform-provided LLM connection artifacts. Includes default "
                "connections to current models (Claude, GPT) included with "
                "subscription tiers. Users can add their own connections."
            ),
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, collection)
        logger.info("Created LLM connections collection (id=%s)", col_id)
        return col_id
    except Exception:
        logger.exception("Failed to create LLM connections collection (id=%s)", col_id)
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
    """Ensure an artifact exists and is linked to the collection."""
    linked = db_get_artifact_by_collection_and_root(arango_db, collection_id, root_id)
    if linked:
        return True

    existing_version = db_get_artifact(arango_db, root_id)
    if existing_version:
        try:
            db_add_artifact_to_collection(arango_db, collection_id, root_id, existing_version.id)
            return True
        except Exception:
            logger.exception("Failed linking existing LLM connection artifact root %s", root_id)
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
        db_add_artifact_to_collection(arango_db, collection_id, root_id, artifact.id)
        return True
    except Exception:
        logger.exception("Failed to create LLM connection artifact root %s", root_id)
        return False


def _build_connection_context(conn: dict) -> str:
    """Build the context JSON for a default LLM connection artifact."""
    context = {
        "content_type": LLM_CONNECTION_CONTENT_TYPE,
        "title": conn["title"],
        "description": conn["description"],
        "provider": conn["provider"],
        "model": conn["model"],
        "tier": conn["tier"],
        "rate_limits": conn["rate_limits"],
        "capabilities": conn["capabilities"],
        "credentials_ref": {
            "secret_type": "llm_key",
            "provider": conn["provider"],
            "resolution": "platform_default",
        },
        "is_platform_default": True,
    }
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)


def _build_connection_content(conn: dict) -> str:
    """Build the content text for a default LLM connection artifact."""
    return (
        f"{conn['title']} — {conn['description']} "
        f"Provider: {conn['provider']}, Model: {conn['model']}, Tier: {conn['tier']}."
    )
