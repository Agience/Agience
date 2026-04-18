"""
Platform topology: runtime ID registry.

All platform-owned collections and artifacts use random UUIDs as their
primary IDs (_key in ArangoDB). Slugs (defined in bootstrap_types) are
human-readable keys that map to those UUIDs.

The slug→UUID mappings are persisted in the ``platform_settings`` collection
(keys: ``platform.id.<slug>``). On restart, ``pre_resolve_platform_ids()``
loads them from settings — no slug-based AQL scans required.

On first boot the UUIDs are generated, registered in-memory, and written to
``platform_settings`` so subsequent boots are deterministic.
"""

import logging
import uuid
from typing import Optional

from arango.database import StandardDatabase

from services.bootstrap_types import (
    AGIENCE_CORE_SLUG,
    ALL_PLATFORM_COLLECTION_SLUGS,
    AUTHORITY_ARTIFACT_SLUG,
    HOST_ARTIFACT_SLUG,
    AGENCY_ARTIFACT_SLUG,
    AGENT_ARTIFACT_SLUG_PREFIX,
    LLM_CONNECTION_SLUG_PREFIX,
    SERVER_ARTIFACT_SLUG_PREFIX,
    PLATFORM_AGENT_SLUGS,
    PLATFORM_LLM_CONNECTION_SLUGS,
)

logger = logging.getLogger(__name__)

# Prefix used when persisting slug→UUID mappings in platform_settings.
_SETTINGS_PREFIX = "platform.id."

# ---------------------------------------------------------------------------
# Runtime ID registry
# Populated at startup by pre_resolve_platform_ids(), queried at request time.
# ---------------------------------------------------------------------------

_registry: dict[str, str] = {}


def register_id(slug: str, uuid_id: str) -> None:
    """Register a slug -> UUID mapping (called during bootstrap)."""
    _registry[slug] = uuid_id


def get_id(slug: str) -> str:
    """
    Get the UUID for a platform slug.
    Raises RuntimeError if the slug hasn't been registered yet.
    """
    if slug not in _registry:
        raise RuntimeError(
            f"Platform slug '{slug}' not registered. "
            "Was pre_resolve_platform_ids() called at startup?"
        )
    return _registry[slug]


def get_id_optional(slug: str) -> Optional[str]:
    """Get the UUID for a platform slug, or None if not registered."""
    return _registry.get(slug)


def get_all_platform_collection_ids() -> list[str]:
    """Return UUIDs for all platform-owned collections (admin grant iteration)."""
    return [get_id(s) for s in ALL_PLATFORM_COLLECTION_SLUGS]


def clear_registry() -> None:
    """Clear the registry (used in tests)."""
    _registry.clear()


# ---------------------------------------------------------------------------
# Bootstrap pre-resolution
# ---------------------------------------------------------------------------

def _all_platform_slugs() -> list[str]:
    """Return every slug that needs an ID mapping."""
    from services import server_registry

    slugs: list[str] = []
    slugs.extend(ALL_PLATFORM_COLLECTION_SLUGS)
    slugs.extend([AUTHORITY_ARTIFACT_SLUG, HOST_ARTIFACT_SLUG, AGENCY_ARTIFACT_SLUG])
    slugs.append(AGIENCE_CORE_SLUG)
    slugs.extend(f"{AGENT_ARTIFACT_SLUG_PREFIX}{s}" for s in PLATFORM_AGENT_SLUGS)
    slugs.extend(f"{LLM_CONNECTION_SLUG_PREFIX}{s}" for s in PLATFORM_LLM_CONNECTION_SLUGS)
    slugs.extend(f"{SERVER_ARTIFACT_SLUG_PREFIX}{name}" for name in server_registry.all_names())
    return slugs


def pre_resolve_platform_ids(arango_db: StandardDatabase) -> None:
    """
    Load or generate UUIDs for all platform singleton collections and
    artifact root_ids. Must be called once at startup BEFORE the ensure_*
    bootstrap functions.

    Resolution order for each slug:
      1. Already in registry (e.g. test setup) → skip
      2. Persisted in platform_settings (``platform.id.<slug>``) → register
      3. Neither → generate new UUID, register, and persist to settings
    """
    from services.platform_settings_service import settings as _settings

    all_slugs = _all_platform_slugs()
    new_settings: list[dict] = []

    for slug in all_slugs:
        # 1. Already registered (e.g. from a previous call or test harness)
        if slug in _registry:
            logger.debug("Slug '%s' already in registry -> %s", slug, _registry[slug])
            continue

        # 2. Check platform_settings cache
        settings_key = f"{_SETTINGS_PREFIX}{slug}"
        persisted = _settings.get(settings_key)
        if persisted:
            register_id(slug, persisted)
            logger.debug("Loaded slug '%s' from settings -> %s", slug, persisted)
            continue

        # 3. Generate new UUID and queue it for persistence
        new_id = str(uuid.uuid4())
        register_id(slug, new_id)
        new_settings.append({
            "key": settings_key,
            "value": new_id,
            "category": "platform",
        })
        logger.info("Generated new UUID for slug '%s' -> %s", slug, new_id)

    # Persist any newly generated IDs so they survive restarts.
    if new_settings:
        _settings.set_many(arango_db, new_settings)
        logger.info("Persisted %d new platform ID mappings to settings", len(new_settings))
