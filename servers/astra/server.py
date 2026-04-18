"""
agience-server-astra � MCP Server
====================================
Ingestion, Validation & Indexing: capture, prepare, and index incoming information.

Astra captures and prepares incoming information through ingestion, validation,
normalization, indexing, hygiene, and telemetry collection from system inputs
and activity streams.

Pipeline position: Input & ingestion (first contact with external content).

Tools
-----
  ingest_file       � Create a workspace card from a file URL or raw text
    document_text_extract � Extract text from a PDF artifact into a derived text artifact
  validate_input    � Validate incoming data against schema or rules
  normalize_artifact    � Normalize card content (dedup fields, standardize formats)
  deduplicate       � Check for duplicate/near-duplicate content
  classify_content  � Classify content type and topic
  connect_source    � Register an external connector (stub)
  sync_source       � Pull latest from a registered connector (stub)
  index_artifact        � Force re-index of a card (stub)
  list_streams      � List active and recent live stream sessions
  collect_telemetry � Collect and record system activity telemetry

Auth
----
  PLATFORM_INTERNAL_SECRET — Shared deployment secret for kernel server auth (set on all platform components)
  AGIENCE_API_URI          — Base URI of the agience-core backend

Stream
------
  SRS_HTTP_API  � SRS HTTP API base URL (e.g. http://srs:1985)

Transport
---------
  MCP_TRANSPORT=streamable-http
  MCP_HOST=0.0.0.0
  MCP_PORT=8087
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("agience-server-astra")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

AGIENCE_API_URI: str = os.getenv("AGIENCE_API_URI", "http://localhost:8081").rstrip("/")
PLATFORM_INTERNAL_SECRET: str | None = os.getenv("PLATFORM_INTERNAL_SECRET")
ASTRA_CLIENT_ID: str = "agience-server-astra"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8087"))
SRS_HTTP_API: str = os.getenv("SRS_HTTP_API", "http://localhost:1985").rstrip("/")
STREAM_INGEST_URL: str = os.getenv("STREAM_INGEST_URL", "rtmp://localhost:1936/live").rstrip("/")
# Optional: LLM connection artifact ID used by the PDF metadata extraction step.
# If unset, metadata extraction is skipped.
ASTRA_LLM_CONNECTION_ARTIFACT_ID: str = os.getenv("ASTRA_LLM_CONNECTION_ARTIFACT_ID", "")


# ---------------------------------------------------------------------------
# Platform auth — client_credentials token exchange
# ---------------------------------------------------------------------------

_token_state: dict = {"access_token": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()


async def _exchange_token() -> str | None:
    """Exchange kernel credentials for a platform JWT; refreshes 60 s before expiry."""
    if not PLATFORM_INTERNAL_SECRET:
        return None

    import time

    async with _token_lock:
        if _token_state["access_token"] and time.time() < _token_state["expires_at"] - 60:
            return _token_state["access_token"]

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{AGIENCE_API_URI}/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": ASTRA_CLIENT_ID,
                    "client_secret": PLATFORM_INTERNAL_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            body = resp.json()

        token = body["access_token"]
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        _token_state["access_token"] = token
        _token_state["expires_at"] = float(payload.get("exp", time.time() + 43200))
        return token


async def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    token = await _exchange_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------------------------------------------------------------------------
# Shared authentication infrastructure (AgieceServerAuth)
# ---------------------------------------------------------------------------

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent.parent / "_shared"))
from agience_server_auth import AgieceServerAuth as _AgieceServerAuth
from artifact_helpers import parse_artifact_context, get_artifact_content_type

_auth = _AgieceServerAuth(ASTRA_CLIENT_ID, AGIENCE_API_URI)


async def _user_headers() -> dict[str, str]:
    """Return headers with the verified delegation JWT, or fall back to server token."""
    return await _auth.user_headers(_exchange_token)


def create_server_app():
    """Return the Astra ASGI app with verified middleware and startup hooks."""
    return _auth.create_app(mcp, _exchange_token)


async def server_startup() -> None:
    """Run Astra startup tasks: Core JWKS fetch + server key registration."""
    await _auth.startup(_exchange_token)


async def _get_workspace_artifact(workspace_id: str, artifact_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/artifacts/{artifact_id}",
            headers=await _user_headers(),
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


async def _get_workspace_artifact_content_url(workspace_id: str, artifact_id: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/artifacts/{artifact_id}/content-url",
            headers=await _user_headers(),
            timeout=30,
        )
    resp.raise_for_status()
    payload = resp.json()
    return str(payload.get("url") or "")


async def _create_workspace_artifact(workspace_id: str, context: dict, content: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/artifacts",
            headers=await _user_headers(),
            json={
                "container_id": workspace_id,
                "context": json.dumps(context),
                "content": content,
                "content_type": context.get("content_type"),
            },
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


async def _update_workspace_artifact(workspace_id: str, artifact_id: str, *, context: dict | None = None, content: str | None = None) -> dict:
    body: dict = {}
    if context is not None:
        body["context"] = json.dumps(context)
    if content is not None:
        body["content"] = content
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{AGIENCE_API_URI}/artifacts/{artifact_id}",
            headers=await _user_headers(),
            json=body,
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    chunks: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def _derive_text_title(title: Optional[str], context: dict) -> str:
    base = title or context.get("title") or context.get("filename") or "Extracted Text"
    if isinstance(base, str) and base.lower().endswith(".pdf"):
        base = base[:-4]
    base = str(base).strip() or "Extracted Text"
    return f"{base} Text"


mcp = FastMCP(
    "agience-server-astra",
    instructions=(
        "You are Astra, the Agience ingestion, validation, and indexing server. "
        "You capture and prepare incoming information through ingestion, validation, "
        "normalization, indexing, and hygiene. You also collect telemetry from system "
        "inputs and activity streams."
    ),
)

from artifact_helpers import register_types_manifest
register_types_manifest(mcp, "astra", __file__)


# ---------------------------------------------------------------------------
# Tool: ingest_file
# ---------------------------------------------------------------------------

@mcp.tool(description="Ingest a file URL or raw text into a workspace as a card.")
async def ingest_file(
    workspace_id: str,
    url: Optional[str] = None,
    text: Optional[str] = None,
    title: Optional[str] = None,
    content_type: str = "text/plain",
) -> str:
    """
    Args:
        workspace_id: Target workspace ID.
        url: Public URL of the file to ingest (mutually exclusive with text).
        text: Raw text content to store directly (mutually exclusive with url).
        title: Optional card title. Inferred from URL filename if not set.
        content_type: MIME type hint (e.g. "text/markdown", "application/pdf").
    """
    if not url and not text:
        return "Error: provide either 'url' or 'text'."

    payload: dict = {
        "context": json.dumps({
            "type": "ingest",
            "source": url or "inline",
            "content_type": content_type,
        }),
        "content": text or "",
    }
    if title:
        payload["title"] = title

    payload["container_id"] = workspace_id
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/artifacts",
            headers=await _user_headers(),
            json=payload,
            timeout=30,
        )

    if resp.status_code >= 400:
        return f"Error: {resp.status_code} � {resp.text[:300]}"

    card = resp.json()
    return f"Ingested card {card.get('id')}: {card.get('title', '(untitled)')}"


# ---------------------------------------------------------------------------
# Tool: document_text_extract
# ---------------------------------------------------------------------------

@mcp.tool(description="Extract text from a PDF artifact and create a derived text artifact. Returns structured JSON with text_artifact_id, extraction method, page count, and content_hash — suitable for downstream Transform steps.")
async def document_text_extract(
    workspace_id: str,
    source_artifact_id: str,
    title: Optional[str] = None,
) -> str:
    """
    Args:
        workspace_id: Workspace that owns the source artifact.
        source_artifact_id: PDF artifact to extract text from.
        title: Optional title override for the derived text artifact.

    Returns JSON:
        {
            "source_artifact_id": str,
            "text_artifact_id": str,
            "title": str,
            "length": int,
            "pages": int,
            "method": "pypdf",
            "content_hash": str,
            "images": [],   # populated by advanced extractors (third-party MCP servers)
            "tables": []    # populated by advanced extractors
        }
    """
    import hashlib

    try:
        artifact = await _get_workspace_artifact(workspace_id, source_artifact_id)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed to load artifact {source_artifact_id}: {exc}"})

    context = _parse_artifact_context(artifact)
    mime = get_artifact_content_type(artifact)
    if mime != "application/pdf":
        return json.dumps({"status": "error", "reason": f"source artifact is not a PDF (content_type={mime or 'unknown'})"})

    try:
        download_url = await _get_workspace_artifact_content_url(workspace_id, source_artifact_id)
        async with httpx.AsyncClient() as client:
            pdf_response = await client.get(download_url, timeout=60)
        pdf_response.raise_for_status()
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed to download PDF: {exc}"})

    pdf_bytes = pdf_response.content
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    try:
        from pypdf import PdfReader as _PdfReader
        reader = _PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        pages_text: list[str] = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                pages_text.append(t.strip())
        extracted_text = "\n\n".join(pages_text).strip()
    except ImportError:
        return json.dumps({"status": "error", "reason": "pypdf not installed on Astra server"})
    except Exception as exc:
        return json.dumps({"status": "error", "reason": f"PDF extraction failed: {exc}"})

    if not extracted_text:
        return json.dumps({"status": "error", "reason": "PDF yielded no extractable text"})

    output_title = _derive_text_title(title, context)
    derived_context = {
        "content_type": "text/markdown",
        "title": output_title,
        "type": "document-text",
        "source_artifact_id": source_artifact_id,
        "content_hash": content_hash,
        "derived_from": {
            "artifact_id": source_artifact_id,
            "transform": "document-text-extract",
            "method": "pypdf",
        },
    }

    try:
        output_artifact = await _create_workspace_artifact(workspace_id, derived_context, extracted_text)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed creating derived artifact: {exc}"})

    # Stamp content_hash onto source artifact context for dedup
    if context.get("content_hash") != content_hash:
        try:
            await _update_workspace_artifact(workspace_id, source_artifact_id, context={**context, "content_hash": content_hash})
        except httpx.HTTPError as exc:
            log.warning("document_text_extract: failed to stamp content_hash on source: %s", exc)

    return json.dumps({
        "status": "ok",
        "source_artifact_id": source_artifact_id,
        "text_artifact_id": output_artifact.get("id"),
        "title": output_title,
        "length": len(extracted_text),
        "pages": page_count,
        "method": "pypdf",
        "content_hash": content_hash,
        "images": [],
        "tables": [],
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: process_uploaded_content
# ---------------------------------------------------------------------------

# Max bytes to download for text extraction.
_MAX_DOWNLOAD_BYTES = 10_000_000  # 10 MB

# Derived-artifact context types to skip (prevent infinite loops).
_SKIP_CONTEXT_TYPES = frozenset({
    "workspace-event-handler",
    "ingest.parsed_text",
    "ingest.chunk",
})

# Chunking thresholds � _CHUNK_SIZE / _CHUNK_OVERLAP (tokens) used by both
# process_uploaded_content and ingest_text.  Character threshold used as a
# fast pre-check before tokenising.
_CHUNK_THRESHOLD_CHARS = 4_000


@mcp.tool(description="Process uploaded text content � extract text and create derived artifacts for search and agent use.")
async def process_uploaded_content(
    workspace_id: str,
    artifact_id: str,
    source_artifact_id: Optional[str] = None,
    event_type: Optional[str] = None,
) -> str:
    """
    Triggered by upload_complete lifecycle events for text-extractable types.
    Extracts text from the uploaded artifact and creates derived parsed_text
    and chunk artifacts in the same workspace.

    Args:
        workspace_id: Workspace containing the uploaded artifact.
        artifact_id: The uploaded artifact to process.
        source_artifact_id: Alias for artifact_id (from event dispatch).
        event_type: The lifecycle event that triggered this (informational).
    """
    source_artifact_id = artifact_id or source_artifact_id
    if not source_artifact_id:
        return json.dumps({"status": "skipped", "reason": "no artifact_id"})

    # Fetch artifact metadata from Core
    try:
        artifact = await _get_workspace_artifact(workspace_id, source_artifact_id)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed to fetch artifact: {exc}"})

    context = _parse_artifact_context(artifact)

    # Skip derived/handler artifacts
    ctx_type = context.get("type") or ""
    if ctx_type in _SKIP_CONTEXT_TYPES:
        return json.dumps({"status": "skipped", "reason": f"source is {ctx_type}"})

    content_type = get_artifact_content_type(artifact)
    if not _is_text_extractable(content_type):
        return json.dumps({"status": "skipped", "reason": "not_text_extractable", "content_type": content_type})

    # Extract text: prefer inline content, fall back to S3 download
    text = (artifact.get("content") or "").strip()
    if not text:
        content_key = context.get("content_key")
        if not content_key:
            return json.dumps({"status": "skipped", "reason": "no content or content_key"})

        try:
            download_url = await _get_workspace_artifact_content_url(workspace_id, source_artifact_id)
            async with httpx.AsyncClient() as client:
                resp = await client.get(download_url, timeout=60)
            resp.raise_for_status()
            raw_bytes = resp.content[:_MAX_DOWNLOAD_BYTES]

            encoding = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
                encoding = charset or "utf-8"
            try:
                text = raw_bytes.decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = raw_bytes.decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            return json.dumps({"status": "error", "reason": f"failed to download content: {exc}"})

    text = text.strip()
    if not text:
        return json.dumps({"status": "skipped", "reason": "no_text_extracted"})

    # Cap text length
    if len(text) > 2_000_000:
        text = text[:2_000_000]

    created_ids: list[str] = []
    filename = context.get("filename") or source_artifact_id

    # Create parsed_text artifact
    parsed_context = {
        "type": "ingest.parsed_text",
        "source_artifact_id": source_artifact_id,
        "content_type": "text/plain",
        "title": f"Parsed: {filename}",
    }
    try:
        parsed = await _create_workspace_artifact(workspace_id, parsed_context, text)
        parsed_id = parsed.get("id")
        if parsed_id:
            created_ids.append(parsed_id)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed to create parsed artifact: {exc}"})

    # Create chunk artifacts for long text
    chunk_ids: list[str] = []
    if len(text) > _CHUNK_THRESHOLD_CHARS:
        chunks = _chunk_text(text)
        for chunk in chunks:
            chunk_context = {
                "type": "ingest.chunk",
                "source_artifact_id": source_artifact_id,
                "parsed_artifact_id": parsed_id,
                "chunk_index": chunk["chunk_id"],
                "content_type": "text/plain",
                "title": f"Chunk {chunk['chunk_id']}: {filename}",
            }
            try:
                chunk_artifact = await _create_workspace_artifact(
                    workspace_id, chunk_context, chunk["text"],
                )
                cid = chunk_artifact.get("id")
                if cid:
                    chunk_ids.append(cid)
                    created_ids.append(cid)
            except httpx.HTTPError:
                log.warning("Failed to create chunk %d for %s", chunk["chunk_id"], source_artifact_id)

    return json.dumps({
        "status": "ok",
        "source_artifact_id": source_artifact_id,
        "parsed_artifact_id": parsed_id,
        "chunk_ids": chunk_ids,
        "total_created": len(created_ids),
    })


# ---------------------------------------------------------------------------
# Tool: validate_input
# ---------------------------------------------------------------------------

@mcp.tool(description="Validate incoming data against a schema or content rules.")
async def validate_input(
    content: str,
    schema: Optional[dict] = None,
    content_type: Optional[str] = None,
) -> str:
    """
    Args:
        content: Content to validate.
        schema: Optional JSON Schema to validate against.
        content_type: Expected MIME type for format validation.
    """
    return "TODO: validate_input not yet implemented."


# ---------------------------------------------------------------------------
# Tool: normalize_artifact
# ---------------------------------------------------------------------------

@mcp.tool(description="Normalize card content � standardize fields, clean formatting, resolve encodings.")
async def normalize_artifact(
    artifact_id: str,
    workspace_id: str,
) -> str:
    """
    Args:
        artifact_id: Card to normalize.
        workspace_id: Workspace containing the card.
    """
    return f"TODO: normalize_artifact not yet implemented. artifact_id={artifact_id}"


# ---------------------------------------------------------------------------
# Tool: apply_metadata
# ---------------------------------------------------------------------------

@mcp.tool(description="Merge LLM-extracted metadata into an artifact's context.metadata field, then re-index so the fields become searchable.")
async def apply_metadata(
    workspace_id: str,
    artifact_id: str,
    metadata: str,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the artifact.
        artifact_id: Artifact to update.
        metadata: JSON string with extracted metadata fields, e.g.:
            {"published_by": "...", "published_date": "2024-01-15",
             "document_type": "Report", "formality": "High",
             "sector": "Technology", "issues": ["AI", "data privacy"]}

    Returns JSON: {"status": "ok", "artifact_id": str, "fields_applied": list}
    """
    try:
        new_meta = json.loads(metadata) if isinstance(metadata, str) else metadata
    except json.JSONDecodeError as exc:
        return json.dumps({"status": "error", "reason": f"invalid metadata JSON: {exc}"})

    if not isinstance(new_meta, dict):
        return json.dumps({"status": "error", "reason": "metadata must be a JSON object"})

    try:
        artifact = await _get_workspace_artifact(workspace_id, artifact_id)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed to fetch artifact: {exc}"})

    context = _parse_artifact_context(artifact)

    # Merge into context.metadata — existing values not overwritten
    existing_meta = context.get("metadata") or {}
    merged_meta = {**new_meta, **existing_meta}  # existing values win on conflict
    updated_context = {**context, "metadata": merged_meta}

    try:
        await _update_workspace_artifact(workspace_id, artifact_id, context=updated_context)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed to update artifact: {exc}"})

    return json.dumps({
        "status": "ok",
        "artifact_id": artifact_id,
        "fields_applied": list(new_meta.keys()),
    })


