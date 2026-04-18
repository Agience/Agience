"""
agience-server-sage � MCP Server
=====================================
Research & Retrieval: discovering sources, gathering evidence, ranking relevance,
evidence-backed synthesis, hybrid search, and structured extraction.

Interior server � works entirely within the platform (no external credentials needed
beyond the Agience API key).

Tools
-----
  search                    � Hybrid search across workspaces and collections
  get_artifact                  � Fetch a card by ID
  browse_collections        � List and explore committed collections
  search_azure              � Search via Azure AI Search (optional connector)
  index_to_azure            � Project cards into an Azure Search index (optional)
  research                  � Multi-step retrieval + LLM synthesis (stub)
  cite_sources              � Produce provenance receipts for a synthesised answer (stub)
  ask                       � Ask a question with optional card context (stub)
  extract_information       � Extract structured fields from a card's content (stub)
  generate_meeting_insights � Derive summary/actions/coaching from a transcript card
                              (migrated from agience-server-aria in taxonomy redesign)

Auth
----
  PLATFORM_INTERNAL_SECRET  ⬩ Shared deployment secret for client_credentials token exchange
  AGIENCE_API_URI           ⬩ Base URI of the agience-core backend

Transport
---------
  MCP_TRANSPORT=streamable-http
  MCP_HOST=0.0.0.0
  MCP_PORT=8084
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from azure_search import AzureSearchClient, parse_connection, upsert_artifacts

log = logging.getLogger("agience-server-sage")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

AGIENCE_API_URI: str = os.getenv("AGIENCE_API_URI", "http://localhost:8081").rstrip("/")
PLATFORM_INTERNAL_SECRET: str | None = os.getenv("PLATFORM_INTERNAL_SECRET")
SAGE_CLIENT_ID: str = "agience-server-sage"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8084"))


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
                    "client_id": SAGE_CLIENT_ID,
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

_auth = _AgieceServerAuth(SAGE_CLIENT_ID, AGIENCE_API_URI)


async def _user_headers() -> dict[str, str]:
    """Return headers with the verified delegation JWT, or fall back to server token."""
    return await _auth.user_headers(_exchange_token)


def create_server_app():
    """Return the Sage ASGI app with verified middleware and startup hooks."""
    return _auth.create_app(mcp, _exchange_token)


async def server_startup() -> None:
    """Run Sage startup tasks: Core JWKS fetch + server key registration."""
    await _auth.startup(_exchange_token)


mcp = FastMCP(
    "agience-server-sage",
    instructions=(
        "You are connected to Sage, the Agience research and retrieval server. "
        "Use Sage to search across workspaces and collections, fetch cards by ID, "
        "synthesise evidence-backed answers, and project cards into external search indexes."
    ),
)
from artifact_helpers import register_types_manifest
register_types_manifest(mcp, "sage", __file__)

# ---------------------------------------------------------------------------
# Tool: search � delegates to platform /search
# ---------------------------------------------------------------------------

@mcp.tool(description="Hybrid semantic + keyword search across workspaces and collections.")
async def search(
    query: str,
    workspace_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    payload: dict[str, Any] = {"query": query, "limit": limit}
    if workspace_id:
        payload["workspace_id"] = workspace_id

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/search",
            headers=await _headers(),
            json=payload,
            timeout=30,
        )
    if resp.status_code >= 400:
        return f"Error: {resp.status_code} � {resp.text[:300]}"

    results = resp.json()
    if not results:
        return "No results found."
    lines = []
    for r in results[:limit]:
        lines.append(f"[{r.get('id')}] {r.get('title', '(untitled)')} � {r.get('content_type', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: get_artifact
# ---------------------------------------------------------------------------

@mcp.tool(description="Fetch a card by ID. Returns full card content and context.")
async def get_artifact(artifact_id: str, workspace_id: Optional[str] = None) -> str:
    url = (
        f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}"
        if workspace_id
        else f"{AGIENCE_API_URI}/artifacts/{artifact_id}"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=await _headers(), timeout=15)
    if resp.status_code >= 400:
        return f"Error: {resp.status_code} � {resp.text[:300]}"
    return json.dumps(resp.json(), indent=2)


# ---------------------------------------------------------------------------
# Tool: browse_collections
# ---------------------------------------------------------------------------

@mcp.tool(description="List committed collections accessible to the current user.")
async def browse_collections(limit: int = 20) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/collections",
            headers=await _headers(),
            params={"limit": limit},
            timeout=15,
        )
    if resp.status_code >= 400:
        return f"Error: {resp.status_code} � {resp.text[:300]}"
    cols = resp.json()
    if not cols:
        return "No collections found."
    return "\n".join(f"[{c.get('id')}] {c.get('name', '(unnamed)')}" for c in cols)


# ---------------------------------------------------------------------------
# Tool: search_azure � uses azure_search.py adapter
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Search cards via an Azure AI Search index. "
        "Requires a connection dict with endpoint, api_key, docs_index. "
        "Use when the workspace has cards projected into Azure Search."
    )
)
async def search_azure(
    query: str,
    connection: dict,
    top: int = 10,
) -> str:
    """
    Args:
        query: Search query string.
        connection: Azure Search config � {endpoint, api_key, api_version?, docs_index?, chunks_index?}
        top: Max results to return.
    """
    try:
        cfg = parse_connection(connection)
        client = AzureSearchClient(cfg)
        results = await client.search(cfg.docs_index, query, top=top)
    except Exception as exc:
        return f"Azure Search error: {exc}"

    if not results:
        return "No results from Azure Search."
    lines = [f"[{r.get('id')}] {r.get('title', '(untitled)')}" for r in results]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: index_to_azure � projects cards into Azure Search
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Project workspace cards into an Azure AI Search index. "
        "Azure Search is a derived index � all source-of-truth lives in the platform."
    )
)
async def index_to_azure(
    workspace_id: str,
    connection: dict,
    artifact_ids: Optional[list[str]] = None,
) -> str:
    """
    Args:
        workspace_id: Source workspace.
        connection: Azure Search config dict.
        artifact_ids: Optional list of specific card IDs to index. If omitted, indexes all cards.
    """
    # Fetch artifacts from platform
    params: dict[str, Any] = {}
    if artifact_ids:
        params["ids"] = ",".join(artifact_ids)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts",
            headers=await _headers(),
            params=params,
            timeout=30,
        )
    if resp.status_code >= 400:
        return f"Error fetching cards: {resp.status_code} � {resp.text[:300]}"

    cards = resp.json()
    if not cards:
        return "No artifacts to index."

    docs = [{"_id": c["id"], "title": c.get("title", ""), "content": c.get("content", ""), **c} for c in cards]
    try:
        upsert_artifacts(connection=connection, docs=docs)
    except Exception as exc:
        return f"Azure indexing error: {exc}"

    return f"Indexed {len(docs)} card(s) to Azure Search."


# ---------------------------------------------------------------------------
# Tool stubs (Phase 1d implementation)
# ---------------------------------------------------------------------------

@mcp.tool(description="Multi-step retrieval + LLM synthesis constrained by evidence.")
async def research(query: str, workspace_id: Optional[str] = None) -> str:
    return f"TODO: multi-step research not yet implemented. query={query!r}"


@mcp.tool(description="Produce a provenance receipt citing the cards used in a synthesised answer.")
async def cite_sources(artifact_ids: list[str], answer: str) -> str:
    return f"TODO: citation formatting not yet implemented. sources={artifact_ids}"


@mcp.tool(description="Ask a question with optional card context � search + synthesise an answer.")
async def ask(
    question: str,
    workspace_id: Optional[str] = None,
    artifact_ids: Optional[list[str]] = None,
) -> str:
    """
    Args:
        question: Natural language question.
        workspace_id: Optional workspace to scope the search.
        artifact_ids: Optional list of card IDs to use as grounding context.
    """
    return f"TODO: ask not yet implemented. question={question!r}"


@mcp.tool(description="Extract structured fields from a card's content using a provided JSON schema.")
async def extract_information(
    artifact_id: str,
    workspace_id: str,
    schema: dict,
) -> str:
    """
    Args:
        artifact_id: ID of the card whose content to parse.
        workspace_id: Workspace the card belongs to.
        schema: JSON Schema describing the fields to extract.
    """
    return f"TODO: extract_information not yet implemented. artifact_id={artifact_id!r}"


@mcp.tool(
    description=(
        "Derive a meeting summary, action items, and coaching insights from a transcript card. "
        "Migrated from agience-server-aria (taxonomy redesign \u2014 analysis belongs in Sage)."
    )
)
async def generate_meeting_insights(
    artifact_id: str,
    workspace_id: str,
    format: str = "markdown",
) -> str:
    """
    Args:
        artifact_id: ID of the transcript card to analyse.
        workspace_id: Workspace the card belongs to.
        format: Output format � 'markdown' (default) or 'json'.
    """
    return f"TODO: generate_meeting_insights not yet implemented. artifact_id={artifact_id!r}"


# ---------------------------------------------------------------------------
# UI Resources
# ---------------------------------------------------------------------------

@mcp.resource("ui://sage/vnd.agience.research.html")
async def research_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.research+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.research+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting agience-server-sage � transport=%s", MCP_TRANSPORT)
    if MCP_TRANSPORT == "streamable-http":
        import uvicorn
        uvicorn.run(create_server_app(), host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()
