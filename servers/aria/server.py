"""
agience-server-aria � MCP Server
==================================
Presentation & Interface: formatting, visualization, language, usability.

Aria communicates results to humans through language, formatting,
visualization, and interface presentation while ensuring clarity
and usability.

Pipeline position: Output & user interaction (last mile to the human).

Tools
-----
  format_response      � Format content for human consumption
  render_visualization � Create charts, diagrams, or visual representations
  adapt_tone           � Adjust language tone/style for target audience
  present_card         � Present a card's content with appropriate formatting
  narrate              � Generate natural-language narrative from structured data

Auth
----
  PLATFORM_INTERNAL_SECRET  ⬩ Shared deployment secret for client_credentials token exchange
  AGIENCE_API_URI           ⬩ Base URI of the agience-core backend

Transport
---------
  MCP_TRANSPORT=streamable-http (default for Agience)
  MCP_HOST=0.0.0.0
  MCP_PORT=8083
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

import httpx
import openai
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("agience-server-aria")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

AGIENCE_API_URI: str = os.getenv("AGIENCE_API_URI", "http://localhost:8081").rstrip("/")
PLATFORM_INTERNAL_SECRET: str | None = os.getenv("PLATFORM_INTERNAL_SECRET")
ARIA_CLIENT_ID: str = "agience-server-aria"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8083"))


# ---------------------------------------------------------------------------
# Shared authentication infrastructure (AgieceServerAuth)
# ---------------------------------------------------------------------------

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent.parent / "_shared"))
from agience_server_auth import AgieceServerAuth as _AgieceServerAuth

_auth = _AgieceServerAuth(ARIA_CLIENT_ID, AGIENCE_API_URI)


def create_aria_app():
    """Return the Aria MCP ASGI app with verified middleware and startup hooks."""
    return _auth.create_app(mcp, _exchange_token)


# ---------------------------------------------------------------------------
# Standard server interface (used by _host and standalone)
# ---------------------------------------------------------------------------

def create_server_app():
    """Return the Aria ASGI app with verified middleware and startup hooks."""
    return create_aria_app()


async def server_startup() -> None:
    """Run Aria startup tasks: Core JWKS fetch + server key registration."""
    await _auth.startup(_exchange_token)


# ---------------------------------------------------------------------------
# Platform auth — client_credentials token exchange (Aria's own server identity)
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
                    "client_id": ARIA_CLIENT_ID,
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
    """Return headers with Aria's own server identity (for non-delegated calls)."""
    h = {"Content-Type": "application/json"}
    token = await _exchange_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _user_headers() -> dict[str, str]:
    """Return headers forwarding the caller's user token (from transport layer).

    When Core calls Aria on behalf of a user it injects the user's JWT as the
    Authorization header.  ``_UserTokenMiddleware`` captures that token into
    ``_request_user_token``.  This function reads it for Core REST callbacks
    so the user identity flows at the protocol level — never as a tool argument.

    Falls back to Aria's server token when no user token is present (e.g. direct
    server-to-server calls that don't carry a user context).
    """
    return await _auth.user_headers(_exchange_token)


mcp = FastMCP(
    "agience-server-aria",
    instructions=(
        "You are Aria, the Agience presentation and interface server. "
        "You communicate results to humans through language, formatting, "
        "visualization, and interface presentation. Your goal is clarity "
        "and usability � transform structured data into human-readable output."
    ),
)

from artifact_helpers import register_types_manifest
register_types_manifest(mcp, "aria", __file__)


# ---------------------------------------------------------------------------
# Tool: format_response
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Format content for human consumption. Transforms raw or structured "
        "data into a polished response with appropriate markup and layout."
    )
)
async def format_response(
    content: str,
    format: str = "markdown",
    style: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> str:
    """
    Args:
        content: Raw content to format.
        format: Target output format � 'markdown', 'html', 'plain'.
        style: Optional style hint � 'concise', 'detailed', 'executive', 'technical'.
        workspace_id: Optional workspace context for LLM-assisted formatting.
    """
    # TODO(agents-invoke-removal): the former implementation dispatched to a
    # backend agent plugin "aria:format_response" via /agents/invoke, but no
    # such plugin exists and inlining would recurse into this tool. Leaving
    # for manual design review — see servers/aria/server.py for the tool
    # signature contract.
    _ = (content, format, style, workspace_id)  # unused — kept for stable tool signature
    return json.dumps({"error": "format_response is not yet implemented"})


# ---------------------------------------------------------------------------
# Tool: render_visualization
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Create a visual representation of structured data � charts, diagrams, "
        "tables, or other visual formats suitable for human review."
    )
)
async def render_visualization(
    data: str,
    chart_type: str = "auto",
    title: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> str:
    """
    Args:
        data: JSON string of data to visualize.
        chart_type: 'auto', 'bar', 'line', 'pie', 'table', 'diagram'.
        title: Optional title for the visualization.
        workspace_id: Optional workspace to store the visualization card.
    """
    return f"TODO: render_visualization not yet implemented. chart_type={chart_type}"


# ---------------------------------------------------------------------------
# Tool: adapt_tone
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Adjust the language, tone, and style of content for a specific "
        "audience or communication context."
    )
)
async def adapt_tone(
    content: str,
    audience: str = "general",
    tone: str = "professional",
    workspace_id: Optional[str] = None,
) -> str:
    """
    Args:
        content: Text to adapt.
        audience: Target audience � 'executive', 'technical', 'general', 'casual'.
        tone: Desired tone � 'professional', 'friendly', 'formal', 'concise'.
        workspace_id: Optional workspace context.
    """
    return f"TODO: adapt_tone not yet implemented. audience={audience}, tone={tone}"


