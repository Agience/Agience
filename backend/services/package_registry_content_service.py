"""Seed the platform package registry collection.

The registry is just a collection: committed ``vnd.agience.package+json``
artifacts live here and are discoverable via the ordinary search surface.
No separate registry service --- everything reuses collection + grant
+ search infrastructure.

Seeded at startup (empty): the registry collection is created with a
stable slug so publish operations can target it without a config lookup.
Starts empty; populated as users publish packages.

Users get auto-granted READ on first login (via the standard
USER_READABLE_SEED_SLUGS fixture in bootstrap_types). Publishing
requires an explicit grant --- Core does not grant can_create to every
user by default, so the marketplace isn't an open drop zone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from arango.database import StandardDatabase

from core.config import AGIENCE_PLATFORM_USER_ID
from db.arango import (
    create_collection as db_create_collection,
    get_collection_by_id as db_get_collection_by_id,
)
from entities.collection import Collection as CollectionEntity, COLLECTION_CONTENT_TYPE
from services.bootstrap_types import PACKAGE_REGISTRY_COLLECTION_SLUG
from services.platform_topology import get_id

logger = logging.getLogger(__name__)


def ensure_package_registry(arango_db: StandardDatabase) -> Optional[str]:
    """Ensure the package registry collection exists. Idempotent.

    Returns the collection ID, or None on failure.
    """
    col_id = get_id(PACKAGE_REGISTRY_COLLECTION_SLUG)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        now = datetime.now(timezone.utc).isoformat()
        collection = CollectionEntity(
            id=col_id,
            name="Agience Package Registry",
            description=(
                "Published package manifests. Users browse this collection "
                "to discover installable packages; publish by committing a "
                "vnd.agience.package+json artifact here."
            ),
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, collection)
        logger.info(
            "Created package registry collection (id=%s)", col_id,
        )
        return col_id
    except Exception:
        logger.exception(
            "Failed to create package registry collection (id=%s)", col_id,
        )
        return None