# ---------------------------------------------------------------------------
# Tool: ingest_pipeline
# ---------------------------------------------------------------------------

_METADATA_EXTRACTION_PROMPT = (
    "You are a document metadata extraction assistant.\n\n"
    "Extract structured metadata from the document text provided.\n\n"
    "Return ONLY a valid JSON object with exactly these fields:\n"
    "- published_by: the publishing organization or author name (string, or null)\n"
    "- published_date: publication date in YYYY-MM-DD format (string, or null)\n"
    "- document_type: one of Report, Correspondence, Forecast, Instructional, "
    "Analysis, Transcript, Schedule, Other\n"
    "- formality: one of High, Medium, Low\n"
    "- sector: primary industry sector (string, e.g. 'Technology', 'Finance', "
    "'Healthcare', 'Government', 'Energy', 'Other')\n"
    "- issues: list of key topics or issues covered (array of short strings, "
    "max 8 items)\n\n"
    "Return only the JSON object — no markdown fences, no explanation."
)

# Limit extracted text sent to LLM to avoid token blow-up
_MAX_LLM_TEXT_CHARS = 60_000


@mcp.tool(
    description=(
        "Run the full document ingestion pipeline on a PDF artifact: "
        "deduplication check → text extraction → LLM metadata extraction → "
        "apply metadata back to the source artifact. Returns a summary of "
        "all steps executed."
    )
)
async def ingest_pipeline(
    workspace_id: str,
    artifact_id: str,
    skip_dedup: bool = False,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the PDF artifact.
        artifact_id: The PDF artifact to ingest.
        skip_dedup: Skip the deduplication check (default: False).
    """
    steps: list[dict] = []

    # Step 1 — Deduplication
    if not skip_dedup:
        dedup_raw = await deduplicate(workspace_id, artifact_id)
        try:
            dedup_result = json.loads(dedup_raw)
        except json.JSONDecodeError:
            return json.dumps({"status": "error", "step": "dedup", "reason": f"unexpected dedup response: {dedup_raw[:200]}"})

        steps.append({"step": "dedup", "result": dedup_result})

        if dedup_result.get("status") == "error":
            return json.dumps({"status": "error", "step": "dedup", "reason": dedup_result.get("reason", "unknown")})
        if dedup_result.get("status") == "duplicate":
            return json.dumps({
                "status": "duplicate",
                "artifact_id": artifact_id,
                "duplicate_of": dedup_result.get("duplicate_of"),
                "steps": steps,
            })
    else:
        steps.append({"step": "dedup", "result": {"status": "skipped"}})

    # Step 2 — PDF text extraction
    extract_raw = await document_text_extract(workspace_id, artifact_id)
    try:
        extract_result = json.loads(extract_raw)
    except json.JSONDecodeError:
        return json.dumps({"status": "error", "step": "pdf_extract", "reason": f"unexpected extract response: {extract_raw[:200]}"})

    steps.append({"step": "pdf_extract", "result": extract_result})

    if extract_result.get("status") != "ok":
        return json.dumps({"status": "error", "step": "pdf_extract", "reason": extract_result.get("reason", "extraction failed"), "steps": steps})

    text_artifact_id = extract_result.get("text_artifact_id")

    # Fetch the extracted text from the derived artifact
    try:
        text_artifact = await _get_workspace_artifact(workspace_id, text_artifact_id)
        extracted_text = (text_artifact.get("content") or "").strip()
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "step": "pdf_extract", "reason": f"failed to read extracted text artifact: {exc}", "steps": steps})

    if not extracted_text:
        return json.dumps({"status": "error", "step": "pdf_extract", "reason": "extracted text artifact is empty", "steps": steps})

    # Step 3 — LLM metadata extraction via Verso's invoke_llm
    if not ASTRA_LLM_CONNECTION_ARTIFACT_ID:
        steps.append({"step": "metadata_extract", "result": {"status": "skipped", "reason": "ASTRA_LLM_CONNECTION_ARTIFACT_ID not configured"}})
        return json.dumps({"status": "partial", "artifact_id": artifact_id, "text_artifact_id": text_artifact_id, "reason": "LLM metadata extraction skipped", "steps": steps})

    truncated_text = extracted_text[:_MAX_LLM_TEXT_CHARS]
    llm_messages = [
        {"role": "system", "content": _METADATA_EXTRACTION_PROMPT},
        {"role": "user", "content": f"Document text:\n{truncated_text}"},
    ]

    try:
        async with httpx.AsyncClient() as client:
            llm_resp = await client.post(
                f"{AGIENCE_API_URI}/artifacts/verso/invoke",
                headers=await _user_headers(),
                json={
                    "name": "invoke_llm",
                    "arguments": {
                        "connection_artifact_id": ASTRA_LLM_CONNECTION_ARTIFACT_ID,
                        "workspace_id": workspace_id,
                        "messages": json.dumps(llm_messages),
                    },
                    "workspace_id": workspace_id,
                },
                timeout=120,
            )
        llm_resp.raise_for_status()
        llm_data = llm_resp.json()
        # verso.invoke_llm returns JSON-encoded {"text": ..., "provider": ..., ...}
        # dispatched through the platform, so llm_data may wrap the MCP tool
        # result in {"result": {"content": [{"type": "text", "text": "..."}]}}.
        llm_output = ""
        if isinstance(llm_data, dict):
            result = llm_data.get("result") or llm_data
            if isinstance(result, dict):
                content_list = result.get("content")
                if isinstance(content_list, list) and content_list:
                    payload_text = content_list[0].get("text", "") if isinstance(content_list[0], dict) else ""
                    try:
                        payload = json.loads(payload_text) if payload_text else {}
                    except json.JSONDecodeError:
                        payload = {"text": payload_text}
                    llm_output = payload.get("text") if isinstance(payload, dict) else ""
                else:
                    llm_output = result.get("text", "") if isinstance(result, dict) else ""
    except httpx.HTTPError as exc:
        steps.append({"step": "metadata_extract", "result": {"status": "error", "reason": str(exc)}})
        return json.dumps({"status": "partial", "artifact_id": artifact_id, "text_artifact_id": text_artifact_id, "reason": f"LLM metadata extraction failed: {exc}", "steps": steps})

    # Parse LLM output as JSON
    metadata = None
    try:
        # Strip markdown fences if the LLM ignored instructions
        clean = llm_output.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        metadata = json.loads(clean)
    except json.JSONDecodeError:
        steps.append({"step": "metadata_extract", "result": {"status": "error", "reason": "LLM returned non-JSON", "raw": llm_output[:500]}})
        return json.dumps({"status": "partial", "artifact_id": artifact_id, "text_artifact_id": text_artifact_id, "reason": "LLM metadata response was not valid JSON", "steps": steps})

    steps.append({"step": "metadata_extract", "result": {"status": "ok", "metadata": metadata}})

    # Step 4 — Apply metadata to source artifact
    apply_raw = await apply_metadata(workspace_id, artifact_id, json.dumps(metadata))
    try:
        apply_result = json.loads(apply_raw)
    except json.JSONDecodeError:
        return json.dumps({"status": "error", "step": "metadata_apply", "reason": f"unexpected apply response: {apply_raw[:200]}", "steps": steps})

    steps.append({"step": "metadata_apply", "result": apply_result})

    if apply_result.get("status") != "ok":
        return json.dumps({"status": "partial", "artifact_id": artifact_id, "text_artifact_id": text_artifact_id, "reason": "metadata apply failed", "steps": steps})

    return json.dumps({
        "status": "ok",
        "artifact_id": artifact_id,
        "text_artifact_id": text_artifact_id,
        "pages": extract_result.get("pages"),
        "content_hash": extract_result.get("content_hash"),
        "metadata": metadata,
        "steps": steps,
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: deduplicate
# ---------------------------------------------------------------------------

@mcp.tool(description="Check for duplicate content by SHA-256 hash. Stamps content_hash on the artifact context and searches the workspace for prior ingestions of the same file.")
async def deduplicate(
    workspace_id: str,
    artifact_id: str,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the artifact to check.
        artifact_id: Artifact to check for duplicates.

    Returns JSON: {"status": "unique"} or {"status": "duplicate", "duplicate_of": "<artifact_id>", "duplicate_title": "..."}
    """
    import hashlib

    try:
        artifact = await _get_workspace_artifact(workspace_id, artifact_id)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed to fetch artifact: {exc}"})

    context = _parse_artifact_context(artifact)

    # Obtain content for hashing — prefer inline, fall back to S3
    raw_bytes: bytes = b""
    inline = (artifact.get("content") or "").strip()
    if inline:
        raw_bytes = inline.encode("utf-8")
    else:
        content_key = context.get("content_key")
        if content_key:
            try:
                url = await _get_workspace_artifact_content_url(workspace_id, artifact_id)
                async with httpx.AsyncClient() as client:
                    dl = await client.get(url, timeout=60)
                dl.raise_for_status()
                raw_bytes = dl.content
            except httpx.HTTPError as exc:
                return json.dumps({"status": "error", "reason": f"failed to download content: {exc}"})

    if not raw_bytes:
        return json.dumps({"status": "skipped", "reason": "no_content"})

    content_hash = hashlib.sha256(raw_bytes).hexdigest()

    # Stamp hash onto artifact context if not already present
    if context.get("content_hash") != content_hash:
        merged_ctx = {**context, "content_hash": content_hash}
        try:
            await _update_workspace_artifact(workspace_id, artifact_id, context=merged_ctx)
        except httpx.HTTPError as exc:
            log.warning("deduplicate: failed to stamp content_hash on %s: %s", artifact_id, exc)

    # Search workspace for other artifacts with the same hash
    try:
        async with httpx.AsyncClient() as client:
            search_resp = await client.post(
                f"{AGIENCE_API_URI}/artifacts/search",
                headers=await _user_headers(),
                json={
                    "query_text": content_hash,
                    "scope": [workspace_id],
                    "size": 5,
                },
                timeout=30,
            )
        search_resp.raise_for_status()
        results = search_resp.json().get("items", [])
    except httpx.HTTPError as exc:
        log.warning("deduplicate: search failed for %s: %s", artifact_id, exc)
        results = []

    # Filter out the artifact itself
    matches = [r for r in results if r.get("id") != artifact_id and r.get("root_id") != artifact_id]
    if matches:
        first = matches[0]
        return json.dumps({
            "status": "duplicate",
            "content_hash": content_hash,
            "duplicate_of": first.get("id") or first.get("root_id"),
            "duplicate_title": first.get("title") or first.get("context", {}).get("title"),
        })

    return json.dumps({"status": "unique", "content_hash": content_hash})


# ---------------------------------------------------------------------------
# Tool: classify_content
# ---------------------------------------------------------------------------

@mcp.tool(description="Classify content by type, topic, or category using LLM analysis.")
async def classify_content(
    content: str,
    workspace_id: Optional[str] = None,
    categories: Optional[list[str]] = None,
) -> str:
    """
    Args:
        content: Content to classify.
        workspace_id: Optional workspace context.
        categories: Optional list of target categories to choose from.
    """
    return "TODO: classify_content not yet implemented."


# ---------------------------------------------------------------------------
# Tool: connect_source
# ---------------------------------------------------------------------------

@mcp.tool(description="Register an external connector (Drive folder, inbox, Slack channel).")
async def connect_source(
    workspace_id: str,
    connector_type: str,
    connection: dict,
) -> str:
    """connector_type: 'google_drive' | 'gmail' | 'slack' | 'notion' | ..."""
    return f"TODO: connector registration not yet implemented. connector_type={connector_type}"


# ---------------------------------------------------------------------------
# Tool: sync_source
# ---------------------------------------------------------------------------

@mcp.tool(description="Pull latest content from a registered connector into the workspace.")
async def sync_source(workspace_id: str, connector_artifact_id: str) -> str:
    return f"TODO: sync not yet implemented. connector_artifact_id={connector_artifact_id}"


# ---------------------------------------------------------------------------
# Tool: index_artifact
# ---------------------------------------------------------------------------

@mcp.tool(description="Force re-index of a card into the search layer.")
async def index_artifact(workspace_id: str, artifact_id: str) -> str:
    return f"TODO: explicit re-index not yet implemented. artifact_id={artifact_id}"


# ---------------------------------------------------------------------------
# Text extraction & chunking utilities (migrated from backend/agents/)
# ---------------------------------------------------------------------------

_TEXT_EXTRACTABLE = {
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/ld+json",
    "application/xhtml+xml",
}

_MAX_CONTENT_LEN = 2_000_000  # ~2 MB cap for derived artifact content
_CHUNK_SIZE = int(os.getenv("SEARCH_CHUNK_SIZE", "1000"))
_CHUNK_OVERLAP = int(os.getenv("SEARCH_CHUNK_OVERLAP", "200"))


def _is_text_extractable(content_type: str) -> bool:
    if not content_type:
        return False
    content_type = content_type.lower().split(";")[0].strip()
    if content_type.startswith("text/"):
        return True
    return content_type in _TEXT_EXTRACTABLE


def _chunk_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[dict]:
    """Split text into overlapping token-based chunks."""
    import tiktoken
    encoder = tiktoken.get_encoding("cl100k_base")

    if not text or not text.strip():
        return []
    tokens = encoder.encode(text)
    total = len(tokens)
    if total <= chunk_size:
        return [{"chunk_id": 0, "text": text, "start_token": 0, "end_token": total}]

    chunks = []
    cid = 0
    start = 0
    while start < total:
        end = min(start + chunk_size, total)
        chunks.append({
            "chunk_id": cid,
            "text": encoder.decode(tokens[start:end]),
            "start_token": start,
            "end_token": end,
        })
        cid += 1
        start += chunk_size - overlap
    return chunks


def _should_chunk(text: str) -> bool:
    import tiktoken
    encoder = tiktoken.get_encoding("cl100k_base")
    return len(encoder.encode(text)) > _CHUNK_SIZE


# Alias shared helper — all servers use the same context parsing.
_parse_artifact_context = parse_artifact_context


# ---------------------------------------------------------------------------
# Tool: ingest_text
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Extract text from an uploaded artifact, optionally chunk it, and create "
        "derived workspace artifacts (ingest.parsed_text and ingest.chunk) that are "
        "automatically indexed by the search pipeline."
    )
)
async def ingest_text(
    workspace_id: str,
    source_artifact_id: str,
    create_chunks: bool = True,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the source artifact.
        source_artifact_id: Artifact to extract text from.
        create_chunks: If true, split extracted text into chunk artifacts.
    """
    if not source_artifact_id:
        return json.dumps({"status": "skipped", "reason": "no source_artifact_id"})

    try:
        artifact = await _get_workspace_artifact(workspace_id, source_artifact_id)
    except httpx.HTTPError:
        return json.dumps({"status": "skipped", "reason": "artifact_not_found"})

    source_ctx = _parse_artifact_context(artifact)
    content_type = source_ctx.get("content_type") or ""

    # Skip handler artifacts and already-derived artifacts
    ctx_type = source_ctx.get("type") or ""
    if ctx_type in {"workspace-event-handler", "ingest.parsed_text", "ingest.chunk"}:
        return json.dumps({"status": "skipped", "reason": f"source is {ctx_type}"})

    # Check if content is extractable
    inline_content = (artifact.get("content") or "").strip()
    if not inline_content and not _is_text_extractable(content_type):
        return json.dumps({"status": "skipped", "reason": "not_text_extractable", "content_type": content_type})

    # Extract text: prefer inline content, fall back to S3 download
    text = inline_content
    if not text:
        try:
            download_url = await _get_workspace_artifact_content_url(workspace_id, source_artifact_id)
            if download_url:
                async with httpx.AsyncClient() as client:
                    dl_resp = await client.get(download_url, timeout=60)
                dl_resp.raise_for_status()
                text = dl_resp.content.decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning("ingest_text: failed to download content for %s: %s", source_artifact_id, exc)

    if not text or not text.strip():
        return json.dumps({"status": "skipped", "reason": "no_text_extracted"})

    if len(text) > _MAX_CONTENT_LEN:
        text = text[:_MAX_CONTENT_LEN]

    created_ids: list[str] = []

    # Create parsed_text artifact
    parsed_ctx = {
        "type": "ingest.parsed_text",
        "source_artifact_id": source_artifact_id,
        "content_type": "text/plain",
        "title": f"Parsed: {source_ctx.get('filename') or source_artifact_id}",
    }
    try:
        parsed_artifact = await _create_workspace_artifact(workspace_id, parsed_ctx, text)
        parsed_id = parsed_artifact.get("id", "")
        created_ids.append(parsed_id)
    except httpx.HTTPError as exc:
        return json.dumps({"status": "error", "reason": f"failed creating parsed artifact: {exc}"})

    # Create chunk artifacts
    chunk_ids: list[str] = []
    if create_chunks and _should_chunk(text):
        chunks = _chunk_text(text)
        for chunk in chunks:
            chunk_ctx = {
                "type": "ingest.chunk",
                "source_artifact_id": source_artifact_id,
                "parsed_artifact_id": parsed_id,
                "chunk_index": chunk["chunk_id"],
                "start_token": chunk["start_token"],
                "end_token": chunk["end_token"],
                "content_type": "text/plain",
                "title": f"Chunk {chunk['chunk_id']}: {source_ctx.get('filename') or source_artifact_id}",
            }
            try:
                chunk_artifact = await _create_workspace_artifact(workspace_id, chunk_ctx, chunk["text"])
                cid = chunk_artifact.get("id", "")
                chunk_ids.append(cid)
                created_ids.append(cid)
            except httpx.HTTPError:
                log.warning("ingest_text: failed creating chunk %d for %s", chunk["chunk_id"], source_artifact_id)

    return json.dumps({
        "status": "ok",
        "source_artifact_id": source_artifact_id,
        "parsed_artifact_id": parsed_id,
        "chunk_ids": chunk_ids,
        "total_created": len(created_ids),
    })


# ---------------------------------------------------------------------------
# Tool: list_streams
# ---------------------------------------------------------------------------

@mcp.tool(description="List active and recent live stream sessions.")
async def list_streams(source_artifact_id: Optional[str] = None) -> str:
    """
    Args:
        source_artifact_id: Optional source artifact to filter by. Returns all active streams if omitted.
    """
    from stream_routes import _ACTIVE_SESSIONS, _is_session_active, _utcnow

    now = _utcnow()
    sessions = []
    for _key, entry in _ACTIVE_SESSIONS.items():
        if not _is_session_active(entry, now):
            continue
        if source_artifact_id and entry.get("source_artifact_id") != source_artifact_id:
            continue
        sessions.append({
            "workspace_id": entry.get("workspace_id"),
            "source_artifact_id": entry.get("source_artifact_id"),
            "artifact_id": entry.get("artifact_id"),
            "status": "live",
        })
    return json.dumps({"count": len(sessions), "sessions": sessions})


# ---------------------------------------------------------------------------
# Tool: transcribe
# ---------------------------------------------------------------------------

@mcp.tool(description="Finalize a completed stream session card into a transcript card.")
async def transcribe(
    workspace_id: str,
    session_artifact_id: str,
    title: Optional[str] = None,
) -> str:
    """
    Args:
        workspace_id: Workspace that owns the stream session card.
        session_artifact_id: The completed stream session card to finalize.
        title: Optional transcript title override.
    """
    if not session_artifact_id:
        return "Error: session_artifact_id is required."

    try:
        artifact = await _get_workspace_artifact(workspace_id, session_artifact_id)
    except httpx.HTTPError as exc:
        return f"Error: failed to load session artifact {session_artifact_id}: {exc}"

    transcript_text = (artifact.get("content") or "").strip()
    if not transcript_text:
        return json.dumps({
            "error": "Session artifact has no transcript content yet. Ensure the stream has ended.",
            "session_artifact_id": session_artifact_id,
        })

    raw_ctx = artifact.get("context") or {}
    if isinstance(raw_ctx, str):
        try:
            raw_ctx = json.loads(raw_ctx)
        except json.JSONDecodeError:
            raw_ctx = {}
    session_ctx = raw_ctx if isinstance(raw_ctx, dict) else {}

    artifact_title = title or session_ctx.get("title") or "Transcript"
    output_context = {
        "content_type": "text/markdown",
        "title": artifact_title,
        "type": "transcript",
        "source_artifact_id": session_artifact_id,
    }

    try:
        output_artifact = await _create_workspace_artifact(workspace_id, output_context, transcript_text)
    except httpx.HTTPError as exc:
        return f"Error: failed creating transcript artifact: {exc}"

    return json.dumps({
        "transcript_artifact_id": output_artifact.get("id"),
        "session_artifact_id": session_artifact_id,
        "title": artifact_title,
        "length": len(transcript_text),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: collect_telemetry
# ---------------------------------------------------------------------------

@mcp.tool(description="Collect and record system activity telemetry as workspace cards.")
async def collect_telemetry(
    workspace_id: str,
    source: str,
    metrics: Optional[dict] = None,
) -> str:
    """
    Args:
        workspace_id: Workspace to store telemetry cards.
        source: Telemetry source identifier.
        metrics: Optional structured metrics data.
    """
    return f"TODO: collect_telemetry not yet implemented. source={source}"


# ---------------------------------------------------------------------------
# Tool: rotate_stream_key
# ---------------------------------------------------------------------------

@mcp.tool(description="Generate or rotate the RTMP stream key for a stream source artifact.")
async def rotate_stream_key(
    workspace_id: str,
    artifact_id: str,
) -> str:
    """
    Args:
        workspace_id: Workspace that owns the stream artifact.
        artifact_id: The stream source artifact whose key to rotate.

    Returns JSON with {key_id, key, server_url} � key value is shown once and must be saved.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/artifacts/{artifact_id}/key",
            headers=await _user_headers(),
            params={"key_context": "stream"},
            timeout=15,
        )
        if resp.status_code >= 400:
            return f"Error: {resp.status_code} � {resp.text[:300]}"

        data = resp.json()
        data["server_url"] = STREAM_INGEST_URL

        # Persist server_url into the artifact context so the viewer can read
        # it on future loads without knowing the platform's ingest URL.
        try:
            art_resp = await client.get(
                f"{AGIENCE_API_URI}/artifacts/{artifact_id}",
                headers=await _user_headers(),
                timeout=15,
            )
            if art_resp.status_code == 200:
                artifact = art_resp.json()
                ctx = artifact.get("context", {})
                if isinstance(ctx, str):
                    ctx = json.loads(ctx)
                stream_cfg = {**(ctx.get("stream") or {}), "server_url": STREAM_INGEST_URL}
                await client.patch(
                    f"{AGIENCE_API_URI}/artifacts/{artifact_id}",
                    headers=await _user_headers(),
                    json={"context": json.dumps({**ctx, "stream": stream_cfg})},
                    timeout=15,
                )
        except Exception as exc:
            log.warning("Failed to persist server_url in artifact context: %s", exc)

    return json.dumps(data)