# ---------------------------------------------------------------------------
# Tool: present_card
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Present a card's content with appropriate formatting and context "
        "for human review. Resolves the card by ID and renders its content."
    )
)
async def present_card(
    artifact_id: str,
    workspace_id: Optional[str] = None,
    format: str = "markdown",
) -> str:
    """
    Args:
        artifact_id: ID of the card to present.
        workspace_id: Workspace containing the card (optional).
        format: Presentation format � 'markdown', 'html', 'plain', 'summary'.
    """
    url = (
        f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}"
        if workspace_id
        else f"{AGIENCE_API_URI}/artifacts/{artifact_id}"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=await _headers(), timeout=15)
    if resp.status_code >= 400:
        return f"Error: {resp.status_code} � {resp.text[:300]}"
    card = resp.json()
    title = card.get("title", "(untitled)")
    content = card.get("content", "")
    content_type = card.get("content_type", "text/plain")
    return f"# {title}\n\nType: {content_type}\n\n{content}"


# ---------------------------------------------------------------------------
# Tool: narrate
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Generate a natural-language narrative explanation from structured "
        "data, results, or card content. Turns data into a story."
    )
)
async def narrate(
    content: str,
    context: Optional[str] = None,
    style: str = "informative",
    workspace_id: Optional[str] = None,
) -> str:
    """
    Args:
        content: Structured data or results to narrate.
        context: Optional background context for the narrative.
        style: Narrative style � 'informative', 'executive-brief', 'tutorial', 'story'.
        workspace_id: Optional workspace context.
    """
    return f"TODO: narrate not yet implemented. style={style}"


# ---------------------------------------------------------------------------
# REST helpers for artifact CRUD
# ---------------------------------------------------------------------------

async def _get_workspace_artifact(workspace_id: str, artifact_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}",
            headers=await _headers(),
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


async def _create_workspace_artifact(workspace_id: str, context: dict, content: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts",
            headers=await _headers(),
            json={"context": context, "content": content},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


async def _update_workspace_artifact(workspace_id: str, artifact_id: str, *, context: dict | None = None, content: str | None = None) -> dict:
    body: dict = {}
    if context is not None:
        body["context"] = context
    if content is not None:
        body["content"] = content
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}",
            headers=await _headers(),
            json=body,
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


def _parse_artifact_context(artifact: dict) -> dict:
    raw = artifact.get("context") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Tool: extract_units � Semantic unit extraction (migrated from agents/)
# ---------------------------------------------------------------------------

_ALLOWED_UNIT_KINDS = {"decision", "constraint", "action", "claim"}


