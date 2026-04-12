# search/ingest/pipeline_unified.py
#
# Unified artifact indexing pipeline.
# Every artifact — regardless of content type — goes through the same path.
# Every artifact lives in a collection. A workspace IS a collection.
# There is no source_type distinction. An artifact is an artifact.

import logging
import time
from typing import Optional, Dict, Any, Tuple

from datetime import datetime, timezone

from entities.artifact import Artifact

from core.embeddings import Embeddings
from db.opensearch import (
    bulk_index_documents,
    delete_by_query,
)
from search.ingest.chunking import (
    chunk_text,
    should_chunk_content,
    extract_text_from_context,
)
from search.ingest.tags import (
    normalize_tags,
    parse_tags_from_context,
    extract_metadata_from_context,
)
from services.ingest_runner_service import extract_text_from_artifact

logger = logging.getLogger(__name__)

_embeddings = Embeddings()

ARTIFACTS_INDEX = "artifacts"

# Optional async queue
try:
    from search.ingest import index_queue
except Exception as e:  # pragma: no cover - queue optional during static analysis
    logger.error(f"Failed to import index_queue: {e}", exc_info=True)
    index_queue = None  # type: ignore


# ============================================================
#  Core: prepare / build IDs
# ============================================================

def _build_chunk_id(root_id: str, version_id: str, chunk_id: int) -> str:
    return f"{root_id}:{version_id}:chunk:{chunk_id}"