# ---------------------------------------------------------------------------
# Tool: get_stream_sessions
# ---------------------------------------------------------------------------

@mcp.tool(description="Get active live sessions for a stream source artifact.")
async def get_stream_sessions(
    source_artifact_id: str,
) -> str:
    """
    Args:
        source_artifact_id: The stream source artifact to check for live sessions.

    Returns JSON with {sessions: [...]} � sessions list will be non-empty when the stream is live.
    """
    from stream_routes import _ACTIVE_SESSIONS, _is_session_active, _utcnow

    now = _utcnow()
    sessions = []
    for _key, entry in _ACTIVE_SESSIONS.items():
        if not _is_session_active(entry, now):
            continue
        if entry.get("source_artifact_id") != source_artifact_id:
            continue
        sessions.append({
            "workspace_id": entry.get("workspace_id"),
            "source_artifact_id": entry.get("source_artifact_id"),
            "artifact_id": entry.get("artifact_id"),
            "status": "live",
        })
    return json.dumps({"count": len(sessions), "sessions": sessions})


# ---------------------------------------------------------------------------
# Resource: Stream Source HTML View
# ---------------------------------------------------------------------------

import pathlib


@mcp.resource("ui://astra/vnd.agience.stream.html")
async def stream_html_view() -> str:
    """Standalone MCP Apps HTML view for vnd.agience.stream+json artifacts."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.stream+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point � wraps MCP app + stream routes in a single FastAPI host
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting agience-server-astra � transport=%s port=%s", MCP_TRANSPORT, MCP_PORT)
    if MCP_TRANSPORT == "streamable-http":
        import uvicorn
        from fastapi import FastAPI as _FastAPI
        from stream_routes import router as _stream_router

        _app = _FastAPI(title="Astra")
        _app.include_router(_stream_router)
        _app.mount("/", create_server_app())
        uvicorn.run(_app, host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()
