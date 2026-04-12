import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Set, TYPE_CHECKING, Any, Dict, Mapping, Sequence, cast

import core.event_bus as event_bus

try:
    from fastapi import HTTPException, status  # type: ignore
except Exception:
    # Minimal fallbacks for environments without FastAPI (static analysis, tests, etc.)
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _Status:
        HTTP_400_BAD_REQUEST = 400
        HTTP_404_NOT_FOUND = 404
        HTTP_500_INTERNAL_SERVER_ERROR = 500

    status = _Status()
if TYPE_CHECKING:
    from arango.database import StandardDatabase  # type: ignore
else:
    StandardDatabase = Any

# arango may not be installed in all environments (static analysis, tests, etc.).
# Try to import the specific exception and fall back to a local definition if unavailable.
try:
    from arango.exceptions import DocumentInsertError  # type: ignore
except Exception:
    class DocumentInsertError(Exception):
        """Fallback when arango.exceptions is not available."""
        pass

from entities.artifact import Artifact as ArtifactEntity
from entities.collection import Collection as CollectionEntity
from entities.commit import Commit as CommitEntity
from entities.commit_item import CommitItem as CommitItemEntity
from entities.grant import Grant as GrantEntity

from db.arango import (
    # Collections
    create_collection as db_create_collection,
    get_collection_by_id as db_get_collection_by_id,
    get_collections_by_owner_id as db_get_collections_by_owner_id,
    update_collection as db_update_collection,
    delete_collection as db_delete_collection,
    get_collection_ids_for_root as db_get_collection_ids_for_root,
    batch_get_collection_ids_for_roots as db_batch_get_collection_ids_for_roots,

    # Artifacts (unified store)
    create_artifact as db_create_artifact,
    create_artifacts_batch as db_create_artifacts_batch,
    get_artifact as db_get_artifact,
    get_artifacts_by_creator_id as db_get_artifacts_by_creator_id,
    get_draft_artifact as db_get_draft_artifact,
    get_latest_committed_artifact as db_get_latest_committed_artifact,
    list_collection_artifacts as db_list_collection_artifacts,
    archive_artifact as db_archive_artifact,
    after_key as _db_after_key,
    get_last_order_key as _db_get_last_order_key,

    # Edges
    add_artifact_to_collection as db_add_artifact_to_collection,
    add_artifacts_to_collection_batch as _db_add_edges_batch,
    remove_artifact_from_collection as _db_remove_edge,

    # Commits
    create_commit as db_create_commit,
    create_commit_items as db_create_commit_items,
    get_commit_by_id as db_get_commit_by_id,
    get_commits_for_collection as db_get_commits_for_collection,

    # Grants - unified authorization records
    get_active_collection_ids_for_user as db_get_active_collection_ids_for_user,
)


# Local thin adapters that re-shape unified-store calls to the names this
# module's body still uses. Keeps the rest of the file untouched.

def db_get_artifact_by_version_id(db, version_id):
    a = db_get_artifact(db, version_id)
    if a and a.state == "archived":
        return None
    return a


def db_get_latest_artifact_version_by_root_id(db, root_id):
    return db_get_latest_committed_artifact(db, root_id)


def db_get_artifact_by_collection_id_and_root_id(db, collection_id, root_id):
    draft = db_get_draft_artifact(db, root_id, collection_id)
    if draft:
        return draft
    return db_get_latest_committed_artifact(db, root_id, collection_id)


def db_get_artifacts_by_collection_id(db, collection_id):
    from entities.artifact import Artifact
    rows = db_list_collection_artifacts(db, collection_id)
    return [Artifact.from_dict(r) for r in rows]


def db_add_cards_to_collection_batch(db, collection_id, root_ids):
    """Batch-insert collection_artifacts edges for the given root IDs."""
    prev = _db_get_last_order_key(db, collection_id)
    edges = []
    for rid in root_ids:
        prev = _db_after_key(prev)
        edges.append((rid, prev))
    return _db_add_edges_batch(db, collection_id, edges)


def db_delete_collection_artifact_by_collection_and_root(db, collection_id, root_id):
    return _db_remove_edge(db, collection_id, root_id)