@mcp.tool(
    description=(
        "Extract structured knowledge units (decisions, constraints, actions, claims) "
        "from a source artifact such as a transcript. Creates new workspace artifacts "
        "with semantic metadata and provenance."
    )
)
async def extract_units(
    workspace_id: str,
    source_artifact_id: str,
    artifact_artifact_ids: Optional[List[str]] = None,
    model: str = "gpt-4o-mini",
    max_units: int = 12,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the source artifact.
        source_artifact_id: Artifact to extract units from.
        artifact_artifact_ids: Optional additional artifact IDs for context.
        model: LLM model to use.
        max_units: Maximum number of units to extract.
    """
    if not workspace_id or not source_artifact_id:
        return json.dumps({"error": "workspace_id and source_artifact_id are required"})

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        return json.dumps({"error": "OPENAI_API_KEY not set on Aria server"})

    try:
        source = await _get_workspace_artifact(workspace_id, source_artifact_id)
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"Failed to load source artifact: {exc}"})

    source_text = (source.get("content") or "").strip()
    if not source_text:
        return json.dumps({
            "workspace_id": workspace_id,
            "source_artifact_id": source_artifact_id,
            "created_artifact_ids": [],
            "warning": "Source artifact has no text content.",
        })

    # Fetch additional context artifacts
    artifact_texts: List[tuple[str, str]] = []
    for aid in (artifact_artifact_ids or []):
        if not aid:
            continue
        try:
            ac = await _get_workspace_artifact(workspace_id, str(aid))
            at = (ac.get("content") or "").strip()
            if at:
                artifact_texts.append((str(aid), at))
        except httpx.HTTPError:
            continue

    system_prompt = (
        "You extract structured knowledge units from messy meeting transcripts.\n"
        "Return JSON ONLY with this exact shape:\n"
        "{\n"
        "  \"units\": [\n"
        "    {\"kind\": \"decision|constraint|action|claim\", \"title\": \"<optional>\", \"content\": \"...\", \"evidence_quotes\": [\"<optional quote>\"]}\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        f"- Output at most {max_units} units.\n"
        "- Keep `content` concise and atomic.\n"
        "- `evidence_quotes` is optional; when provided, quotes must be exact substrings copied from the SOURCE text.\n"
        "- If unsure, omit evidence_quotes and still output the unit.\n"
        "- Do not include any text outside the JSON.\n"
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "SOURCE (TRANSCRIPT):\n" + source_text},
    ]

    if artifact_texts:
        blob_lines = ["ARTIFACTS:"]
        for aid, txt in artifact_texts:
            blob_lines.append(f"--- artifact_artifact_id={aid} ---")
            blob_lines.append(txt)
        messages.append({"role": "user", "content": "\n".join(blob_lines)})

    client = openai.OpenAI(api_key=openai_key)
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages,
            temperature=0.2, max_tokens=1200,
        )
        output_text = resp.choices[0].message.content or ""
    except Exception as exc:
        return json.dumps({"error": f"LLM call failed: {exc}"})

    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError:
        payload = {}

    raw_units = payload.get("units") if isinstance(payload, dict) else []
    if not isinstance(raw_units, list):
        raw_units = []

    source_ctx = _parse_artifact_context(source)
    source_title = source_ctx.get("title")
    source_ref: Dict[str, Any] = {"type": "workspace_artifact", "artifact_id": source_artifact_id}
    if source_title:
        source_ref["title"] = str(source_title)

    created_ids: List[str] = []
    for item in raw_units:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _ALLOWED_UNIT_KINDS:
            continue
        content = str(item.get("content") or item.get("statement") or "").strip()
        if not content:
            continue

        # Build evidence from quotes that appear in the source
        evidence: List[Dict[str, Any]] = []
        quotes = item.get("evidence_quotes") or item.get("quotes") or item.get("evidence")
        if isinstance(quotes, list):
            for q in quotes:
                q_str = str(q).strip()
                if q_str and q_str in source_text:
                    evidence.append({"source_artifact_id": source_artifact_id, "quote": q_str})

        ctx: Dict[str, Any] = {
            "semantic": {"kind": kind, "sources": [source_ref], "evidence": evidence},
            "agent": {
                "name": "extract_units",
                "model": model,
                "source_artifact_id": source_artifact_id,
                "artifact_artifact_ids": list(artifact_artifact_ids or []),
            },
        }

        if kind == "action" and not content.startswith("- "):
            content = "- " + content

        try:
            created = await _create_workspace_artifact(workspace_id, ctx, content)
            cid = created.get("id")
            if cid:
                created_ids.append(str(cid))
        except httpx.HTTPError:
            log.warning("extract_units: failed creating unit artifact")

    return json.dumps({
        "workspace_id": workspace_id,
        "source_artifact_id": source_artifact_id,
        "created_artifact_ids": created_ids,
        "unit_count": len(created_ids),
    })


# ---------------------------------------------------------------------------
# Tool: attach_provenance � Evidence & source metadata (migrated from agents/)
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Attach provenance metadata (sources and supporting evidence quotes) "
        "to existing workspace artifacts. Writes into each target artifact's "
        "context.semantic.sources and context.semantic.evidence."
    )
)
async def attach_provenance(
    workspace_id: str,
    source_artifact_id: str,
    target_artifact_ids: Optional[List[str]] = None,
    target_artifact_id: Optional[str] = None,
    mode: str = "evidence",
    model: str = "gpt-4o-mini",
    max_evidence: int = 5,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the artifacts.
        source_artifact_id: Artifact that is the source of provenance.
        target_artifact_ids: List of artifact IDs to attach provenance to.
        target_artifact_id: Single target artifact ID (alternative to list).
        mode: 'sources_only' (no LLM call) or 'evidence' (extract supporting quotes).
        model: LLM model for evidence extraction.
        max_evidence: Maximum evidence items per target.
    """
    if not workspace_id or not source_artifact_id:
        return json.dumps({"error": "workspace_id and source_artifact_id are required"})

    ids: List[str] = []
    if isinstance(target_artifact_ids, list):
        ids.extend([str(x) for x in target_artifact_ids if str(x or "").strip()])
    if target_artifact_id:
        ids.append(str(target_artifact_id))
    ids = list(dict.fromkeys(ids))
    if not ids:
        return json.dumps({"error": "At least one target artifact ID required"})

    mode_norm = (mode or "evidence").strip().lower()
    if mode_norm not in {"sources_only", "evidence"}:
        return json.dumps({"error": "mode must be 'sources_only' or 'evidence'"})

    try:
        source = await _get_workspace_artifact(workspace_id, source_artifact_id)
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"Failed to load source artifact: {exc}"})

    source_ctx = _parse_artifact_context(source)
    source_title = source_ctx.get("title")
    source_text = (source.get("content") or "").strip()

    source_ref: Dict[str, Any] = {"type": "workspace_artifact", "artifact_id": source_artifact_id}
    if source_title:
        source_ref["title"] = str(source_title)

    openai_key = os.getenv("OPENAI_API_KEY") if mode_norm == "evidence" else None

    updated: List[str] = []
    skipped: List[Dict[str, Any]] = []

    for tid in ids:
        try:
            target = await _get_workspace_artifact(workspace_id, tid)
        except httpx.HTTPError as exc:
            skipped.append({"artifact_id": tid, "reason": f"target_fetch_failed: {exc}"})
            continue

        ctx = _parse_artifact_context(target)
        semantic = ctx.get("semantic") if isinstance(ctx.get("semantic"), dict) else {}

        # Dedupe sources
        sources = semantic.get("sources") if isinstance(semantic.get("sources"), list) else []
        sources.append(source_ref)
        seen_src: set[tuple] = set()
        deduped_sources: List[Dict[str, Any]] = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            key = (str(s.get("type") or ""), str(s.get("artifact_id") or ""), str(s.get("uri") or ""))
            if key not in seen_src:
                seen_src.add(key)
                deduped_sources.append(s)
        semantic["sources"] = deduped_sources

        evidence_to_add: List[Dict[str, Any]] = []
        if mode_norm == "evidence" and openai_key:
            target_text = (target.get("content") or "").strip()
            if source_text and target_text:
                system_prompt = (
                    "You attach provenance to notes.\n"
                    "Given a SOURCE document and a TARGET note, return JSON ONLY with the shape:\n"
                    "{\n"
                    "  \"evidence\": [\n"
                    "    {\"quote\": \"...\", \"claim\": \"...\", \"relevance\": \"...\"}\n"
                    "  ]\n"
                    "}\n\n"
                    "Rules:\n"
                    f"- Provide up to {max_evidence} evidence items.\n"
                    "- Each quote MUST be an exact substring copied from SOURCE.\n"
                    "- Quotes should be short (<= 240 chars).\n"
                    "- If no support exists, return {\"evidence\": []}.\n"
                )
                try:
                    client = openai.OpenAI(api_key=openai_key)
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": "SOURCE:\n" + source_text},
                            {"role": "user", "content": "TARGET NOTE:\n" + target_text},
                        ],
                        temperature=0.2, max_tokens=900,
                    )
                    output_text = resp.choices[0].message.content or ""
                    parsed = json.loads(output_text)
                    raw_ev = parsed.get("evidence") if isinstance(parsed, dict) else []
                    if isinstance(raw_ev, list):
                        for ev in raw_ev[:max_evidence]:
                            if not isinstance(ev, dict):
                                continue
                            quote = str(ev.get("quote") or "").strip()
                            if not quote or quote not in source_text:
                                continue
                            item: Dict[str, Any] = {"source_artifact_id": source_artifact_id, "quote": quote}
                            claim = str(ev.get("claim") or "").strip()
                            if claim:
                                item["claim"] = claim
                            relevance = str(ev.get("relevance") or "").strip()
                            if relevance:
                                item["relevance"] = relevance
                            evidence_to_add.append(item)
                except Exception as exc:
                    log.warning("attach_provenance: evidence extraction failed for %s: %s", tid, exc)

        # Dedupe evidence
        existing_ev = semantic.get("evidence") if isinstance(semantic.get("evidence"), list) else []
        existing_ev.extend(evidence_to_add)
        seen_ev: set[tuple] = set()
        deduped_ev: List[Dict[str, Any]] = []
        for ev in existing_ev:
            if not isinstance(ev, dict):
                continue
            key = (str(ev.get("source_artifact_id") or ""), str(ev.get("quote") or ""), str(ev.get("claim") or ""))
            if key not in seen_ev:
                seen_ev.add(key)
                deduped_ev.append(ev)
        semantic["evidence"] = deduped_ev

        ctx["semantic"] = semantic
        agent_meta = ctx.get("agent") if isinstance(ctx.get("agent"), dict) else {}
        agent_meta.update({"name": "attach_provenance", "source_artifact_id": source_artifact_id, "mode": mode_norm})
        ctx["agent"] = agent_meta

        try:
            await _update_workspace_artifact(workspace_id, tid, context=ctx)
            updated.append(tid)
        except httpx.HTTPError as exc:
            skipped.append({"artifact_id": tid, "reason": f"update_failed: {exc}"})

    return json.dumps({
        "workspace_id": workspace_id,
        "source_artifact_id": source_artifact_id,
        "mode": mode_norm,
        "updated_artifact_ids": updated,
        "skipped": skipped,
    })


