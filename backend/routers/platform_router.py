"""
routers/platform_router.py

Platform admin endpoints — merged successor to `operator_router.py` and
`admin_router.py` (retired 2026-04-06 as part of the operator+admin merge).

All routes require the `require_platform_admin` grant check: a write grant
on the authority collection (canonical check), with a fallback fast-path
for the initial bootstrap operator recorded in `platform.operator_id`
settings (needed during the post-setup, pre-Phase-4 window when the
authority collection may not yet be fully granted).

Data tier: platform settings, user admin grants, and seed collections are
all Tier-1 kernel primitives per `.dev/features/layered-architecture.md`
§ Data Tiers. They stay DB-native; this router is the authoritative
surface for managing them. No `vnd.agience.platform-settings+json`
artifact is created — the artifact store is NOT the home for these.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from arango.database import StandardDatabase

from api.platform import PlatformUserResponse, PlatformUsersListResponse, SeedCollectionResponse
from core.dependencies import get_arango_db
from db.arango import (
    get_active_grants_for_principal_resource as db_get_active_grants,
    get_collection_by_id as db_get_collection_by_id,
    list_collection_artifacts as db_list_collection_artifacts,
    upsert_user_collection_grant as db_upsert_grant,
)
from db.arango_identity import (
    list_all_people as db_list_all_people,
    get_person_by_id as db_get_person_by_id,
)
from services.dependencies import get_auth, require_platform_admin, AuthContext
from services.bootstrap_types import AUTHORITY_COLLECTION_SLUG
from services.platform_topology import get_all_platform_collection_ids, get_id
from services.platform_settings_service import settings as platform_settings

logger = logging.getLogger(__name__)

platform_router = APIRouter(prefix="/platform", tags=["Platform"])


# ---------------------------------------------------------------------------
# Settings models
# ---------------------------------------------------------------------------

class SettingItem(BaseModel):
    key: str
    value: Optional[str] = None
    is_secret: bool = False


class UpdateSettingsRequest(BaseModel):
    settings: list[SettingItem]


class UpdateSettingsResponse(BaseModel):
    updated: int
    restart_required: bool = False


# Settings that require a restart to take effect. Infrastructure primitives
# (Arango + OpenSearch connection details) are loaded once at lifespan
# startup and cannot be hot-rebound without re-initializing their pools.
_RESTART_REQUIRED_KEYS = {
    "db.arango.host", "db.arango.port", "db.arango.username", "db.arango.password", "db.arango.database",
    "search.opensearch.host", "search.opensearch.port",
    "search.opensearch.username", "search.opensearch.password",
    "search.opensearch.use_ssl", "search.opensearch.verify_certs",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_user_platform_admin(arango_db: StandardDatabase, user_id: str) -> bool:
    """Check if a user has platform admin (write grant on authority collection)."""
    try:
        grants = db_get_active_grants(
            arango_db,
            grantee_id=user_id,
            resource_id=get_id(AUTHORITY_COLLECTION_SLUG),
        )
        return any(g.can_update and g.is_active() for g in grants)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Settings endpoints (former /operator/settings)
# ---------------------------------------------------------------------------

@platform_router.get("/settings")
async def get_all_settings(
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
) -> dict:
    """Get all platform settings grouped by category. Secret values are masked as None."""
    require_platform_admin(auth, arango_db)
    return {"categories": platform_settings.get_all_by_category()}


@platform_router.get("/settings/{category}")
async def get_settings_by_category(
    category: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
) -> list[dict]:
    """Get settings for a specific category. Secret values are masked."""
    require_platform_admin(auth, arango_db)
    grouped = platform_settings.get_all_by_category(category=category)
    return grouped.get(category, [])


@platform_router.patch("/settings", response_model=UpdateSettingsResponse)
async def update_settings(
    body: UpdateSettingsRequest,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
) -> UpdateSettingsResponse:
    """Update platform settings. Platform-admin only."""
    admin_user_id = require_platform_admin(auth, arango_db)
    settings_dicts = []
    restart_required = False

    for item in body.settings:
        # Skip secrets with a null/empty value — the frontend masks secret
        # values as None in GET responses; patching back with None would
        # overwrite the real stored secret with an empty string.
        if item.is_secret and not item.value:
            continue

        if item.key in _RESTART_REQUIRED_KEYS:
            restart_required = True

        category = item.key.split(".")[0] if "." in item.key else "platform"

        settings_dicts.append({
            "key": item.key,
            "value": item.value or "",
            "category": category,
            "is_secret": item.is_secret,
        })

    count = platform_settings.set_many(arango_db, settings_dicts, updated_by=admin_user_id)

    # Rebind config module variables with the new values.
    from core import config
    config.load_settings_from_db()

    return UpdateSettingsResponse(updated=count, restart_required=restart_required)


# ---------------------------------------------------------------------------
# User admin endpoints (former /admin/users)
# ---------------------------------------------------------------------------

@platform_router.get("/users", response_model=PlatformUsersListResponse)
async def list_users(
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """List all platform users with their platform-admin status."""
    require_platform_admin(auth, arango_db)
    rows = db_list_all_people(arango_db)
    users: List[PlatformUserResponse] = []
    for row in rows:
        person_id = row.get("_key") or row.get("id")
        created_time = row.get("created_time")
        if isinstance(created_time, str):
            ct_iso = created_time
        elif hasattr(created_time, "isoformat"):
            ct_iso = created_time.isoformat()
        else:
            ct_iso = None
        users.append(PlatformUserResponse(
            id=person_id,
            email=row.get("email"),
            name=row.get("name"),
            picture=row.get("picture"),
            is_platform_admin=_is_user_platform_admin(arango_db, person_id),
            created_time=ct_iso,
        ))
    return PlatformUsersListResponse(users=users)


@platform_router.post("/users/{user_id}/grant-admin")
async def grant_platform_admin(
    user_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Grant platform admin (write on all platform collections) to a user."""
    admin_user_id = require_platform_admin(auth, arango_db)
    target = db_get_person_by_id(arango_db, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    for col_id in get_all_platform_collection_ids():
        db_upsert_grant(
            arango_db,
            user_id=user_id,
            collection_id=col_id,
            granted_by=admin_user_id,
            can_read=True,
            can_update=True,
            name="Platform admin (granted by admin)",
        )
    logger.info("Platform admin %s granted platform admin to user %s", admin_user_id, user_id)
    return {"status": "granted", "user_id": user_id}


@platform_router.delete("/users/{user_id}/revoke-admin")
async def revoke_platform_admin(
    user_id: str,
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """Revoke platform admin (downgrade to read-only on platform collections)."""
    admin_user_id = require_platform_admin(auth, arango_db)
    if user_id == admin_user_id:
        raise HTTPException(status_code=400, detail="Cannot revoke your own platform admin access")

    target = db_get_person_by_id(arango_db, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    for col_id in get_all_platform_collection_ids():
        db_upsert_grant(
            arango_db,
            user_id=user_id,
            collection_id=col_id,
            granted_by=admin_user_id,
            can_read=True,
            can_update=False,
            name="Platform user (admin revoked)",
        )
    logger.info("Platform admin %s revoked platform admin from user %s", admin_user_id, user_id)
    return {"status": "revoked", "user_id": user_id}


# ---------------------------------------------------------------------------
# Seed collections endpoint (former /admin/seed-collections)
# ---------------------------------------------------------------------------

@platform_router.get("/seed-collections", response_model=List[SeedCollectionResponse])
async def list_seed_collections(
    auth: AuthContext = Depends(get_auth),
    arango_db: StandardDatabase = Depends(get_arango_db),
):
    """List platform seed collections with artifact counts."""
    require_platform_admin(auth, arango_db)
    results: List[SeedCollectionResponse] = []
    for col_id in get_all_platform_collection_ids():
        col = db_get_collection_by_id(arango_db, col_id)
        if not col:
            continue
        try:
            artifacts = db_list_collection_artifacts(arango_db, col_id) or []
            count = len(artifacts)
        except Exception:
            count = 0
        results.append(SeedCollectionResponse(
            id=col.id,
            name=col.name,
            description=col.description,
            artifact_count=count,
        ))
    return results