from search.ingest.pipeline_unified import (  # noqa: E402
    index_artifact,
    enqueue_index_artifact,
    enqueue_index_artifacts_batch,
    delete_artifact_from_index,
)
from services.content_service import generate_signed_url  # noqa: E402

logger = logging.getLogger(__name__)

def ensure_collection_descriptor(db: StandardDatabase, collection: CollectionEntity) -> CollectionEntity:
    """Ensure container artifacts are searchable in OpenSearch.

    Container-as-artifact means we index the container artifact itself
    (workspace/collection), not a separate descriptor artifact.
    """
    try:
        index_artifact(collection, collection.id, is_head=True)
    except Exception as e:
        logger.warning("Failed to index container artifact %s: %s", collection.id, e)
    return collection

def _attach_committed_collection_ids(db: StandardDatabase, artifacts: Sequence[ArtifactEntity]) -> None:
    """Populate committed_collection_ids on each artifact entity."""
    if not artifacts:
        return

    root_ids: List[str] = []
    for artifact in artifacts:
        root_value = getattr(artifact, "root_id", None)
        if not root_value:
            continue
        root_ids.append(str(root_value))
    if not root_ids:
        for artifact in artifacts:
            setattr(artifact, "committed_collection_ids", [])
        return

    # Deduplicate roots to keep queries lean
    unique_root_ids = list(dict.fromkeys(root_ids))

    memberships: Dict[str, List[str]] = {}
    try:
        memberships = db_batch_get_collection_ids_for_roots(db, unique_root_ids)
    except Exception:
        logger.exception("Failed batch fetching memberships; falling back to per-root queries")
        memberships = {}

    # Fill any missing roots via individual lookups
    for root_id in unique_root_ids:
        if root_id in memberships:
            continue
        try:
            memberships[root_id] = db_get_collection_ids_for_root(db, root_id) or []
        except Exception:
            memberships[root_id] = []

    # Attach sorted unique memberships per artifact
    for artifact in artifacts:
        root_id = getattr(artifact, "root_id", None)
        committed = memberships.get(root_id or "", []) if root_id else []
        setattr(artifact, "committed_collection_ids", sorted(dict.fromkeys(committed)))


def attach_committed_collection_ids(db: StandardDatabase, artifacts: Sequence[ArtifactEntity]) -> None:
    """Public API: populate committed_collection_ids on a list of artifacts."""
    _attach_committed_collection_ids(db, artifacts)


# === Collection CRUD ===

def create_new_collection(
    db: StandardDatabase,
    owner_id: str,
    name: str,
    description: str,
    is_personal: bool = False,
) -> CollectionEntity:
    from entities.collection import COLLECTION_CONTENT_TYPE
    now = datetime.now(timezone.utc).isoformat()
    entity = CollectionEntity(
        id=owner_id if is_personal else str(uuid.uuid4()),
        name=name,
        description=description,
        created_by=owner_id,
        content_type=COLLECTION_CONTENT_TYPE,
        state=CollectionEntity.STATE_COMMITTED,
        created_time=now,
        modified_time=now,
    )
    created = db_create_collection(db, entity)
    ensure_collection_descriptor(db, created)
    return created


def get_collections_for_user(
    db: StandardDatabase,
    user_id: Optional[str],
    grant_key: Optional[GrantEntity] = None,
) -> List[CollectionEntity]:
    """
    Get collections accessible to user/grant key.
    Includes: owned collections + grant-accessible collections + grant_key collection.
    """
    from db.arango import get_containers_for_user as db_get_containers_for_user
    owned = db_get_containers_for_user(db, user_id) if user_id else []
    seen_ids: Set[str] = {c.id for c in owned}

    # Grant-accessible collections (e.g. Agience platform collections shared read-only)
    if user_id:
        try:
            grant_col_ids = db_get_active_collection_ids_for_user(db, user_id)
            for col_id in grant_col_ids:
                if col_id not in seen_ids:
                    col = db_get_collection_by_id(db, col_id)
                    if col:
                        owned.append(col)
                        seen_ids.add(col_id)
        except Exception:
            logger.exception("get_collections_for_user: failed resolving user grants")

    # Grant key grant
    if grant_key:
        if (
            grant_key.grantee_type == GrantEntity.GRANTEE_GRANT_KEY
            and grant_key.resource_type == "collection"
            and grant_key.can_read
        ):
            read_requires_identity = (
                grant_key.read_requires_identity
                if grant_key.read_requires_identity is not None
                else grant_key.requires_identity
            )
            if not (read_requires_identity and not user_id):
                shared = db_get_collection_by_id(db, grant_key.resource_id)
                if shared and shared.id not in seen_ids:
                    owned.append(shared)
    
    return owned