# ---------------------------------------------------------------------------
# Tool: run_chat_turn � Agentic chat loop
# ---------------------------------------------------------------------------

_CHAT_SYSTEM_PROMPT = """You are an intelligent assistant embedded in Agience, a knowledge curation platform.

You have access to tools that let you search the user's knowledge base, read artifacts, \
browse workspaces and collections, and create or update artifacts.

When answering:
- Use the search tool to find relevant information before answering when appropriate.
- Create artifacts to capture important answers or insights the user may want to keep.
- Reference specific artifact IDs or titles when citing sources.
- Be concise and helpful.

The user's active workspace is where their draft knowledge lives. \
Collections hold committed, versioned knowledge."""

_CHAT_TOOLS = [
    {"type": "function", "function": {"name": "search", "description": "Hybrid semantic + keyword search across the user's collections and workspaces.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "size": {"type": "integer", "default": 10}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_artifact", "description": "Retrieve the full content and context of a specific artifact by its ID.", "parameters": {"type": "object", "properties": {"artifact_id": {"type": "string"}, "workspace_id": {"type": "string"}}, "required": ["artifact_id"]}}},
    {"type": "function", "function": {"name": "browse_workspaces", "description": "List workspaces, or artifacts within a workspace.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "browse_collections", "description": "List collections, or artifacts within a collection.", "parameters": {"type": "object", "properties": {"collection_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "create_artifact", "description": "Create a new text artifact in a workspace.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}}, "required": ["workspace_id", "content"]}}},
    {"type": "function", "function": {"name": "update_artifact", "description": "Update the content or title of an existing workspace artifact.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "artifact_id": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}}, "required": ["workspace_id", "artifact_id"]}}},
]

_MAX_TOOL_ITERATIONS = 8


async def _execute_external_tool(server_id: str, tool_name: str, arguments: dict) -> str:
    """Execute an external MCP tool by calling back through Core's artifact invoke."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{AGIENCE_API_URI}/artifacts/{server_id}/invoke",
                headers=await _headers(),
                json={"name": tool_name, "arguments": arguments},
                timeout=30,
            )
            if resp.status_code >= 400:
                return json.dumps({"error": f"External tool call failed: {resp.status_code} {resp.text[:200]}"})
            return json.dumps(resp.json())
        except Exception as e:
            log.exception("External tool call failed: server=%s tool=%s", server_id, tool_name)
            return json.dumps({"error": f"External tool call failed: {e}"})


async def _execute_chat_tool(name: str, arguments: dict, workspace_id: str | None) -> str:
    """Execute a chat tool by calling the Agience Core REST API.

    Uses the user token received at the transport layer (``_request_user_token``)
    so that user identity flows via HTTP Authorization headers — never as a tool argument.
    """
    user_headers = await _user_headers()
    async with httpx.AsyncClient() as client:
        if name == "search":
            resp = await client.post(
                f"{AGIENCE_API_URI}/artifacts/search",
                headers=user_headers,
                json={"query_text": arguments.get("query", ""), "size": arguments.get("size", 10)},
                timeout=30,
            )
            if resp.status_code >= 400:
                return json.dumps({"error": f"Search failed: {resp.status_code}"})
            data = resp.json()
            hits = [{"id": h.get("root_id") or h.get("id"), "title": h.get("title"), "description": h.get("description"), "content": (h.get("content") or "")[:400]} for h in (data.get("hits") or [])[:10]]
            return json.dumps({"total": data.get("total", 0), "hits": hits})

        elif name == "get_artifact":
            artifact_id = arguments["artifact_id"]
            resp = await client.get(
                f"{AGIENCE_API_URI}/artifacts/{artifact_id}",
                headers=user_headers, timeout=15,
            )
            if resp.status_code < 400:
                a = resp.json()
                return json.dumps({"id": a.get("id"), "content": (a.get("content") or "")[:3000], "context": a.get("context")})
            return json.dumps({"error": "Artifact not found", "artifact_id": artifact_id})

        elif name == "browse_workspaces":
            ws_id = arguments.get("workspace_id")
            if ws_id:
                resp = await client.get(f"{AGIENCE_API_URI}/artifacts/list", headers=user_headers, params={"container_id": ws_id}, timeout=15)
                if resp.status_code < 400:
                    data = resp.json()
                    items = data.get("items", []) if isinstance(data, dict) else data
                    return json.dumps({"workspace_id": ws_id, "count": len(items), "artifacts": [{"id": a.get("id"), "title": (json.loads(a.get("context") or "{}") or {}).get("title", "")} for a in items[:50]]})
            else:
                resp = await client.get(f"{AGIENCE_API_URI}/artifacts/containers", headers=user_headers, timeout=15)
                if resp.status_code < 400:
                    return json.dumps([{"id": w.get("id"), "name": w.get("name")} for w in resp.json()])
            return json.dumps({"error": f"Failed: {resp.status_code}"})

        elif name == "browse_collections":
            col_id = arguments.get("collection_id")
            if col_id:
                resp = await client.get(f"{AGIENCE_API_URI}/artifacts/list", headers=user_headers, params={"container_id": col_id}, timeout=15)
            else:
                resp = await client.get(f"{AGIENCE_API_URI}/artifacts/containers", headers=user_headers, timeout=15)
            if resp.status_code < 400:
                return json.dumps(resp.json())
            return json.dumps({"error": f"Failed: {resp.status_code}"})

        elif name == "create_artifact":
            ws_id = arguments.get("workspace_id") or workspace_id
            if not ws_id:
                return json.dumps({"error": "workspace_id required"})
            content = arguments.get("content", "")
            title = arguments.get("title") or content[:60].split("\n")[0]
            resp = await client.post(
                f"{AGIENCE_API_URI}/artifacts",
                headers=user_headers,
                json={"container_id": ws_id, "context": json.dumps({"content_type": "text/plain", "title": title, "type": "chat-output", "generated_by": "chat"}), "content": content},
                timeout=15,
            )
            if resp.status_code < 400:
                return json.dumps({"id": resp.json().get("id"), "created": True, "title": title})
            return json.dumps({"error": f"Create failed: {resp.status_code}"})

        elif name == "update_artifact":
            artifact_id = arguments.get("artifact_id")
            if not artifact_id:
                return json.dumps({"error": "artifact_id required"})
            body: dict = {}
            if "content" in arguments:
                body["content"] = arguments["content"]
            if "title" in arguments:
                # Fetch current context, merge title
                get_resp = await client.get(f"{AGIENCE_API_URI}/artifacts/{artifact_id}", headers=user_headers, timeout=10)
                if get_resp.status_code < 400:
                    ctx = json.loads(get_resp.json().get("context") or "{}")
                    ctx["title"] = arguments["title"]
                    body["context"] = json.dumps(ctx)
            resp = await client.patch(
                f"{AGIENCE_API_URI}/artifacts/{artifact_id}",
                headers=user_headers, json=body, timeout=15,
            )
            return json.dumps({"updated": resp.status_code < 400, "artifact_id": artifact_id})

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})


@mcp.tool(description="Run one agentic chat turn with tool calling. Returns the assistant reply and tool call log.")
async def run_chat_turn(
    messages: str,
    workspace_id: Optional[str] = None,
    model: str = "gpt-4o-mini",
    connection_artifact_id: Optional[str] = None,
    mcp_server_ids: Optional[str] = None,
    chat_artifact_id: Optional[str] = None,
) -> str:
    """
    Run an agentic chat turn. The LLM can call platform tools (search, browse, create/update artifacts).

    When a connection_artifact_id is provided, LLM calls are routed through Verso's invoke_llm
    tool (which resolves credentials via Seraph and meters usage via Ophan). Otherwise, falls
    back to direct OpenAI API calls using the OPENAI_API_KEY environment variable.

    User identity is carried at the transport layer (Authorization header) — never as a tool argument.

    Args:
        messages: JSON-encoded array of message objects [{role, content}, ...].
        workspace_id: Active workspace ID for scoping tool calls.
        model: LLM model to use (default: gpt-4o-mini).
        connection_artifact_id: Optional LLM Connection artifact ID for credential resolution and metering.
        mcp_server_ids: Optional JSON-encoded list of external MCP server artifact IDs whose
            tools should be injected into this chat turn's tool surface.
        chat_artifact_id: Optional artifact ID of the chat artifact. When provided,
            token deltas are streamed to the browser via Core's event bus.
    """
    try:
        return await _run_chat_turn_impl(messages, workspace_id, model, connection_artifact_id, mcp_server_ids, chat_artifact_id)
    except Exception:
        log.exception("run_chat_turn unhandled exception")
        raise


async def _resolve_server_tools(server_ids: list[str]) -> tuple[list[dict], dict[str, dict]]:
    """Resolve external MCP server tools by calling Core's REST API.

    Returns:
        (tool_defs, external_tool_map) where tool_defs is a list of OpenAI
        function tool definitions and external_tool_map maps prefixed names
        back to {server_id, original_name}.
    """
    tool_defs: list[dict] = []
    external_tool_map: dict[str, dict] = {}

    async with httpx.AsyncClient() as client:
        for server_id in server_ids:
            try:
                resp = await client.get(
                    f"{AGIENCE_API_URI}/mcp/servers/{server_id}/info",
                    headers=await _headers(),
                    timeout=15,
                )
                if resp.status_code >= 400:
                    log.warning("Failed to get info for MCP server %s: %s", server_id, resp.status_code)
                    continue

                info = resp.json()
                if info.get("status") != "ok":
                    continue

                for tool in info.get("tools", []):
                    original_name = tool.get("name", "")
                    if not original_name:
                        continue

                    prefixed_name = f"ext__{server_id}__{original_name}"
                    tool_defs.append({
                        "type": "function",
                        "function": {
                            "name": prefixed_name,
                            "description": tool.get("description", ""),
                            "parameters": tool.get("input_schema") or tool.get("inputSchema") or {"type": "object", "properties": {}},
                        },
                    })
                    external_tool_map[prefixed_name] = {
                        "server_id": server_id,
                        "original_name": original_name,
                    }
            except Exception:
                log.exception("Failed to resolve tools for MCP server %s", server_id)

    return tool_defs, external_tool_map


async def _invoke_llm_via_verso(
    messages: list[dict],
    connection_artifact_id: str,
    workspace_id: str,
    temperature: float = 0.7,
    max_output_tokens: int = 2048,
) -> dict:
    """Call Verso's invoke_llm tool via the artifact-native invoke path.

    Uses Aria's server identity (``_headers()``) for the call to Core.
    User context flows to Verso via the transport layer, not as a parameter.
    """
    arguments: dict = {
        "connection_artifact_id": connection_artifact_id,
        "workspace_id": workspace_id,
        "messages": json.dumps(messages),
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/artifacts/verso/invoke",
            headers=await _headers(),
            json={
                "name": "invoke_llm",
                "arguments": arguments,
                "workspace_id": workspace_id,
            },
            timeout=120,
        )
    if resp.status_code >= 400:
        return {"error": f"Verso invoke_llm failed: {resp.status_code}"}
    try:
        return resp.json()
    except Exception:
        return {"text": resp.text}


async def _push_chat_delta(
    http: httpx.AsyncClient,
    chat_artifact_id: str,
    container_id: str | None,
    delta: str,
) -> None:
    """Push a token delta to Core's event bus so the browser can render
    streaming text in real-time."""
    try:
        await http.post(
            f"{AGIENCE_API_URI}/events/emit",
            headers=await _user_headers(),
            json={
                "event": "artifact.chat.delta",
                "payload": {"delta": delta, "artifact_id": chat_artifact_id},
                "container_id": container_id,
                "artifact_id": chat_artifact_id,
            },
            timeout=5,
        )
    except Exception:
        pass  # best-effort — don't break the chat turn


async def _push_chat_status(
    http: httpx.AsyncClient,
    chat_artifact_id: str,
    container_id: str | None,
    status: str,
) -> None:
    """Push a status event (started / completed / tool_calling) to the browser."""
    try:
        await http.post(
            f"{AGIENCE_API_URI}/events/emit",
            headers=await _user_headers(),
            json={
                "event": "artifact.chat.status",
                "payload": {"status": status, "artifact_id": chat_artifact_id},
                "container_id": container_id,
                "artifact_id": chat_artifact_id,
            },
            timeout=5,
        )
    except Exception:
        pass


async def _run_chat_turn_impl(
    messages: str,
    workspace_id: Optional[str],
    model: str,
    connection_artifact_id: Optional[str] = None,
    mcp_server_ids: Optional[str] = None,
    chat_artifact_id: Optional[str] = None,
) -> str:
    log.info("run_chat_turn called � workspace_id=%s model=%s connection=%s", workspace_id, model, connection_artifact_id or "direct")

    try:
        msg_list = json.loads(messages)
    except json.JSONDecodeError:
        log.error("run_chat_turn: invalid messages JSON")
        return json.dumps({"error": "Invalid messages JSON"})

    # When using a connection artifact, route through Verso for credential resolution and metering
    if connection_artifact_id and workspace_id:
        if mcp_server_ids:
            log.warning(
                "mcp_server_ids provided with connection_artifact_id but the Verso path "
                "does not yet support dynamic external tool injection � ignoring mcp_server_ids."
            )
        working_messages = list(msg_list)
        if not working_messages or working_messages[0].get("role") != "system":
            working_messages = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}] + working_messages

        tool_calls_log: list[dict] = []

        for _ in range(_MAX_TOOL_ITERATIONS):
            # LLM call goes through Verso (credentials via Seraph, metering via Ophan)
            result = await _invoke_llm_via_verso(
                working_messages, connection_artifact_id, workspace_id,
            )

            if "error" in result:
                return json.dumps({"reply": f"LLM error: {result['error']}", "tool_calls": tool_calls_log, "messages": msg_list})

            reply_text = result.get("text", "")
            clean = [m for m in working_messages if m.get("role") != "system"]
            log.info("run_chat_turn (via Verso) complete � tool_calls=%d reply_len=%d", len(tool_calls_log), len(reply_text))
            return json.dumps({"reply": reply_text.strip(), "tool_calls": tool_calls_log, "messages": clean})

        clean = [m for m in working_messages if m.get("role") != "system"]
        return json.dumps({"reply": "I reached the tool call limit. Please try rephrasing.", "tool_calls": tool_calls_log, "messages": clean})

    # Direct OpenAI API call (with optional streaming when chat_artifact_id is provided)
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        log.error("run_chat_turn: OPENAI_API_KEY not set and no connection_artifact_id provided")
        return json.dumps({"error": "No LLM connection available. Set OPENAI_API_KEY or provide a connection_artifact_id."})

    stream_enabled = bool(chat_artifact_id)
    if stream_enabled:
        oai_client = openai.AsyncOpenAI(api_key=openai_key)
    else:
        oai_client = openai.OpenAI(api_key=openai_key)

    tool_calls_log: list[dict] = []

    # Build the combined tool surface
    all_tools = list(_CHAT_TOOLS)  # copy platform tools
    external_tool_map: dict[str, dict] = {}  # prefixed_name -> {server_id, original_name}

    if mcp_server_ids:
        try:
            server_ids = json.loads(mcp_server_ids)
            if isinstance(server_ids, list) and server_ids:
                ext_defs, external_tool_map = await _resolve_server_tools(server_ids)
                all_tools.extend(ext_defs)
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("Failed to parse mcp_server_ids: %s", e)

    working_messages = list(msg_list)
    if not working_messages or working_messages[0].get("role") != "system":
        working_messages = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}] + working_messages

    for iteration in range(_MAX_TOOL_ITERATIONS):

        # ---- Streaming path ------------------------------------------------
        if stream_enabled:
            async with httpx.AsyncClient() as delta_http:
                await _push_chat_status(delta_http, chat_artifact_id, workspace_id, "streaming")

                collected_content: list[str] = []
                tc_accum: dict[int, dict] = {}      # index -> {id, name, arguments}
                stream_usage = None
                delta_tasks: list[asyncio.Task] = []

                stream = await oai_client.chat.completions.create(
                    model=model, messages=working_messages, tools=all_tools,
                    tool_choice="auto", max_tokens=2048, temperature=0.7,
                    stream=True, stream_options={"include_usage": True},
                )
                async with stream:
                    async for chunk in stream:
                        if chunk.usage:
                            stream_usage = chunk.usage
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta

                        if delta.content:
                            collected_content.append(delta.content)
                            if chat_artifact_id:
                                delta_tasks.append(asyncio.create_task(
                                    _push_chat_delta(delta_http, chat_artifact_id, workspace_id, delta.content)
                                ))

                        if delta.tool_calls:
                            for tc_d in delta.tool_calls:
                                idx = tc_d.index
                                if idx not in tc_accum:
                                    tc_accum[idx] = {"id": "", "name": "", "arguments": ""}
                                if tc_d.id:
                                    tc_accum[idx]["id"] = tc_d.id
                                if tc_d.function:
                                    if tc_d.function.name:
                                        tc_accum[idx]["name"] += tc_d.function.name
                                    if tc_d.function.arguments:
                                        tc_accum[idx]["arguments"] += tc_d.function.arguments

                # Drain all delta push tasks concurrently before closing the HTTP client
                if delta_tasks:
                    await asyncio.gather(*delta_tasks, return_exceptions=True)

                content_str = "".join(collected_content)

                if stream_usage:
                    log.info(
                        "run_chat_turn LLM usage ⬩ model=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d",
                        model, stream_usage.prompt_tokens, stream_usage.completion_tokens, stream_usage.total_tokens,
                    )

                # Build assistant message
                msg_dict: dict = {"role": "assistant"}
                if content_str:
                    msg_dict["content"] = content_str
                if tc_accum:
                    msg_dict["tool_calls"] = [
                        {"id": v["id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}}
                        for _, v in sorted(tc_accum.items())
                    ]
                working_messages.append(msg_dict)

                if not tc_accum:
                    await _push_chat_status(delta_http, chat_artifact_id, workspace_id, "completed")
                    clean = [m for m in working_messages if m.get("role") != "system"]
                    log.info("run_chat_turn complete ⬩ tool_calls=%d reply_len=%d", len(tool_calls_log), len(content_str))
                    return json.dumps({"reply": content_str.strip(), "tool_calls": tool_calls_log, "messages": clean})

                # Execute tool calls, then continue the loop
                await _push_chat_status(delta_http, chat_artifact_id, workspace_id, "tool_calling")
            for _, tc_data in sorted(tc_accum.items()):
                try:
                    fn_args = json.loads(tc_data["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                tool_name = tc_data["name"]
                log.info("run_chat_turn tool call ⬩ name=%s", tool_name)

                if tool_name in external_tool_map:
                    ext_info = external_tool_map[tool_name]
                    result_str = await _execute_external_tool(ext_info["server_id"], ext_info["original_name"], fn_args)
                else:
                    result_str = await _execute_chat_tool(tool_name, fn_args, workspace_id)

                tool_calls_log.append({"name": tool_name, "arguments": fn_args, "result": result_str})
                working_messages.append({"role": "tool", "tool_call_id": tc_data["id"], "content": result_str})

            continue  # next iteration of the tool loop

        # ---- Non-streaming path (fallback) ----------------------------------
        resp = oai_client.chat.completions.create(
            model=model, messages=working_messages, tools=all_tools,
            tool_choice="auto", max_tokens=2048, temperature=0.7,
        )

        if resp.usage:
            log.info(
                "run_chat_turn LLM usage ⬩ model=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d",
                model, resp.usage.prompt_tokens, resp.usage.completion_tokens, resp.usage.total_tokens,
            )

        choice = resp.choices[0]
        msg = choice.message

        msg_dict: dict = {"role": "assistant"}
        if msg.content:
            msg_dict["content"] = msg.content
        if msg.tool_calls:
            msg_dict["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        working_messages.append(msg_dict)

        if not msg.tool_calls:
            clean = [m for m in working_messages if m.get("role") != "system"]
            log.info("run_chat_turn complete ⬩ tool_calls=%d reply_len=%d", len(tool_calls_log), len(msg.content or ""))
            return json.dumps({"reply": (msg.content or "").strip(), "tool_calls": tool_calls_log, "messages": clean})

        for tc in msg.tool_calls:
            try:
                fn_args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            tool_name = tc.function.name
            log.info("run_chat_turn tool call ⬩ name=%s", tool_name)

            # Route external tools through Core MCP proxy
            if tool_name in external_tool_map:
                ext_info = external_tool_map[tool_name]
                result_str = await _execute_external_tool(
                    ext_info["server_id"],
                    ext_info["original_name"],
                    fn_args,
                )
            else:
                result_str = await _execute_chat_tool(tool_name, fn_args, workspace_id)

            tool_calls_log.append({"name": tool_name, "arguments": fn_args, "result": result_str})
            working_messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

    if stream_enabled:
        async with httpx.AsyncClient() as h:
            await _push_chat_status(h, chat_artifact_id, workspace_id, "completed")
    clean = [m for m in working_messages if m.get("role") != "system"]
    return json.dumps({"reply": "I reached the tool call limit. Please try rephrasing.", "tool_calls": tool_calls_log, "messages": clean})


# ---------------------------------------------------------------------------
# UI Resources
# ---------------------------------------------------------------------------

@mcp.resource("ui://aria/vnd.agience.view.html")
async def view_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.view+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.view+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://aria/vnd.agience.chat.html")
async def chat_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.chat+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.chat+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting agience-server-aria � transport=%s", MCP_TRANSPORT)
    if MCP_TRANSPORT == "streamable-http":
        import uvicorn
        uvicorn.run(create_server_app(), host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()
