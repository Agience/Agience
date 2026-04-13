# db/arango.py
# type: ignore[import, attr-defined, assignment, arg-type, union-attr, call-arg, index, misc, return-value, override, var-annotated]
# python-arango library has incomplete type stubs; this file works correctly at runtime.
"""
ArangoDB client wrapper for Agience.

Unified artifact store: one `artifacts` collection, one `collections`
collection (with `content_type` discriminator). See
`.dev/features/unified-artifact-store.md`.

All ordering lives on the `collection_artifacts` edge (`order_key`). The edge
`_to` always points at `artifacts/{root_id}` — the stable root document —
never at a specific version.
"""
import logging
from typing import Any, Dict, Optional, Type, TypeVar, List, Tuple
from pydantic import BaseModel
from datetime import datetime

from arango.database import StandardDatabase
from arango.exceptions import DocumentInsertError, DocumentUpdateError, DocumentDeleteError, DocumentGetError, DocumentReplaceError

from entities.artifact import Artifact as ArtifactEntity
from entities.collection import Collection as CollectionEntity
from entities.api_key import APIKey as APIKeyEntity
from entities.commit import Commit as CommitEntity
from entities.grant import Grant as GrantEntity
from entities.server_credential import ServerCredential as ServerCredentialEntity

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------- Collection names ----------
COLLECTION_ARTIFACTS = "artifacts"
COLLECTION_COLLECTIONS = "collections"
COLLECTION_GRANTS = "grants"
COLLECTION_API_KEYS = "api_keys"
COLLECTION_SERVER_CREDENTIALS = "server_credentials"
COLLECTION_COMMITS = "commits"
COLLECTION_COMMIT_ITEMS = "commit_items"
COLLECTION_COLLECTION_ARTIFACTS = "collection_artifacts"  # Edge collection
COLLECTION_SERVER_KEYS = "server_keys"

GRAPH_NAME = "agience_graph"


# ============================================================
#  Fractional index helpers (base-62, lexicographic)
# ------------------------------------------------------------
#  Ordering lives on `collection_artifacts` edges. Use these to
#  pick a new `order_key` when inserting or reordering.
# ============================================================

_ALPH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def after_key(a: Optional[str]) -> str:
    """Return a key strictly greater than *a* (or 'U' if None)."""
    if not a:
        return "U"
    last = _ALPH.find(a[-1])
    if last == -1 or last == len(_ALPH) - 1:
        return a + "U"
    return a[:-1] + _ALPH[last + 1]