def get_collection_for_user(
    db: StandardDatabase,
    user_id: Optional[str],
    collection_id: str,
) -> CollectionEntity:
    collection = db_get_collection_by_id(db, collection_id)
    if not collection:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return collection


def update_user_collection(
    db: StandardDatabase,
    user_id: Optional[str],
    collection_id: str,
    name: Optional[str],
    description: Optional[str],
) -> CollectionEntity:
    collection = db_get_collection_by_id(db, collection_id)
    if not collection or collection.created_by != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if name:
        collection.name = name
    if description:
        collection.description = description
    collection.modified_time = datetime.now(timezone.utc).isoformat()
    updated = db_update_collection(db, collection)
    if not updated:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to update collection")
    return updated


def delete_user_collection(db: StandardDatabase, user_id: Optional[str], collection_id: str) -> None:
    """
    Delete a collection and clean up references:
    - Remove links from all artifacts to this collection
    - If an artifact (root) has no other collection references after unlink, delete all its versions
    - Remove corresponding search index documents
    """
    collection = db_get_collection_by_id(db, collection_id)
    if not collection or collection.created_by != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    # 1) Fetch all artifacts currently linked to this collection
    linked_cards = db_get_artifacts_by_collection_id(db, collection_id)

    # 2) For each linked root: unlink and optionally delete root (if last reference)
    for artifact in linked_cards:
        root_id = getattr(artifact, "root_id", None)
        version_id = getattr(artifact, "id", None)
        if not root_id:
            continue

        # Unlink from this collection
        try:
            db_delete_collection_artifact_by_collection_and_root(db, collection_id, root_id)
        except Exception:
            logger.exception("Failed unlinking root %s from collection %s", root_id, collection_id)

        # Remove unified index doc for this (collection, root, version)
        if version_id:
            try:
                delete_artifact_from_index(version_id, root_id)
            except Exception:
                logger.exception("Failed to delete index for version %s (root %s)", version_id, root_id)

        # Check if this root remains in any collection; if not, delete all its versions
        try:
            memberships = db_get_collection_ids_for_root(db, root_id)  # type: ignore[attr-defined]
        except Exception:
            memberships = []
        if not memberships:
            # Delete all versions for this root and remove their index docs
            try:
                # deleted_versions = [] db_delete_artifact_versions_by_root(db, root_id)  # type: ignore[attr-defined]        
                deleted_versions = [] # Placeholder
            except Exception:
                deleted_versions = []
            for vid in deleted_versions:
                try:
                    delete_artifact_from_index(vid, root_id)
                except Exception:
                    logger.exception("Failed to delete index for version %s (root %s)", vid, root_id)

    # 3) Delete the collection itself
    if not db_delete_collection(db, collection_id):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Deletion failed")


# === Artifact Operations ===

def create_new_artifact(db: StandardDatabase, user_id: str, context: str, content: str) -> ArtifactEntity:
    """
    Create a new artifact VERSION document only. Root and linking are handled separately.
    EXACT 1:1 mapping - no modified_by/modified_time (don't exist in ArangoDB schema).
    """
    now = datetime.now(timezone.utc).isoformat()
    artifact = ArtifactEntity(
        id=str(uuid.uuid4()),
        context=context,
        content=content,
        created_by=user_id,
        created_time=now,
    )
    created = db_create_artifact(db, artifact)
    _attach_committed_collection_ids(db, [created])
    _attach_committed_collection_ids(db, [created])
    return created