def _prepare_base_doc(
    artifact: Artifact,
    collection_id: str,
    *,
    is_head: bool = True,
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the base OpenSearch document for any artifact."""
    _owner = owner_id or artifact.created_by or getattr(artifact, "modified_by", None) or "unknown"

    text_fields = extract_text_from_context(artifact.context)
    raw_tags = parse_tags_from_context(artifact.context)
    canonical_tags = normalize_tags(raw_tags)

    metadata = extract_metadata_from_context(artifact.context)
    # Container-as-artifact fallback: content_type may be on the artifact row,
    # not in context JSON.
    if "content_type" not in metadata:
        artifact_content_type = getattr(artifact, "content_type", None)
        if artifact_content_type:
            metadata["content_type"] = artifact_content_type
    metadata["created_at"] = artifact.created_time

    description = text_fields.get("description", "").strip()
    title = text_fields.get("title", "").strip()

    # Containers (workspace/collection artifacts) keep human labels on the row.
    if not title:
        title = (getattr(artifact, "name", "") or "").strip()
    if not description:
        description = (getattr(artifact, "description", "") or "").strip()

    content = (extract_text_from_artifact(artifact) or "").strip()
    if not content:
        # Ensure name/description-only containers are still discoverable by BM25.
        content = "\n\n".join([s for s in [title, description] if s]).strip()

    logger.debug(
        "Artifact %s: title='%s', description='%s', content length=%d",
        artifact.id, title, description[:50], len(content),
    )

    return {
        "root_id": artifact.root_id or artifact.id,
        "version_id": artifact.id,
        "owner_id": _owner,
        "collection_id": collection_id,
        "state": artifact.state,
        "is_head": is_head,
        "description": description,
        "title": title,
        "content": content,
        "tags_canonical": canonical_tags,
        "tags_ngram": " ".join(canonical_tags),
        "metadata": metadata,
        "indexed_at": datetime.now(timezone.utc).isoformat() + "Z",
    }


# ============================================================
#  Single-artifact indexing
# ============================================================

def index_artifact(
    artifact: Artifact,
    collection_id: str,
    *,
    is_head: bool = True,
) -> bool:
    """Index one artifact into OpenSearch.  This is the ONE indexing path.

    Every artifact — any content type, any collection — goes through here.
    Archived artifacts are skipped.
    """
    if artifact.state == Artifact.STATE_ARCHIVED:
        logger.debug("Skipping archived artifact %s", artifact.id)
        return False

    try:
        base_doc = _prepare_base_doc(artifact, collection_id, is_head=is_head)

        # Chunks (with optional embedding)
        chunk_docs: list[dict] = []
        content_text = base_doc["content"]

        if should_chunk_content(content_text):
            for chunk in chunk_text(content_text):
                text = chunk["text"]
                _emb = _embeddings([text]) if text else []
                embedding = _emb[0] if _emb else None
                chunk_docs.append({
                    "_id": _build_chunk_id(base_doc["root_id"], base_doc["version_id"], chunk["chunk_id"]),
                    **base_doc,
                    "chunk_index": chunk["chunk_id"],
                    "content": text,
                    "content_vector": embedding,
                })
        else:
            _emb = _embeddings([content_text]) if content_text else []
            embedding = _emb[0] if _emb else None
            chunk_docs.append({
                "_id": _build_chunk_id(base_doc["root_id"], base_doc["version_id"], 0),
                **base_doc,
                "chunk_index": 0,
                "content": content_text,
                "content_vector": embedding,
            })

        if chunk_docs:
            bulk_index_documents(ARTIFACTS_INDEX, chunk_docs, id_field="_id")

        logger.info("Indexed artifact %s in collection %s", artifact.id, collection_id)
        return True
    except Exception as e:
        logger.error("Indexing failed for artifact %s: %s", artifact.id, e, exc_info=True)
        return False


# ============================================================
#  Batch indexing
# ============================================================

def index_artifacts_batch(
    artifacts: list[Artifact],
    collection_id: str,
    *,
    is_head: bool = True,
) -> bool:
    """Bulk-index a list of artifacts into OpenSearch in one operation."""
    if not artifacts:
        return True

    start_time = time.time()
    logger.info("Starting bulk index of %d artifacts", len(artifacts))

    try:
        texts_to_embed: list[str] = []
        chunk_text_map: Dict[int, Tuple[Dict, int, str]] = {}
        no_embed_chunks: list[Tuple[Dict, int, str]] = []

        for artifact in artifacts:
            if artifact.state == Artifact.STATE_ARCHIVED:
                continue

            base_doc = _prepare_base_doc(artifact, collection_id, is_head=is_head)

            content_text = base_doc["content"]
            if should_chunk_content(content_text):
                for chunk in chunk_text(content_text):
                    text = chunk["text"]
                    if text:
                        idx = len(texts_to_embed)
                        texts_to_embed.append(text)
                        chunk_text_map[idx] = (base_doc, chunk["chunk_id"], text)
            else:
                if content_text:
                    idx = len(texts_to_embed)
                    texts_to_embed.append(content_text)
                    chunk_text_map[idx] = (base_doc, 0, content_text)
                else:
                    no_embed_chunks.append((base_doc, 0, ""))

        # Batch generate embeddings
        embeddings = _embeddings(texts_to_embed) if texts_to_embed else []

        # Build chunk records
        chunk_records: list[dict] = []
        for idx, (base_doc, chunk_id, text) in chunk_text_map.items():
            chunk_records.append({
                "_id": _build_chunk_id(base_doc["root_id"], base_doc["version_id"], chunk_id),
                **base_doc,
                "chunk_index": chunk_id,
                "content": text,
                "content_vector": embeddings[idx] if idx < len(embeddings) else None,
            })
        for base_doc, chunk_id, text in no_embed_chunks:
            chunk_records.append({
                "_id": _build_chunk_id(base_doc["root_id"], base_doc["version_id"], chunk_id),
                **base_doc,
                "chunk_index": chunk_id,
                "content": text,
                "content_vector": None,
            })

        # Bulk write
        if chunk_records:
            bulk_index_documents(ARTIFACTS_INDEX, chunk_records, id_field="_id")

        total_time = time.time() - start_time
        logger.info(
            "Bulk indexed %d artifacts (%d chunks) in %.3fs",
            len(artifacts), len(chunk_records), total_time,
        )
        return True
    except Exception as e:
        logger.error("Bulk indexing failed: %s", e, exc_info=True)
        return False


# ============================================================
#  Delete
# ============================================================

def delete_artifact_from_index(
    version_id: str,
    root_id: Optional[str] = None,
) -> bool:
    """Delete an artifact from the canonical OpenSearch artifacts index."""
    try:
        root = root_id or version_id

        delete_by_query(
            ARTIFACTS_INDEX,
            {
                "bool": {
                    "must": [
                        {"term": {"root_id": root}},
                        {"term": {"version_id": version_id}},
                    ]
                }
            },
        )

        logger.info("Deleted artifact %s from search", version_id)
        return True
    except Exception as e:
        logger.error("Failed to delete artifact %s from search: %s", version_id, e, exc_info=True)
        return False


# ============================================================
#  Enqueue helpers
# ============================================================

def enqueue_index_artifact(
    artifact: Artifact,
    collection_id: str,
    *,
    is_head: bool = True,
    tenant_id: Optional[str] = None,
) -> None:
    """Enqueue a single artifact for async indexing.  Falls back to sync if queue unavailable."""
    def _act() -> bool:
        return index_artifact(artifact, collection_id, is_head=is_head)

    desc = f"index artifact {artifact.id} -> {collection_id}"
    if index_queue:
        try:
            index_queue.enqueue(_act, description=desc, tenant_id=tenant_id)
            return
        except RuntimeError:
            pass
    # Queue not available (startup, worker not running) — index synchronously
    logger.debug("Index queue unavailable, indexing synchronously: %s", desc)
    _act()


def enqueue_index_artifacts_batch(
    artifacts: list[Artifact],
    collection_id: str,
    *,
    is_head: bool = True,
    tenant_id: Optional[str] = None,
) -> None:
    """Enqueue a batch of artifacts for async bulk indexing.  Falls back to sync if queue unavailable."""
    def _act() -> bool:
        return index_artifacts_batch(artifacts, collection_id, is_head=is_head)

    desc = f"batch index {len(artifacts)} artifacts -> {collection_id}"
    if index_queue:
        try:
            index_queue.enqueue(_act, description=desc, tenant_id=tenant_id)
            return
        except RuntimeError:
            pass
    # Queue not available — index synchronously
    logger.debug("Index queue unavailable, indexing batch synchronously: %s", desc)
    _act()


