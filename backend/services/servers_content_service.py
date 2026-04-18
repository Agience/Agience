"""Seed first-party MCP server artifacts.

Every Agience persona (Aria, Astra, Atlas, Sage, Nexus, Ophan, Seraph,
Verso) is seeded as a `vnd.agience.mcp-server+json` artifact in the
`agience-seeds-all-servers` collection at platform bootstrap. First-party
servers are identified by `context.transport == "builtin"` — the MCP client
infrastructure routes these through `server_registry` and issues a
delegation JWT with `aud=agience-server-{name}`.

Third-party MCP servers continue to be user-registered artifacts with
`context.transport = "http"` (or `"stdio"`).

This service runs idempotently at startup via `main.py` lifespan, parallel
to `authority_content_service.ensure_current_instance_authority` and
`resources_content_service.ensure_platform_resources`.
"""

from __future__ import annotations

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
    AGIENCE_CORE_SLUG,
    ALL_SERVERS_COLLECTION_SLUG,
    MCP_SERVER_CONTENT_TYPE,
    SERVER_ARTIFACT_SLUG_PREFIX,
)
from services.platform_topology import get_id
from services import server_registry

logger = logging.getLogger(__name__)


def ensure_platform_servers(arango_db: StandardDatabase) -> Optional[str]:
    """Ensure the all-servers collection exists and contains one seeded
    artifact per first-party MCP server persona. Idempotent.

    Returns the collection ID, or None on failure.
    """
    collection_id = _ensure_all_servers_collection(arango_db)
    if not collection_id:
        return None

    # Seed agience-core (the kernel MCP server) as a proper artifact
    _ensure_server_artifact_linked(
        arango_db,
        collection_id=collection_id,
        root_id=get_id(AGIENCE_CORE_SLUG),
        context=_build_agience_core_context(),
        content="Agience Core — the platform's built-in MCP server providing artifact CRUD, search, commit, and agent invocation tools.",
        content_type=MCP_SERVER_CONTENT_TYPE,
    )

    for entry in server_registry.all_entries():
        artifact_slug = f"{SERVER_ARTIFACT_SLUG_PREFIX}{entry.name}"
        root_id = get_id(artifact_slug)
        _ensure_server_artifact_linked(
            arango_db,
            collection_id=collection_id,
            root_id=root_id,
            context=_build_server_context(entry),
            content=_build_server_content(entry),
            content_type=MCP_SERVER_CONTENT_TYPE,
        )

    # TODO: Capability materialization should happen lazily on first
    # connection, not at startup.  When the platform first connects to
    # each server (e.g. via list_servers_for_workspace or invoke_tool),
    # call mcp_service.materialize_server_capabilities(db, root_id,
    # AGIENCE_PLATFORM_USER_ID) to create tool/resource/prompt child
    # artifacts.  Until then, callers can trigger it on demand via
    # POST /artifacts/{server_id}/op/materialize_capabilities.

    return collection_id


def grant_servers_collection_to_user(arango_db: StandardDatabase, user_id: str) -> None:
    """Grant a user read access to the platform MCP server collection.
    Idempotent. Safe to call on every login."""
    if not user_id:
        logger.warning("grant_servers_collection_to_user called with empty user_id — skipping")
        return

    collection_id = get_id(ALL_SERVERS_COLLECTION_SLUG)
    try:
        _grant, changed = db_upsert_user_collection_grant(
            arango_db,
            user_id=user_id,
            collection_id=collection_id,
            granted_by=AGIENCE_PLATFORM_USER_ID,
            can_read=True,
            can_update=False,
            can_invoke=True,
            name="Platform MCP servers (auto-granted on first login)",
        )
        if changed:
            logger.info(
                "Granted user %s read access to all-servers collection %s",
                user_id, collection_id,
            )
    except Exception:
        logger.exception(
            "Failed to grant user %s read access to all-servers collection %s",
            user_id, collection_id,
        )


def _ensure_all_servers_collection(arango_db: StandardDatabase) -> Optional[str]:
    col_id = get_id(ALL_SERVERS_COLLECTION_SLUG)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        now = datetime.now(timezone.utc).isoformat()
        from entities.collection import COLLECTION_CONTENT_TYPE
        collection = CollectionEntity(
            id=col_id,
            name="Agience Servers",
            description=(
                "Platform-owned MCP server catalog. Every first-party persona "
                "(Aria, Astra, Atlas, Sage, Nexus, Ophan, Seraph, Verso) is "
                "seeded here as a vnd.agience.mcp-server+json artifact. Users "
                "receive read access automatically on first login."
            ),
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, collection)
        # NOTE: No self-referential membership is created here; cross-reference member links are created
        # by inbox_seeds_content_service._populate_platform_artifacts.
        logger.info("Created all-servers collection (id=%s)", col_id)
        return col_id
    except Exception:
        logger.exception("Failed to create all-servers collection (id=%s)", col_id)
        return None


def _ensure_server_artifact_linked(
    arango_db: StandardDatabase,
    *,
    collection_id: str,
    root_id: str,
    context: str,
    content: str,
    content_type: Optional[str] = None,
) -> bool:
    """Idempotently ensure a server artifact exists and is linked to the
    all-servers collection. Mirrors the pattern in
    `resources_content_service._ensure_artifact_linked`.
    """
    linked = db_get_artifact_by_collection_and_root(arango_db, collection_id, root_id)
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
            logger.exception("Failed linking existing MCP server artifact root %s", root_id)
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
        logger.info(
            "Created platform MCP server artifact (root_id=%s, version=%s)",
            root_id, artifact.id,
        )
        return True
    except Exception:
        logger.exception("Failed to create MCP server artifact root %s", root_id)
        return False


def _build_server_context(entry) -> str:
    """Build the artifact context for a first-party MCP server record.

    Key invariants:
    - `content_type` is always `MCP_SERVER_CONTENT_TYPE` so the dispatcher resolves
      `operations.invoke` via the type registry.
    - `transport: "builtin"` is the resolver signal — `mcp_service` routes
      these through `server_registry` without requiring a URL.
    - `client_id` matches the `server_registry.all_client_ids()` fast-path in
      `auth_router.handle_client_credentials_grant` so delegation JWTs are
      issued with `aud=agience-server-{name}`.
    - `name` is echoed into context so clients can identify the persona
      without consulting the slug registry.
    """
    context = {
        "type": "mcp-server",
        "content_type": MCP_SERVER_CONTENT_TYPE,
        "title": entry.title,
        "description": entry.summary,
        "mcp_server": {
            "version": 1,
            "kind": "platform-builtin",
            "name": entry.name,
            "role": entry.role,
            "client_id": entry.client_id,
            "transport": "builtin",
        },
    }
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)


def _build_server_content(entry) -> str:
    return (
        f"{entry.title} — {entry.role}. "
        f"{entry.summary}"
    )


def _build_agience_core_context() -> str:
    """Build the artifact context for the kernel MCP server (agience-core).

    Follows the same shape as persona server contexts but with
    ``transport: "local"`` since agience-core is an in-process MCP
    handler, not an external HTTP server.
    """
    context = {
        "type": "mcp-server",
        "content_type": MCP_SERVER_CONTENT_TYPE,
        "title": "Agience Core",
        "description": "The platform's built-in MCP server — artifact CRUD, search, commit, and agent invocation.",
        "mcp_server": {
            "version": 1,
            "kind": "platform-builtin",
            "transport": "local",
        },
    }
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)