def create_and_add_artifact(
    db: StandardDatabase,
    user_id: str,
    collection_id: str,
    context: str,
    content: str,
    *,
    record: bool = True,
    actor_id: Optional[str] = None,
) -> ArtifactEntity:
    """
    Create a brand-new ROOT and VERSION and link them into the collection.
    """
    now = datetime.now(timezone.utc).isoformat()
    root_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())

    artifact = ArtifactEntity(
        id=version_id,
        root_id=root_id,
        context=context,
        content=content,
        created_by=actor_id or user_id,
        created_time=now,
    )
    created = db_create_artifact(db, artifact)

    db_add_artifact_to_collection(db, collection_id, root_id, created.id)

    if record:
        record_collection_commit(
            db, user_id, collection_id,
            adds=[created.id],
            message=f"Added artifact version {created.id}",
            author_id=actor_id or user_id,
        )

    # Index to unified search
    try:
        enqueue_index_artifact(created, collection_id, is_head=True, tenant_id=user_id)
    except Exception as e:
        logger.warning(f"Failed to enqueue indexing for collection artifact {created.id}: {e}")

    event_bus.emit_artifact_event_sync(collection_id, "artifact.created", {
        "artifact": {**created.to_dict(), "collection_id": collection_id}
    })
    return created


def create_and_add_artifacts_batch(
    db: StandardDatabase,
    user_id: str,
    collection_id: str,
    artifacts: List[tuple[str, str]],
    *,
    record: bool = True,
    actor_id: Optional[str] = None,
) -> List[ArtifactEntity]:
    """
    Efficiently create multiple brand-new ROOT+VERSION artifacts and link them into the collection.
    - Creates all versions and CollectionArtifact links
    - Records a single commit with all adds (when record=True)
    - Indexes each head version into unified search (async via queue)

    artifacts: list of (context_json, content_str)
    """
    if not artifacts:
        return []

    created: List[ArtifactEntity] = []
    add_version_ids: List[str] = []

    # Build entities first to enable true batch insertion
    now = datetime.now(timezone.utc).isoformat()
    for context, content in artifacts:
        root_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        created.append(ArtifactEntity(
            id=version_id,
            root_id=root_id,
            context=context,
            content=content,
            created_by=actor_id or user_id,
            created_time=now,
        ))
        add_version_ids.append(version_id)

    # Batch insert versions
    created = db_create_artifacts_batch(db, created) or []

    # Batch link all artifacts to the collection (brand-new roots)
    root_ids = [str(c.root_id) for c in created]
    db_add_cards_to_collection_batch(db, collection_id, root_ids)

    if record and add_version_ids:
        record_collection_commit(
            db,
            user_id,
            collection_id,
            adds=add_version_ids,
            message=f"Added {len(add_version_ids)} artifact versions",
            author_id=actor_id or user_id,
        )

    # Index to unified search (batch enqueued to async worker)
    if created:
        logger.info(f"[{datetime.now(timezone.utc).isoformat()}] [outbox] Enqueueing {len(created)} artifacts for indexing...")
        enqueue_start = time.time()
        try:
            enqueue_index_artifacts_batch(created, collection_id, is_head=True, tenant_id=user_id)
        except Exception as e:
            logger.error(f"Failed to enqueue batch indexing for {len(created)} artifacts: {e}")
            raise
        logger.info(f"[{datetime.now(timezone.utc).isoformat()}] [ok]... Enqueued in {time.time() - enqueue_start:.3f}s")

    attach_start = time.time()
    _attach_committed_collection_ids(db, created)
    logger.info(f"[{datetime.now(timezone.utc).isoformat()}] [ok]... Attached collection IDs in {time.time() - attach_start:.3f}s")

    for artifact in created:
        event_bus.emit_artifact_event_sync(collection_id, "artifact.created", {
            "artifact": {**artifact.to_dict(), "collection_id": collection_id}
        })
    return created