def mid_key(a: Optional[str], b: Optional[str]) -> str:
    """Return a key strictly between (a, b)."""
    pad = "U"
    if a is None and b is None:
        return pad
    if a is None:
        a = ""
    if b is None:
        return a + pad
    i = 0
    while True:
        ca = _ALPH.find(a[i]) if i < len(a) else _ALPH.find(pad)
        cb = _ALPH.find(b[i]) if i < len(b) else len(_ALPH) - 1
        if ca + 1 < cb:
            return (a[:i] if i < len(a) else a) + _ALPH[(ca + cb) // 2]
        i += 1
        if i > max(len(a), len(b)) + 4:
            return a + _ALPH[1]


# ============================================================
#  Generic helpers
# ============================================================

def _strip_nones(obj: Any) -> Any:
    """Remove None values from dicts and lists."""
    if isinstance(obj, dict):
        return {k: _strip_nones(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nones(v) for v in obj if v is not None]
    return obj


def _get_collection_name(entity_type: str) -> str:
    mapping = {
        "Artifact": COLLECTION_ARTIFACTS,
        # Collection is now an alias for Artifact — both go to artifacts.
        "Collection": COLLECTION_ARTIFACTS,
        "Grant": COLLECTION_GRANTS,
        "Commit": COLLECTION_COMMITS,
        "CommitItem": COLLECTION_COMMIT_ITEMS,
        "CollectionArtifact": COLLECTION_COLLECTION_ARTIFACTS,
    }
    return mapping.get(entity_type, entity_type.lower() + "s")


def _serialize_datetime(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: _serialize_datetime(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_serialize_datetime(item) for item in obj]
    return obj


def to_arango_doc(entity) -> dict:
    """Convert entity → ArangoDB document (with `_key` and `_type`)."""
    ent_type: Optional[str] = getattr(entity, "PREFIX", None)

    if hasattr(entity, "to_dict") and callable(getattr(entity, "to_dict")):
        doc = entity.to_dict()
    elif isinstance(entity, BaseModel):
        doc = entity.model_dump(exclude_none=True, by_alias=True) if hasattr(entity, "model_dump") \
              else entity.dict(exclude_none=True, by_alias=True)
    elif isinstance(entity, dict):
        doc = dict(entity)
    else:
        doc = {k: v for k, v in entity.__dict__.items() if not k.startswith("_")}

    if not ent_type:
        raise ValueError("to_arango_doc: unable to resolve entity type (PREFIX)")

    doc = _strip_nones(doc)

    if "id" in doc:
        doc["_key"] = doc.pop("id")

    doc["_type"] = ent_type
    doc = _serialize_datetime(doc)
    return doc


def from_arango_doc(raw: dict, cls: Type[T]) -> Optional[T]:
    if not raw:
        return None
    raw["id"] = raw.get("_key", raw.get("_id", "").split("/")[-1])
    return cls.from_dict(raw)


# ============================================================
#  Generic CRUD
# ============================================================

def insert_document(db: StandardDatabase, entity, collection_name: Optional[str] = None) -> object:
    doc = to_arango_doc(entity)
    if not collection_name:
        collection_name = _get_collection_name(doc.get("_type", "unknown"))

    try:
        collection = db.collection(collection_name)
        result = collection.insert(doc)
        logger.info("Inserted %s: %s", doc["_type"], result.get("_key", "<generated>"))
        return entity
    except DocumentInsertError as e:
        logger.error("Failed to insert %s: %s", doc.get("_type"), e)
        raise


def replace_document(db: StandardDatabase, entity, collection_name: Optional[str] = None) -> Optional[object]:
    doc = to_arango_doc(entity)
    if not collection_name:
        collection_name = _get_collection_name(doc.get("_type", "unknown"))

    try:
        collection = db.collection(collection_name)
        collection.replace(doc)
        logger.info("Updated %s: %s", doc["_type"], doc.get("_key", "<no-key>"))
        return entity
    except (DocumentReplaceError, DocumentUpdateError) as e:
        logger.error("Failed to update %s %s: %s", doc.get("_type"), doc.get("_key"), e)
        return None


def delete_document(db: StandardDatabase, collection_name: str, key: str) -> bool:
    try:
        collection = db.collection(collection_name)
        collection.delete(key)
        logger.info("Deleted %s: %s", collection_name, key)
        return True
    except DocumentDeleteError as e:
        logger.error("Failed to delete %s %s: %s", collection_name, key, e)
        return False


def get_document_by_key(db: StandardDatabase, cls: Type[T], collection_name: str, key: str) -> Optional[T]:
    try:
        collection = db.collection(collection_name)
        raw = collection.get(key)
        return from_arango_doc(raw, cls)
    except DocumentGetError:
        return None


def query_documents(db: StandardDatabase, cls: Type[T], collection_name: str, filters: dict) -> List[T]:
    try:
        filter_clauses = []
        bind_vars = {"@collection": collection_name}

        for i, (key, value) in enumerate(filters.items()):
            param_name = f"param{i}"
            filter_clauses.append(f"doc.{key} == @{param_name}")
            bind_vars[param_name] = value

        filter_str = " AND ".join(filter_clauses) if filter_clauses else "true"
        aql = f"FOR doc IN @@collection FILTER {filter_str} RETURN doc"

        cursor = db.aql.execute(aql, bind_vars=bind_vars)
        return [from_arango_doc(raw, cls) for raw in cursor]
    except Exception as e:
        logger.error("Failed to query %s: %s", collection_name, e)
        return []


# ============================================================
#  Collections (containers: workspace or otherwise)
#  Container-as-artifact: all containers now live in `artifacts`.
# ============================================================

def create_collection(db: StandardDatabase, entity: CollectionEntity) -> CollectionEntity:
    return insert_document(db, entity, COLLECTION_ARTIFACTS)


def get_collection_by_id(db: StandardDatabase, id: str) -> Optional[CollectionEntity]:
    return get_document_by_key(db, CollectionEntity, COLLECTION_ARTIFACTS, id)


def get_collections_by_owner_id(db: StandardDatabase, owner_id: str) -> List[CollectionEntity]:
    return get_containers_for_user(db, owner_id)


def get_collections_by_owner_and_type(
    db: StandardDatabase, owner_id: str, content_type: str
) -> List[CollectionEntity]:
    return get_containers_for_user(db, owner_id, content_type)


def update_collection(db: StandardDatabase, entity: CollectionEntity) -> Optional[CollectionEntity]:
    return replace_document(db, entity, COLLECTION_ARTIFACTS)


def delete_collection(db: StandardDatabase, id: str) -> bool:
    return delete_document(db, COLLECTION_ARTIFACTS, id)


def get_collection_by_slug(db: StandardDatabase, slug: str) -> Optional[CollectionEntity]:
    try:
        cursor = db.aql.execute(
            "FOR c IN @@col FILTER c.slug == @slug AND c.content_type != null LIMIT 1 RETURN c",
            bind_vars={"@col": COLLECTION_ARTIFACTS, "slug": slug},
        )
        for doc in cursor:
            return from_arango_doc(doc, CollectionEntity)
        return None
    except Exception as e:
        logger.error("Failed slug lookup in %s for slug=%s: %s", COLLECTION_ARTIFACTS, slug, e)
        return None


def get_containers_for_user(
    db: StandardDatabase,
    user_id: str,
    content_type: Optional[str] = None,
) -> List[CollectionEntity]:
    """
    Query artifacts that are containers (have content_type set) owned by user_id.
    Optionally filter to a specific content_type.
    """
    try:
        if content_type:
            aql = """
            FOR a IN @@col
              FILTER a.created_by == @uid AND a.content_type == @ct
              RETURN a
            """
            bind_vars = {"@col": COLLECTION_ARTIFACTS, "uid": user_id, "ct": content_type}
        else:
            aql = """
            FOR a IN @@col
              FILTER a.created_by == @uid AND a.content_type != null
              RETURN a
            """
            bind_vars = {"@col": COLLECTION_ARTIFACTS, "uid": user_id}
        cursor = db.aql.execute(aql, bind_vars=bind_vars)
        return [from_arango_doc(row, CollectionEntity) for row in cursor if row]
    except Exception as e:
        logger.error("get_containers_for_user(%s) failed: %s", user_id, e)
        return []


# ============================================================
#  Artifacts (unified — draft, committed, archived)
# ============================================================

def create_artifact(db: StandardDatabase, entity: ArtifactEntity) -> ArtifactEntity:
    return insert_document(db, entity, COLLECTION_ARTIFACTS)


def create_artifacts_batch(db: StandardDatabase, entities: List[ArtifactEntity]) -> List[ArtifactEntity]:
    if not entities:
        return []
    docs = [to_arango_doc(e) for e in entities]
    try:
        collection = db.collection(COLLECTION_ARTIFACTS)
        collection.insert_many(docs)
        logger.info("Batch inserted %d Artifact docs", len(docs))
        return entities
    except Exception as e:
        logger.error("Failed batch inserting Artifact docs: %s", e)
        out: List[ArtifactEntity] = []
        for e2 in entities:
            try:
                insert_document(db, e2, COLLECTION_ARTIFACTS)
                out.append(e2)
            except Exception:
                logger.exception("Failed inserting Artifact %s", getattr(e2, "id", "<unknown>"))
        return out


def get_artifact(db: StandardDatabase, artifact_id: str) -> Optional[ArtifactEntity]:
    """Fetch a single artifact version by its id (version id)."""
    return get_document_by_key(db, ArtifactEntity, COLLECTION_ARTIFACTS, artifact_id)


def get_artifact_by_slug(db: StandardDatabase, slug: str) -> Optional[ArtifactEntity]:
    try:
        cursor = db.aql.execute(
            "FOR a IN @@col FILTER a.slug == @slug LIMIT 1 RETURN a",
            bind_vars={"@col": COLLECTION_ARTIFACTS, "slug": slug},
        )
        for doc in cursor:
            return from_arango_doc(doc, ArtifactEntity)
        return None
    except Exception as e:
        logger.error("Failed slug lookup in %s for slug=%s: %s", COLLECTION_ARTIFACTS, slug, e)
        return None


def find_artifact_by_slug_in_collection(
    db: StandardDatabase, collection_id: str, slug: str,
) -> Optional[ArtifactEntity]:
    """Find a non-archived artifact by slug within a specific collection.

    Returns the draft-preferred version if multiple exist for the same slug.
    """
    try:
        cursor = db.aql.execute(
            """
            FOR a IN @@col
              FILTER a.collection_id == @cid
                 AND a.slug == @slug
                 AND a.state != "archived"
              SORT a.state == "draft" ? 0 : 1, a.created_time DESC
              LIMIT 1
              RETURN a
            """,
            bind_vars={"@col": COLLECTION_ARTIFACTS, "cid": collection_id, "slug": slug},
        )
        for doc in cursor:
            return from_arango_doc(doc, ArtifactEntity)
        return None
    except Exception as e:
        logger.error(
            "find_artifact_by_slug_in_collection(%s, %s) failed: %s",
            collection_id, slug, e,
        )
        return None


def get_artifacts_by_creator_id(db: StandardDatabase, creator_id: str) -> List[ArtifactEntity]:
    try:
        cursor = db.aql.execute(
            """
            FOR a IN @@col
              FILTER a.created_by == @creator
                 AND a.state != "archived"
              RETURN a
            """,
            bind_vars={"@col": COLLECTION_ARTIFACTS, "creator": creator_id},
        )
        return [from_arango_doc(row, ArtifactEntity) for row in cursor if row]
    except Exception as e:
        logger.error("get_artifacts_by_creator_id failed: %s", e)
        return []


def find_artifact_by_context_field(
    db: StandardDatabase,
    field_name: str,
    field_value: str,
    content_type: Optional[str] = None,
) -> Optional[ArtifactEntity]:
    """
    Find a non-archived artifact whose JSON context contains a field matching a value.

    Resolution: prefer the draft-or-latest-committed version. `content_type` here
    refers to `context.content_type` (the MIME type of the payload), not the
    container's content_type.
    """
    parts = [
        f"FOR doc IN {COLLECTION_ARTIFACTS}",
        "  FILTER doc.state != \"archived\"",
        "  LET ctx = IS_STRING(doc.context) ? JSON_PARSE(doc.context) : (doc.context || {})",
        f"  FILTER ctx.{field_name} == @field_value",
    ]
    bind_vars: dict = {"field_value": field_value}

    if content_type:
        parts.append("  FILTER ctx.content_type == @content_type")
        bind_vars["content_type"] = content_type

    # Prefer draft → latest committed.
    parts.append("  SORT doc.state == 'draft' ? 0 : 1, doc.created_time DESC")
    parts.append("  LIMIT 1")
    parts.append("  RETURN doc")

    aql = "\n".join(parts)
    try:
        cursor = db.aql.execute(aql, bind_vars=bind_vars)
        results = list(cursor)
        if results:
            return from_arango_doc(results[0], ArtifactEntity)
        return None
    except Exception as e:
        logger.error("find_artifact_by_context_field(%s=%s): %s", field_name, field_value, e)
        return None


def get_draft_artifact(
    db: StandardDatabase, root_id: str, collection_id: str
) -> Optional[ArtifactEntity]:
    """Return the single draft record for (root_id, collection_id) if any."""
    try:
        cursor = db.aql.execute(
            """
            FOR a IN @@col
              FILTER a.root_id == @root
                 AND a.collection_id == @cid
                 AND a.state == "draft"
              LIMIT 1
              RETURN a
            """,
            bind_vars={"@col": COLLECTION_ARTIFACTS, "root": root_id, "cid": collection_id},
        )
        for row in cursor:
            return from_arango_doc(row, ArtifactEntity)
        return None
    except Exception as e:
        logger.error("get_draft_artifact(%s,%s) failed: %s", root_id, collection_id, e)
        return None


def get_latest_committed_artifact(
    db: StandardDatabase,
    root_id: str,
    collection_id: Optional[str] = None,
) -> Optional[ArtifactEntity]:
    """
    Return the newest committed version for *root_id*. If *collection_id* is
    given, restrict to that collection; otherwise return the latest committed
    version globally (used when resolving a published edge in another collection).
    """
    try:
        if collection_id:
            aql = """
            FOR a IN @@col
              FILTER a.root_id == @root
                 AND a.collection_id == @cid
                 AND a.state == "committed"
              SORT a.created_time DESC
              LIMIT 1
              RETURN a
            """
            bind_vars = {"@col": COLLECTION_ARTIFACTS, "root": root_id, "cid": collection_id}
        else:
            aql = """
            FOR a IN @@col
              FILTER a.root_id == @root AND a.state == "committed"
              SORT a.created_time DESC
              LIMIT 1
              RETURN a
            """
            bind_vars = {"@col": COLLECTION_ARTIFACTS, "root": root_id}

        cursor = db.aql.execute(aql, bind_vars=bind_vars)
        for row in cursor:
            return from_arango_doc(row, ArtifactEntity)
        return None
    except Exception as e:
        logger.error("get_latest_committed_artifact(%s) failed: %s", root_id, e)
        return None


def get_current_in_collection(
    db: StandardDatabase, collection_id: str, root_id: str
) -> Optional[ArtifactEntity]:
    """
    Resolve the *current* artifact for a (collection, root_id): the draft
    if one exists, otherwise the latest committed version in this collection.
    """
    draft = get_draft_artifact(db, root_id, collection_id)
    if draft:
        return draft
    return get_latest_committed_artifact(db, root_id, collection_id)


def list_collection_artifacts(
    db: StandardDatabase,
    collection_id: str,
    *,
    include_archived: bool = False,
) -> List[Dict[str, Any]]:
    """
    Resolve a collection's contents using the edge table.

    For each edge in `collection_artifacts`, return the current version of
    its root — draft-preferred in the *same* collection, else the latest
    committed version in the same collection, else the latest committed
    version globally (for published edges from other collections).

    Each returned dict is the Artifact dict with two added keys:
      - `order_key`: from the edge
      - `has_committed_version`: bool — whether any committed version exists
        for this root_id (used by frontend to distinguish "delete" vs "revert").
    """
    archived_filter = "" if include_archived else 'FILTER (current.state != "archived")'
    aql = f"""
    LET cdoc = DOCUMENT(@collection_doc_id)
    FOR v, e IN 1..1 OUTBOUND cdoc {COLLECTION_COLLECTION_ARTIFACTS}
      LET current = FIRST(
        FOR a IN {COLLECTION_ARTIFACTS}
          FILTER a.root_id == e.root_id AND a.collection_id == @cid
          SORT a.state == "draft" ? 0 : 1, a.created_time DESC
          LIMIT 1
          RETURN a
      )
      LET fallback = current == null ? FIRST(
        FOR a IN {COLLECTION_ARTIFACTS}
          FILTER a.root_id == e.root_id AND a.state == "committed"
          SORT a.created_time DESC
          LIMIT 1
          RETURN a
      ) : null
      LET chosen = current == null ? fallback : current
      FILTER chosen != null
      LET committed_count = LENGTH(
        FOR a IN {COLLECTION_ARTIFACTS}
          FILTER a.root_id == e.root_id AND a.state == "committed"
          LIMIT 1
          RETURN 1
      )
      {archived_filter.replace('current', 'chosen')}
      SORT e.order_key
      RETURN MERGE(chosen, {{
        order_key: e.order_key,
        has_committed_version: committed_count > 0
      }})
    """
    try:
        cursor = db.aql.execute(
            aql,
            bind_vars={
                "collection_doc_id": f"{COLLECTION_ARTIFACTS}/{collection_id}",
                "cid": collection_id,
            },
        )
        out: List[Dict[str, Any]] = []
        for raw in cursor:
            raw["id"] = raw.get("_key", raw.get("id"))
            raw.pop("_key", None)
            raw.pop("_id", None)
            raw.pop("_rev", None)
            raw.pop("_type", None)
            out.append(raw)
        return out
    except Exception as e:
        logger.error("list_collection_artifacts(%s) failed: %s", collection_id, e)
        return []


def list_version_history(db: StandardDatabase, root_id: str) -> List[ArtifactEntity]:
    """Return all committed versions for a root_id, newest first."""
    try:
        cursor = db.aql.execute(
            """
            FOR a IN @@col
              FILTER a.root_id == @root AND a.state == "committed"
              SORT a.created_time DESC
              RETURN a
            """,
            bind_vars={"@col": COLLECTION_ARTIFACTS, "root": root_id},
        )
        return [from_arango_doc(row, ArtifactEntity) for row in cursor if row]
    except Exception as e:
        logger.error("list_version_history(%s) failed: %s", root_id, e)
        return []


def list_draft_artifacts(db: StandardDatabase, collection_id: str) -> List[ArtifactEntity]:
    """Return every draft artifact in a collection (used by commit)."""
    try:
        cursor = db.aql.execute(
            """
            FOR a IN @@col
              FILTER a.collection_id == @cid AND a.state == "draft"
              RETURN a
            """,
            bind_vars={"@col": COLLECTION_ARTIFACTS, "cid": collection_id},
        )
        return [from_arango_doc(row, ArtifactEntity) for row in cursor if row]
    except Exception as e:
        logger.error("list_draft_artifacts(%s) failed: %s", collection_id, e)
        return []


def update_artifact(db: StandardDatabase, entity: ArtifactEntity) -> Optional[ArtifactEntity]:
    return replace_document(db, entity, COLLECTION_ARTIFACTS)


def batch_commit_drafts(
    db: StandardDatabase,
    collection_id: str,
    artifact_ids: List[str],
    committed_by: str,
    committed_time: str,
) -> int:
    """
    Flip a batch of drafts in a collection to `committed` in a single AQL call.
    Returns the number of documents updated.
    """
    if not artifact_ids:
        return 0
    try:
        cursor = db.aql.execute(
            """
            FOR a IN @@col
              FILTER a.collection_id == @cid
                 AND a._key IN @ids
                 AND a.state == "draft"
              UPDATE a WITH {
                state: "committed",
                modified_by: @by,
                modified_time: @when
              } IN @@col
              RETURN NEW._key
            """,
            bind_vars={
                "@col": COLLECTION_ARTIFACTS,
                "cid": collection_id,
                "ids": artifact_ids,
                "by": committed_by,
                "when": committed_time,
            },
        )
        updated = list(cursor)
        logger.info("Committed %d drafts in collection %s", len(updated), collection_id)
        return len(updated)
    except Exception as e:
        logger.error("batch_commit_drafts(%s) failed: %s", collection_id, e)
        return 0


def delete_artifact(db: StandardDatabase, artifact_id: str) -> bool:
    """Hard-delete a single artifact version document."""
    return delete_document(db, COLLECTION_ARTIFACTS, artifact_id)


def delete_artifacts_by_root(db: StandardDatabase, root_id: str) -> List[str]:
    """Hard-delete every version (draft + committed + archived) with this root."""
    try:
        cursor = db.aql.execute(
            f"""
            FOR a IN {COLLECTION_ARTIFACTS}
              FILTER a.root_id == @root
              REMOVE a IN {COLLECTION_ARTIFACTS}
              RETURN OLD._key
            """,
            bind_vars={"root": root_id},
        )
        deleted = list(cursor)
        logger.info("Deleted %d artifact docs for root %s", len(deleted), root_id)
        return deleted
    except Exception as e:
        logger.error("delete_artifacts_by_root(%s) failed: %s", root_id, e)
        return []


def archive_artifact(db: StandardDatabase, user_id: str, artifact_id: str) -> bool:
    """Mark an artifact as archived (soft delete). Owner check via `created_by`."""
    try:
        artifact = get_artifact(db, artifact_id)
        if not artifact:
            logger.warning("Artifact %s not found for archiving", artifact_id)
            return False

        if getattr(artifact, "created_by", None) != user_id:
            logger.warning(
                "User %s attempted to archive artifact %s owned by %s",
                user_id, artifact_id, getattr(artifact, "created_by", "unknown"),
            )
            return False

        artifact.state = ArtifactEntity.STATE_ARCHIVED
        replace_document(db, artifact, COLLECTION_ARTIFACTS)
        logger.info("Archived artifact: %s", artifact_id)
        return True
    except Exception as e:
        logger.error("Failed to archive artifact %s: %s", artifact_id, e)
        return False


# ============================================================
#  Collection membership edges
# ------------------------------------------------------------
#  `collection_artifacts` edges:
#    _from:     artifacts/{collection_id}  (container artifact)
#    _to:       artifacts/{root_id}        (stable, never updated)
#    root_id:   redundant mirror of _to for AQL convenience
#    order_key: fractional index for listing order
# ============================================================

def _edge_key(collection_id: str, root_id: str) -> str:
    """Deterministic edge key — makes upserts idempotent."""
    return f"{collection_id}__{root_id}"


def get_edge(db: StandardDatabase, collection_id: str, root_id: str) -> Optional[dict]:
    try:
        coll = db.collection(COLLECTION_COLLECTION_ARTIFACTS)
        raw = coll.get(_edge_key(collection_id, root_id))
        return raw
    except Exception:
        return None


def get_last_order_key(db: StandardDatabase, collection_id: str) -> Optional[str]:
    """Return the maximum `order_key` currently used in this collection."""
    try:
        cursor = db.aql.execute(
            """
            FOR e IN @@edges
              FILTER e._from == @from
              SORT e.order_key DESC
              LIMIT 1
              RETURN e.order_key
            """,
            bind_vars={
                "@edges": COLLECTION_COLLECTION_ARTIFACTS,
                "from": f"{COLLECTION_ARTIFACTS}/{collection_id}",
            },
        )
        for v in cursor:
            return v
        return None
    except Exception as e:
        logger.error("get_last_order_key(%s) failed: %s", collection_id, e)
        return None


def add_artifact_to_collection(
    db: StandardDatabase,
    collection_id: str,
    root_id: str,
    order_key: Optional[str] = None,
) -> bool:
    """
    Insert or upsert a `collection_artifacts` edge pointing at
    `artifacts/{root_id}`. If *order_key* is not given, places the new
    edge at the end (after the current max key).

    """
    if order_key is None:
        order_key = after_key(get_last_order_key(db, collection_id))

    edge = {
        "_key": _edge_key(collection_id, root_id),
        "_from": f"{COLLECTION_ARTIFACTS}/{collection_id}",
        "_to": f"{COLLECTION_ARTIFACTS}/{root_id}",
        "_type": "CollectionArtifact",
        "root_id": root_id,
        "order_key": order_key,
    }
    try:
        coll = db.collection(COLLECTION_COLLECTION_ARTIFACTS)
        coll.insert(edge, overwrite=True)
        return True
    except Exception as e:
        logger.error("add_artifact_to_collection(%s,%s) failed: %s", collection_id, root_id, e)
        return False


def add_artifacts_to_collection_batch(
    db: StandardDatabase,
    collection_id: str,
    root_id_order_pairs: List[Tuple[str, str]],
) -> bool:
    """Batch insert/upsert edges. Pairs are (root_id, order_key)."""
    if not root_id_order_pairs:
        return True
    try:
        coll = db.collection(COLLECTION_COLLECTION_ARTIFACTS)
        edges = [
            {
                "_key": _edge_key(collection_id, rid),
                "_from": f"{COLLECTION_ARTIFACTS}/{collection_id}",
                "_to": f"{COLLECTION_ARTIFACTS}/{rid}",
                "_type": "CollectionArtifact",
                "root_id": rid,
                "order_key": ok,
            }
            for (rid, ok) in root_id_order_pairs
        ]
        coll.insert_many(edges, overwrite=True)
        return True
    except Exception as e:
        logger.error("add_artifacts_to_collection_batch(%s) failed: %s", collection_id, e)
        ok_all = True
        for rid, order_key in root_id_order_pairs:
            if not add_artifact_to_collection(db, collection_id, rid, order_key):
                ok_all = False
        return ok_all


def remove_artifact_from_collection(
    db: StandardDatabase, collection_id: str, root_id: str
) -> bool:
    """Delete the edge for (collection_id, root_id). Idempotent."""
    try:
        coll = db.collection(COLLECTION_COLLECTION_ARTIFACTS)
        key = _edge_key(collection_id, root_id)
        if coll.has(key):
            coll.delete(key)
        return True
    except Exception as e:
        logger.error("remove_artifact_from_collection(%s,%s) failed: %s", collection_id, root_id, e)
        return False


def remove_all_edges_for_root(db: StandardDatabase, root_id: str) -> int:
    """Delete every edge that points at `artifacts/{root_id}`. Returns count."""
    try:
        cursor = db.aql.execute(
            f"""
            FOR e IN {COLLECTION_COLLECTION_ARTIFACTS}
              FILTER e.root_id == @root
              REMOVE e IN {COLLECTION_COLLECTION_ARTIFACTS}
              RETURN OLD._key
            """,
            bind_vars={"root": root_id},
        )
        return len(list(cursor))
    except Exception as e:
        logger.error("remove_all_edges_for_root(%s) failed: %s", root_id, e)
        return 0


def set_edge_order_key(
    db: StandardDatabase, collection_id: str, root_id: str, new_order_key: str
) -> bool:
    """Update the order_key on a single edge."""
    try:
        coll = db.collection(COLLECTION_COLLECTION_ARTIFACTS)
        coll.update({"_key": _edge_key(collection_id, root_id), "order_key": new_order_key})
        return True
    except Exception as e:
        logger.error("set_edge_order_key(%s,%s) failed: %s", collection_id, root_id, e)
        return False


def reorder_collection_artifacts(
    db: StandardDatabase, collection_id: str, ordered_root_ids: List[str]
) -> int:
    """
    Assign monotonically increasing order_keys to a sequence of root_ids
    (used by `PATCH /artifacts/{id}/order` when the frontend sends the
    full ordering). Returns the number of edges updated.
    """
    if not ordered_root_ids:
        return 0
    try:
        coll = db.collection(COLLECTION_COLLECTION_ARTIFACTS)
        updated = 0
        prev: Optional[str] = None
        for rid in ordered_root_ids:
            key = after_key(prev)
            coll.update({"_key": _edge_key(collection_id, rid), "order_key": key})
            prev = key
            updated += 1
        return updated
    except Exception as e:
        logger.error("reorder_collection_artifacts(%s) failed: %s", collection_id, e)
        return 0


def batch_get_collection_ids_for_roots(
    db: StandardDatabase, root_ids: List[str]
) -> Dict[str, List[str]]:
    """Return {root_id: [collection_id, ...]} in one pass over edges. Excludes workspaces."""
    if not root_ids:
        return {}
    try:
        cursor = db.aql.execute(
            f"""
            FOR e IN {COLLECTION_COLLECTION_ARTIFACTS}
              FILTER e.root_id IN @roots
              LET col_doc = DOCUMENT(e._from)
              FILTER col_doc.content_type != @workspace_type
              RETURN {{ root: e.root_id, col: SUBSTRING(e._from, LENGTH("{COLLECTION_ARTIFACTS}/")) }}
            """,
            bind_vars={"roots": root_ids, "workspace_type": "application/vnd.agience.workspace+json"},
        )
        out: Dict[str, List[str]] = {r: [] for r in root_ids}
        seen: Dict[str, set] = {r: set() for r in root_ids}
        for row in cursor:
            r = row.get("root")
            c = row.get("col")
            if r in out and c and c not in seen[r]:
                out[r].append(c)
                seen[r].add(c)
        return out
    except Exception as e:
        logger.error("batch_get_collection_ids_for_roots failed: %s", e)
        return {r: [] for r in root_ids}


def get_collection_ids_for_root(db: StandardDatabase, root_id: str) -> List[str]:
    """Return every collection (excluding workspaces) that has an edge to this root."""
    try:
        cursor = db.aql.execute(
            f"""
            FOR e IN {COLLECTION_COLLECTION_ARTIFACTS}
              FILTER e.root_id == @root
              LET col_doc = DOCUMENT(e._from)
              FILTER col_doc.content_type != @workspace_type
              RETURN DISTINCT SUBSTRING(e._from, LENGTH("{COLLECTION_ARTIFACTS}/"))
            """,
            bind_vars={"root": root_id, "workspace_type": "application/vnd.agience.workspace+json"},
        )
        return [v for v in cursor if v]
    except Exception as e:
        logger.error("get_collection_ids_for_root(%s) failed: %s", root_id, e)
        return []


# ============================================================
#  API Keys
# ============================================================

def create_api_key(db: StandardDatabase, entity: APIKeyEntity) -> APIKeyEntity:
    return insert_document(db, entity, COLLECTION_API_KEYS)


def get_api_key_by_hash(db: StandardDatabase, key_hash: str) -> Optional[APIKeyEntity]:
    try:
        result = db.aql.execute(
            """
            FOR key IN @@collection
                FILTER key.key_hash == @key_hash
                FILTER key.is_active == true
                LIMIT 1
                RETURN key
            """,
            bind_vars={"@collection": COLLECTION_API_KEYS, "key_hash": key_hash},
        )
        for doc in result:
            if doc.get("expires_at"):
                expires_at = datetime.fromisoformat(doc["expires_at"].replace("Z", "+00:00"))
                if expires_at < datetime.now(expires_at.tzinfo):
                    return None
            return APIKeyEntity.from_dict(doc)
        return None
    except Exception as e:
        logger.error(f"Error getting API key by hash: {e}")
        return None


def get_api_key_by_id(db: StandardDatabase, id: str) -> Optional[APIKeyEntity]:
    return get_document_by_key(db, APIKeyEntity, COLLECTION_API_KEYS, id)


def get_api_keys_by_user(db: StandardDatabase, user_id: str) -> List[APIKeyEntity]:
    return query_documents(db, APIKeyEntity, COLLECTION_API_KEYS, {"user_id": user_id})


def update_api_key(db: StandardDatabase, entity: APIKeyEntity) -> Optional[APIKeyEntity]:
    return replace_document(db, entity, COLLECTION_API_KEYS)


def update_api_key_last_used(db: StandardDatabase, key_id: str, timestamp: str) -> bool:
    try:
        db.aql.execute(
            """
            FOR key IN @@collection
                FILTER key._key == @key_id
                UPDATE key WITH { last_used_at: @timestamp } IN @@collection
            """,
            bind_vars={
                "@collection": COLLECTION_API_KEYS,
                "key_id": key_id.split("/")[-1],
                "timestamp": timestamp,
            },
        )
        return True
    except Exception as e:
        logger.error(f"Error updating API key last_used_at: {e}")
        return False


def delete_api_key(db: StandardDatabase, id: str) -> bool:
    return delete_document(db, COLLECTION_API_KEYS, id)


# ============================================================
#  Server Credentials
# ============================================================

def create_server_credential(db: StandardDatabase, entity: ServerCredentialEntity) -> ServerCredentialEntity:
    return insert_document(db, entity, COLLECTION_SERVER_CREDENTIALS)


def get_server_credential_by_client_id(db: StandardDatabase, client_id: str) -> Optional[ServerCredentialEntity]:
    try:
        result = db.aql.execute(
            """
            FOR cred IN @@collection
                FILTER cred.client_id == @client_id
                FILTER cred.is_active == true
                LIMIT 1
                RETURN cred
            """,
            bind_vars={
                "@collection": COLLECTION_SERVER_CREDENTIALS,
                "client_id": client_id,
            },
        )
        for doc in result:
            return ServerCredentialEntity.from_dict(doc)
        return None
    except Exception as e:
        logger.error("Error getting server credential by client_id: %s", e)
        return None


def get_server_credential_by_id(db: StandardDatabase, id: str) -> Optional[ServerCredentialEntity]:
    return get_document_by_key(db, ServerCredentialEntity, COLLECTION_SERVER_CREDENTIALS, id)


def get_server_credentials_by_user(db: StandardDatabase, user_id: str) -> List[ServerCredentialEntity]:
    return query_documents(db, ServerCredentialEntity, COLLECTION_SERVER_CREDENTIALS, {"user_id": user_id})


def get_all_server_credentials(db: StandardDatabase) -> List[ServerCredentialEntity]:
    return query_documents(db, ServerCredentialEntity, COLLECTION_SERVER_CREDENTIALS, {})


def update_server_credential(db: StandardDatabase, entity: ServerCredentialEntity) -> Optional[ServerCredentialEntity]:
    return replace_document(db, entity, COLLECTION_SERVER_CREDENTIALS)


def update_server_credential_last_used(db: StandardDatabase, cred_id: str, timestamp: str) -> bool:
    try:
        db.aql.execute(
            """
            FOR cred IN @@collection
                FILTER cred._key == @cred_id
                UPDATE cred WITH { last_used_at: @timestamp } IN @@collection
            """,
            bind_vars={
                "@collection": COLLECTION_SERVER_CREDENTIALS,
                "cred_id": cred_id.split("/")[-1],
                "timestamp": timestamp,
            },
        )
        return True
    except Exception as e:
        logger.error("Error updating server credential last_used_at: %s", e)
        return False


def delete_server_credential(db: StandardDatabase, id: str) -> bool:
    return delete_document(db, COLLECTION_SERVER_CREDENTIALS, id)


# ============================================================
#  Grants
# ============================================================

def create_grant(db: StandardDatabase, entity: GrantEntity) -> GrantEntity:
    return insert_document(db, entity, COLLECTION_GRANTS)


def get_grant_by_id(db: StandardDatabase, grant_id: str) -> Optional[GrantEntity]:
    return get_document_by_key(db, GrantEntity, COLLECTION_GRANTS, grant_id)


def get_active_grants_for_principal_resource(
    db: StandardDatabase,
    grantee_id: str,
    resource_type: str,
    resource_id: str,
) -> List[GrantEntity]:
    try:
        cursor = db.aql.execute(
            """
            FOR g IN @@col
              FILTER g.grantee_id == @grantee_id
                 AND g.resource_type == @resource_type
                 AND g.resource_id == @resource_id
                 AND g.state == "active"
                 AND (g.expires_at == null OR g.expires_at > DATE_ISO8601(DATE_NOW()))
              RETURN g
            """,
            bind_vars={
                "@col": COLLECTION_GRANTS,
                "grantee_id": grantee_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
        )
        return [from_arango_doc(row, GrantEntity) for row in cursor if row]
    except Exception as e:
        logger.error("get_active_grants_for_principal_resource failed: %s", e)
        return []


def get_active_grants_for_grantee(
    db: StandardDatabase,
    grantee_id: str,
    grantee_type: str = "api_key",
) -> List[GrantEntity]:
    try:
        cursor = db.aql.execute(
            """
            FOR g IN @@col
              FILTER g.grantee_id == @grantee_id
                 AND g.grantee_type == @grantee_type
                 AND g.state == "active"
                 AND (g.expires_at == null OR g.expires_at > DATE_ISO8601(DATE_NOW()))
              RETURN g
            """,
            bind_vars={
                "@col": COLLECTION_GRANTS,
                "grantee_id": grantee_id,
                "grantee_type": grantee_type,
            },
        )
        return [from_arango_doc(row, GrantEntity) for row in cursor if row]
    except Exception as e:
        logger.error("get_active_grants_for_grantee failed: %s", e)
        return []


def get_active_collection_ids_for_user(db: StandardDatabase, user_id: str) -> List[str]:
    try:
        cursor = db.aql.execute(
            """
            FOR g IN @@col
              FILTER g.grantee_id == @user_id
                 AND g.grantee_type == "user"
                 AND g.resource_type == "collection"
                 AND g.state == "active"
                 AND g.can_read == true
                 AND (g.expires_at == null OR g.expires_at > DATE_ISO8601(DATE_NOW()))
              RETURN g.resource_id
            """,
            bind_vars={"@col": COLLECTION_GRANTS, "user_id": user_id},
        )
        return [row for row in cursor if row]
    except Exception as e:
        logger.error("get_active_collection_ids_for_user failed: %s", e)
        return []


def get_grants_for_collection(db: StandardDatabase, collection_id: str) -> List[GrantEntity]:
    return query_documents(
        db, GrantEntity, COLLECTION_GRANTS,
        {"resource_type": "collection", "resource_id": collection_id},
    )


def update_grant(db: StandardDatabase, entity: GrantEntity) -> Optional[GrantEntity]:
    return replace_document(db, entity, COLLECTION_GRANTS)


def upsert_user_collection_grant(
    db: StandardDatabase,
    *,
    user_id: str,
    collection_id: str,
    granted_by: str,
    can_read: bool = True,
    can_update: bool = False,
    can_invoke: bool = False,
    name: Optional[str] = None,
) -> Tuple[GrantEntity, bool]:
    """Upsert a user→collection grant. Returns ``(grant, changed)`` where
    *changed* is ``True`` when a new grant was created or permissions were
    updated, and ``False`` when the existing grant already matched."""
    from datetime import datetime, timezone

    existing_list = get_active_grants_for_principal_resource(
        db, grantee_id=user_id, resource_type="collection", resource_id=collection_id
    )
    if existing_list:
        existing = existing_list[0]
        if existing.can_read == can_read and existing.can_update == can_update and existing.can_invoke == can_invoke:
            return existing, False
        now = datetime.now(timezone.utc).isoformat()
        patch: dict = {
            "can_read": can_read,
            "can_update": can_update,
            "can_invoke": can_invoke,
            "modified_time": now,
        }
        if name is not None:
            patch["name"] = name
        col = db.collection(COLLECTION_GRANTS)
        col.update({"_key": existing.id, **patch})
        existing.can_read = can_read
        existing.can_update = can_update
        existing.can_invoke = can_invoke
        existing.modified_time = now
        return existing, True

    now = datetime.now(timezone.utc).isoformat()
    grant = GrantEntity(
        resource_type="collection",
        resource_id=collection_id,
        grantee_type=GrantEntity.GRANTEE_USER,
        grantee_id=user_id,
        granted_by=granted_by,
        can_read=can_read,
        can_update=can_update,
        can_invoke=can_invoke,
        requires_identity=True,
        state=GrantEntity.STATE_ACTIVE,
        name=name,
        granted_at=now,
        created_time=now,
        modified_time=now,
    )
    return create_grant(db, grant), True


def get_active_grants_by_key(db: StandardDatabase, token: str) -> List[GrantEntity]:
    from services import auth_service

    token_hash = auth_service.hash_api_key(token)
    try:
        cursor = db.aql.execute(
            """
            FOR g IN @@col
              FILTER g.grantee_type == "grant_key"
                 AND g.grantee_id == @token_hash
                 AND g.state == "active"
                 AND (g.expires_at == null OR g.expires_at > DATE_ISO8601(DATE_NOW()))
              RETURN g
            """,
            bind_vars={"@col": COLLECTION_GRANTS, "token_hash": token_hash},
        )
        return [from_arango_doc(row, GrantEntity) for row in cursor if row]
    except Exception as e:
        logger.error("get_active_grants_by_key failed: %s", e)
        return []


def get_active_grant_key_grants_for_collection(db: StandardDatabase, collection_id: str) -> List[GrantEntity]:
    try:
        cursor = db.aql.execute(
            """
            FOR g IN @@col
              FILTER g.resource_type == "collection"
                 AND g.resource_id == @collection_id
                 AND g.grantee_type == "grant_key"
                 AND g.state == "active"
                 AND (g.expires_at == null OR g.expires_at > DATE_ISO8601(DATE_NOW()))
              RETURN g
            """,
            bind_vars={"@col": COLLECTION_GRANTS, "collection_id": collection_id},
        )
        return [from_arango_doc(row, GrantEntity) for row in cursor if row]
    except Exception as e:
        logger.error("get_active_grant_key_grants_for_collection failed: %s", e)
        return []


# ============================================================
#  Commits
# ============================================================

def create_commit(db: StandardDatabase, commit: CommitEntity) -> CommitEntity:
    insert_document(db, commit, COLLECTION_COMMITS)
    return commit


def create_commit_items(db: StandardDatabase, items: List) -> List[str]:
    if not items:
        return []

    collection = db.collection(COLLECTION_COMMIT_ITEMS)
    ids: List[str] = []

    for item in items:
        doc = to_arango_doc(item)
        result = collection.insert(doc)
        ids.append(result.get("_key"))

    logger.info("Created %d commit items", len(ids))
    return ids


def get_commit_by_id(db: StandardDatabase, commit_id: str):
    try:
        collection = db.collection(COLLECTION_COMMITS)
        raw = collection.get(commit_id)
        if raw:
            raw["id"] = raw.get("_key", commit_id)
        return raw
    except Exception:
        return None


def get_commits_for_collection(db: StandardDatabase, collection_id: str) -> List[dict]:
    aql_items = """
    FOR item IN @@items_collection
        FILTER item.collection_id == @collection_id
        RETURN item._key
    """
    cursor = db.aql.execute(aql_items, bind_vars={
        "@items_collection": COLLECTION_COMMIT_ITEMS,
        "collection_id": collection_id
    })
    item_ids = set(cursor)

    aql_commits = f"FOR commit IN {COLLECTION_COMMITS} RETURN commit"
    cursor = db.aql.execute(aql_commits)
    commits = list(cursor)

    out: List[dict] = []
    for c in commits:
        ids = set(c.get("item_ids", []))
        if ids & item_ids:
            c["id"] = c.get("_key", c.get("_id", "").split("/")[-1])
            out.append(c)

    out.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return out


# ============================================================
#  Server JWK registry
# ============================================================

def upsert_server_jwk(db: StandardDatabase, server_client_id: str, public_jwk: dict) -> None:
    coll = db.collection(COLLECTION_SERVER_KEYS)
    doc = {"_key": server_client_id, "public_jwk": public_jwk}
    coll.insert(doc, overwrite=True)


def get_server_jwk(db: StandardDatabase, server_client_id: str) -> Optional[dict]:
    try:
        coll = db.collection(COLLECTION_SERVER_KEYS)
        doc = coll.get(server_client_id)
        if doc:
            return doc.get("public_jwk")
        return None
    except Exception:
        return None