def edit_artifact_in_collection(
    db: StandardDatabase,
    user_id: str,
    collection_id: str,
    root_id: str,
    context: str,
    content: str,
    *,
    archive: bool = False,
    record: bool = True,
    prev_version_id: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> ArtifactEntity:
    """
    Create a NEW VERSION under an existing ROOT and relink to it.
    Archive the previously linked VERSION if requested.
    Provenance: remove by previous VERSION id, add by new VERSION id.

    prev_version_id: Pre-fetched currently linked version (skip lookup)
    """
    # Read the currently linked version BEFORE unlinking (if not provided)
    if prev_version_id is None:
        linked = db_get_artifact_by_collection_id_and_root_id(db, collection_id, root_id)
        prev_version_id = getattr(linked, "id", None) if linked else None

    # Optionally archive the previously linked version
    if archive and prev_version_id:
        try:
            db_archive_artifact(db, user_id, prev_version_id)
        except Exception:
            logger.exception("Failed to archive previous version %s", prev_version_id)

    # Unlink current root from collection (will be replaced)
    db_delete_collection_artifact_by_collection_and_root(db, collection_id, root_id)

    # Create new version
    now = datetime.now(timezone.utc).isoformat()
    new_id = str(uuid.uuid4())
    new_artifact = ArtifactEntity(
        id=new_id,
        root_id=root_id,
        collection_id=collection_id,
        context=context,
        content=content,
        created_by=actor_id or user_id,
        created_time=now,
    )
    created = db_create_artifact(db, new_artifact)

    # Relink to new version
    db_add_artifact_to_collection(db, collection_id, root_id, created.id)

    if record:
        record_collection_commit(
            db, user_id, collection_id,
            removes=[prev_version_id] if prev_version_id else [],
            adds=[created.id],
            message=f"Replaced version {prev_version_id} with {created.id}",
            author_id=actor_id or user_id,
        )
    
    # Delete old version from unified search index
    if prev_version_id:
        try:
            delete_artifact_from_index(prev_version_id, root_id)
        except Exception as e:
            logger.warning(f"Failed to delete old collection artifact from search {prev_version_id}: {e}")
    
    # Index new version to unified search
    try:
        index_artifact(created, collection_id, is_head=True)
    except Exception as e:
        logger.warning(f"Failed to index collection artifact {created.id}: {e}")

    _attach_committed_collection_ids(db, [created])
    event_bus.emit_artifact_event_sync(collection_id, "artifact.updated", {
        "artifact": {**created.to_dict(), "collection_id": collection_id}
    })
    return created




def remove_artifact_from_collection_by_version(
    db: StandardDatabase,
    user_id: str,
    collection_id: str,
    *,
    version_id: str,
    archive: bool = False,
    record: bool = True,
    root_id: Optional[str] = None,
    linked_version_id: Optional[str] = None,
) -> None:
    """
    Unlink by VERSION id. If the currently linked version equals version_id, unlink.
    If it differs, unlink by root to honor user intent and consider that a drift case.
    If archive=True and you are unlinking the currently linked version, set is_archived=true.
    Provenance: record remove using VERSION id.

    root_id: Pre-fetched root_id (skip version lookup)
    linked_version_id: Pre-fetched currently linked version (skip link lookup)
    """
    # Get root_id if not provided
    if not root_id:
        v = db_get_artifact_by_version_id(db, version_id)
        if not v:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact version not found")
        root_id = getattr(v, "root_id", None)
        if not root_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Version has no root_id")

    # Get linked version if not provided
    if linked_version_id is None:
        linked = db_get_artifact_by_collection_id_and_root_id(db, collection_id, root_id)
        linked_version_id = getattr(linked, "id", None) if linked else None

    # Unlink
    db_delete_collection_artifact_by_collection_and_root(db, collection_id, root_id)

    # Archive only if we were unlinking the currently linked version
    if archive and linked_version_id and linked_version_id == version_id:
        try:
            db_archive_artifact(db, user_id, version_id)
        except Exception:
            logger.exception("Failed to archive version %s on remove", version_id)

    if record:
        record_collection_commit(
            db, user_id, collection_id,
            removes=[version_id],
            message=f"Removed version {version_id} from collection {collection_id}",
        )
    
    # Delete from unified search index if this was the linked version
    if linked_version_id and linked_version_id == version_id:
        try:
            delete_artifact_from_index(version_id, root_id)
        except Exception as e:
            logger.warning(f"Failed to delete collection artifact from search {version_id}: {e}")

    event_bus.emit_artifact_event_sync(collection_id, "artifact.deleted", {
        "artifact_id": root_id, "collection_id": collection_id
    })


def get_unattached_artifacts(db: StandardDatabase, user_id: str) -> List[ArtifactEntity]:
    """
    Return artifacts created by `user_id` that have no CollectionArtifact link.
    We check link existence per root. If a version lacks a visible root_id field,
    we conservatively skip it.
    """
    all_cards = db_get_artifacts_by_creator_id(db, user_id)

    # group one representative version per root
    roots_seen: Set[str] = set()
    reps: List[ArtifactEntity] = []
    for c in all_cards:
        rid = getattr(c, "root_id", None)
        if not rid:
            continue
        if rid not in roots_seen:
            roots_seen.add(rid)
            reps.append(c)

    unattached: List[ArtifactEntity] = []
    for c in reps:
        rid = getattr(c, "root_id", None)
        if not rid:
            continue
        # Use the arango helper to get collection ids linked to this root.
        linked_ids = db_get_collection_ids_for_root(db, rid) or []
        if not linked_ids:
            unattached.append(c)

    _attach_committed_collection_ids(db, unattached)
    return unattached


def get_artifacts_for_user(db: StandardDatabase, user_id: str) -> List[ArtifactEntity]:
    artifacts = db_get_artifacts_by_creator_id(db, user_id)
    _attach_committed_collection_ids(db, artifacts)
    return artifacts


def get_artifact_by_id_for_user(
    db: StandardDatabase,
    user_id: Optional[str],
    artifact_root_id: str,
) -> ArtifactEntity:
    latest = db_get_latest_artifact_version_by_root_id(db, artifact_root_id)
    if latest and getattr(latest, "created_by", None) == user_id:
        _attach_committed_collection_ids(db, [latest])
        return latest

    linked_collection_ids = db_get_collection_ids_for_root(db, artifact_root_id)
    for col_id in linked_collection_ids:
        artifact = db_get_artifact_by_collection_id_and_root_id(db, col_id, artifact_root_id)
        if artifact:
            _attach_committed_collection_ids(db, [artifact])
            return artifact

    raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")


def get_collection_artifact(
    db: StandardDatabase,
    user_id: Optional[str],
    collection_id: str,
    artifact_root_id: str,
) -> ArtifactEntity:
    artifact = db_get_artifact_by_collection_id_and_root_id(db, collection_id, artifact_root_id)
    if not artifact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _attach_committed_collection_ids(db, [artifact])
    return artifact


def get_collection_artifact_content_url(
    db: StandardDatabase,
    user_id: Optional[str],
    collection_id: str,
    artifact_root_id: str,
    *,
    expires_in: int,
) -> Dict[str, Any]:
    collection = db_get_collection_by_id(db, collection_id)
    if not collection:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    artifact = db_get_artifact_by_collection_id_and_root_id(db, collection_id, artifact_root_id)
    if not artifact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    raw_context = getattr(artifact, "context", None)
    if isinstance(raw_context, str):
        try:
            ctx = json.loads(raw_context or "{}")
        except Exception:
            ctx = {}
    elif isinstance(raw_context, Mapping):
        ctx = dict(raw_context)
    else:
        ctx = {}

    access = ctx.get("access", "private")
    if access == "public" and "uri" in ctx:
        return {
            "url": ctx["uri"],
            "expires_in": None,
            "filename": ctx.get("filename", "download"),
        }

    if ctx.get("content_source") != "agience-content":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not an agience-hosted file")

    key = ctx.get("content_key") or f"{collection.created_by}/{artifact_root_id}.content"
    filename = ctx.get("filename", "download")
    content_type = ctx.get("content_type", "application/octet-stream")
    url = generate_signed_url(
        key=key,
        filename=filename,
        content_type=content_type,
        expires_in=expires_in,
    )
    return {
        "url": url,
        "expires_in": expires_in,
        "filename": filename,
    }


def get_collection_artifacts_batch(
    db: StandardDatabase,
    user_id: Optional[str],
    collection_id: str,
    artifact_root_ids: List[str],
) -> List[ArtifactEntity]:
    """Batch fetch multiple artifacts from a collection."""
    artifacts = []
    for root_id in artifact_root_ids:
        artifact = db_get_artifact_by_collection_id_and_root_id(db, collection_id, root_id)
        if artifact and not getattr(artifact, "is_archived", False):
            artifacts.append(artifact)
    _attach_committed_collection_ids(db, artifacts)
    return artifacts


def get_collection_artifacts_batch_global(
    db: StandardDatabase,
    user_id: Optional[str],
    artifact_root_ids: List[str],
) -> List[ArtifactEntity]:
    """Batch fetch multiple artifacts across all accessible collections by root IDs."""
    owned_collections = db_get_collections_by_owner_id(db, user_id) if user_id else []
    collection_ids = {c.id for c in owned_collections}
    if user_id:
        try:
            collection_ids.update(db_get_active_collection_ids_for_user(db, user_id))
        except Exception:
            logger.exception("get_collection_artifacts_batch_global: failed resolving user grants")
    
    # For each root_id, find the artifact in any accessible collection
    artifacts = []
    seen_root_ids = set()
    for root_id in artifact_root_ids:
        if root_id in seen_root_ids:
            continue  # Skip duplicates
        
        # Try to find this root_id in any accessible collection
        for collection_id in collection_ids:
            artifact = db_get_artifact_by_collection_id_and_root_id(db, collection_id, root_id)
            if artifact and not getattr(artifact, "is_archived", False):
                artifacts.append(artifact)
                seen_root_ids.add(root_id)
                break  # Found it, move to next root_id
    
    _attach_committed_collection_ids(db, artifacts)
    return artifacts


def get_collection_artifacts(
    db: StandardDatabase,
    user_id: Optional[str],
    collection_id: str,
) -> List[ArtifactEntity]:
    all_artifacts = db_get_artifacts_by_collection_id(db, collection_id)
    active_artifacts = [artifact for artifact in all_artifacts if not getattr(artifact, "is_archived", False)]
    _attach_committed_collection_ids(db, active_artifacts)
    return active_artifacts


def add_artifact_to_collection_with_access_check(
    db: StandardDatabase,
    user_id: str,
    collection_id: str,
    version_id: str,
    root_id: Optional[str] = None,
) -> ArtifactEntity:
    """
    Service-level wrapper: Link an existing VERSION into a collection.
    Resolve ROOT from the VERSION.

    root_id: Pre-fetched root_id (skip version lookup)
    """

    # Validate version exists and fetch its root (if not provided)
    artifact = None
    if not root_id:
        artifact = db_get_artifact_by_version_id(db, version_id)
        if not artifact:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact version not found")
        root_id = getattr(artifact, "root_id", None)
        if not root_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Version has no root_id")
    
    # Fetch artifact if we didn't already (for return value)
    if not artifact:
        artifact = db_get_artifact_by_version_id(db, version_id)
        if not artifact:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact version not found")

    # Create/replace link
    db_delete_collection_artifact_by_collection_and_root(db, collection_id, root_id)
    db_add_artifact_to_collection(db, collection_id, root_id, version_id)

    record_collection_commit(
        db, user_id, collection_id,
        adds=[version_id],
        message=f"Linked existing artifact version {version_id}",
    )
    _attach_committed_collection_ids(db, [artifact])
    event_bus.emit_artifact_event_sync(collection_id, "artifact.created", {
        "artifact": {**artifact.to_dict(), "collection_id": collection_id}
    })
    return artifact


def archive_artifact_by_version_id(db: StandardDatabase, user_id: str, version_id: str) -> bool:
    """
    Archives an artifact by its version ID.
    """
    artifact = db_get_artifact_by_version_id(db, version_id)
    if not artifact:
        logger.warning("Artifact %s not found for archiving", version_id)
        return False
    return db_archive_artifact(db, user_id, version_id)


# === Commit ===

def create_commit(db: StandardDatabase, user_id: str, commit: CommitEntity) -> CommitEntity:
    """
    Handles explicit commit creation from the API.
    """
    # Validate the user can write to every referenced collection_id in items
    # If CommitOpEntity has collection_id optional in your model, enforce presence for now.
    # touched_collections = set()
    for item_id in getattr(commit, "item_ids", []) or []:
        # If you store items first, this path might be a no-op. Keep API minimal for now.
        # Caller should use record_collection_commit for normal flows.
        pass

    # Minimal envelope write: fill id, timestamp, author then create
    now = datetime.now(timezone.utc).isoformat()
    commit.id = commit.id or str(uuid.uuid4())
    commit.timestamp = now
    commit.author_id = user_id
    db_create_commit(db, commit)
    return commit


def get_commit(db: StandardDatabase, user_id: str, commit_id: str) -> CommitEntity:
    """Fetch a commit by id and convert it to a domain entity."""
    raw = db_get_commit_by_id(db, commit_id)
    if not raw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if isinstance(raw, Mapping):
        data: Dict[str, Any] = dict(cast(Mapping[str, Any], raw))
    else:
        data = cast(Dict[str, Any], raw)

    data.setdefault("id", commit_id)
    data.setdefault("created_time", data.get("timestamp"))
    data.setdefault("modified_time", data.get("timestamp"))
    return CommitEntity.from_dict(data)


def get_commits_for_collection(db: StandardDatabase, user_id: str, collection_id: str) -> List[CommitEntity]:
    """Return commits associated with a collection."""
    records = db_get_commits_for_collection(db, collection_id) or []
    entities: List[CommitEntity] = []
    for raw in records:
        if isinstance(raw, Mapping):
            data = dict(cast(Mapping[str, Any], raw))
        else:
            data = cast(Dict[str, Any], raw)
        commit_id = data.get("id") or data.get("_key") or str(uuid.uuid4())
        data.setdefault("id", commit_id)
        data.setdefault("created_time", data.get("timestamp"))
        data.setdefault("modified_time", data.get("timestamp"))
        entities.append(CommitEntity.from_dict(data))
    return entities

# === Provenance helper ===

def record_collection_commit(
    db: StandardDatabase,
    user_id: str,
    collection_id: str,
    *,
    adds: Optional[List[str]] = None,
    removes: Optional[List[str]] = None,
    message: Optional[str] = None,
    author_id: Optional[str] = None,
    subject_user_id: Optional[str] = None,
    presenter_type: Optional[str] = None,
    presenter_id: Optional[str] = None,
    client_id: Optional[str] = None,
    host_id: Optional[str] = None,
    server_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    confirmation: Optional[str] = None,
    changeset_type: Optional[str] = None,
) -> str:
    """
    Create up to two CommitItems (add/remove) for a single collection and one Commit that references them.
    Returns the commit id.
    """
    adds = list({a for a in (adds or []) if a})
    removes = list({r for r in (removes or []) if r})

    # Build items
    item_ids: List[str] = []
    items: List[CommitItemEntity] = []

    if adds:
        items.append(CommitItemEntity(
            id=str(uuid.uuid4()),
            item_type="add",
            collection_id=collection_id,
            artifact_version_ids=adds,
        ))
    if removes:
        items.append(CommitItemEntity(
            id=str(uuid.uuid4()),
            item_type="remove",
            collection_id=collection_id,
            artifact_version_ids=removes,
        ))

    if items:
        created_item_ids = db_create_commit_items(db, items) or []
        item_ids.extend(created_item_ids)

    commit = CommitEntity(
        id=str(uuid.uuid4()),
        message=message or "",
        timestamp=datetime.now(timezone.utc).isoformat(),
        author_id=author_id or user_id,
        subject_user_id=subject_user_id or user_id,
        presenter_type=presenter_type,
        presenter_id=presenter_id,
        client_id=client_id,
        host_id=host_id,
        server_id=server_id,
        agent_id=agent_id,
        api_key_id=api_key_id,
        confirmation=confirmation or "human_affirmed",
        changeset_type=changeset_type or "manual",
        item_ids=item_ids,
    )
    db_create_commit(db, commit)
    return commit.id



