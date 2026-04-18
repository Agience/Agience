"""
agience-server-atlas � MCP Server
====================================
Provenance, Attribution & Lineage: tracing, conflicts, contracts, graph traversal,
boundary enforcement.

Tools
-----
  check_provenance     � Trace a card's version lineage
  detect_conflicts     � Find cards with conflicting claims
  apply_contract       � Validate a workspace against a contract
  suggest_merge        � Propose a merge of two divergent cards (stub)
  traverse_graph       � Follow relationship edges in the collection graph (stub)
  attribute_source     � Link a card to its origin (stub)
  check_coherence      � Assess logical coherence across a set of cards (stub)


Auth
----
  PLATFORM_INTERNAL_SECRET — Shared deployment secret for kernel server auth (set on all platform components)
  AGIENCE_API_URI          — Base URI of the agience-core backend

Transport
---------
  MCP_TRANSPORT=streamable-http
  MCP_HOST=0.0.0.0
  MCP_PORT=8085
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("agience-server-atlas")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

AGIENCE_API_URI: str = os.getenv("AGIENCE_API_URI", "http://localhost:8081").rstrip("/")
PLATFORM_INTERNAL_SECRET: str | None = os.getenv("PLATFORM_INTERNAL_SECRET")
ATLAS_CLIENT_ID: str = "agience-server-atlas"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8085"))


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
                    "client_id": ATLAS_CLIENT_ID,
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

_auth = _AgieceServerAuth(ATLAS_CLIENT_ID, AGIENCE_API_URI)


async def _user_headers() -> dict[str, str]:
    """Return headers with the verified delegation JWT, or fall back to server token."""
    return await _auth.user_headers(_exchange_token)


def create_server_app():
    """Return the Atlas ASGI app with verified middleware and startup hooks."""
    return _auth.create_app(mcp, _exchange_token)


async def server_startup() -> None:
    """Run Atlas startup tasks: Core JWKS fetch + server key registration."""
    await _auth.startup(_exchange_token)


mcp = FastMCP(
    "agience-server-atlas",
    instructions=(
        "You are Atlas, the Agience provenance, attribution, and lineage server. "
        "Use Atlas to trace card lineage, detect conflicting claims, enforce contracts, "
        "attribute content to its sources, and reason about the graph structure of "
        "committed knowledge."
    ),
)

from artifact_helpers import register_types_manifest
register_types_manifest(mcp, "atlas", __file__)


# ---------------------------------------------------------------------------
# Tool: check_provenance
# ---------------------------------------------------------------------------

@mcp.tool(description="Trace a card's version lineage and source attribution.")
async def check_provenance(artifact_id: str) -> str:
    """
    Args:
        artifact_id: Card to trace. Works for both workspace cards and committed versions.
    """
    # TODO(agents-invoke-removal): "provenance:check" is a legacy agent plugin that
    # does not exist in backend/agents/ and has no entry in _OPERATOR_TO_SERVER.
    # This tool has no working implementation path; leaving for manual design review.
    _ = artifact_id  # unused — kept for stable tool signature
    return json.dumps({"error": "check_provenance is not yet implemented"})


# ---------------------------------------------------------------------------
# Tool: detect_conflicts
# ---------------------------------------------------------------------------

@mcp.tool(description="Find cards in a workspace or collection that contain conflicting claims.")
async def detect_conflicts(
    workspace_id: Optional[str] = None,
    collection_id: Optional[str] = None,
    focus_artifact_id: Optional[str] = None,
) -> str:
    """
    Args:
        workspace_id: Scope to a specific workspace.
        collection_id: Scope to a specific committed collection.
        focus_artifact_id: Narrow conflict detection to claims in this card.
    """
    # TODO(agents-invoke-removal): "provenance:detect_conflicts" is a legacy
    # agent plugin that does not exist in backend/agents/ and has no entry in
    # _OPERATOR_TO_SERVER. This tool has no working implementation path; leaving
    # for manual design review.
    _ = (workspace_id, collection_id, focus_artifact_id)  # unused — kept for stable tool signature
    return json.dumps({"error": "detect_conflicts is not yet implemented"})


# ---------------------------------------------------------------------------
# Tool: apply_contract
# Aligns with backend/agents/contracts.py
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Validate workspace cards against a contract card that defines "
        "required fields, allowed values, or structural rules."
    )
)
async def apply_contract(
    workspace_id: str,
    contract_artifact_id: str,
) -> str:
    """
    Args:
        workspace_id: Workspace to validate.
        contract_artifact_id: ID of the contract card defining the rules.
    """
    # TODO(agents-invoke-removal): "contracts:apply" is a legacy agent plugin
    # that does not exist in backend/agents/ and has no entry in
    # _OPERATOR_TO_SERVER. This tool has no working implementation path;
    # leaving for manual design review.
    _ = (workspace_id, contract_artifact_id)  # unused — kept for stable tool signature
    return json.dumps({"error": "apply_contract is not yet implemented"})


# ---------------------------------------------------------------------------
# Tool stubs (Phase 1e implementation)
# ---------------------------------------------------------------------------

@mcp.tool(description="Propose a merge of two divergent versions of a card.")
async def suggest_merge(artifact_id_a: str, artifact_id_b: str) -> str:
    return f"TODO: suggest_merge not yet implemented. a={artifact_id_a}, b={artifact_id_b}"


@mcp.tool(description="Traverse relationship edges from a card in the knowledge graph.")
async def traverse_graph(artifact_id: str, depth: int = 2, edge_type: Optional[str] = None) -> str:
    return f"TODO: traverse_graph not yet implemented. artifact_id={artifact_id}, depth={depth}"


@mcp.tool(description="Link a card to its origin � person, event, tool, or external document.")
async def attribute_source(
    artifact_id: str,
    source_type: str,
    source_ref: str,
) -> str:
    """Attach source attribution metadata to a card."""
    return f"TODO: attribute_source not yet implemented. artifact_id={artifact_id}"


@mcp.tool(description="Assess logical coherence across a set of cards.")
async def check_coherence(
    artifact_ids: list[str],
    workspace_id: Optional[str] = None,
) -> str:
    """Check for logical inconsistencies, contradictions, or gaps across the given cards."""
    return "TODO: check_coherence not yet implemented."


# ---------------------------------------------------------------------------
# Resources: HTML Views
# ---------------------------------------------------------------------------

import pathlib as _pathlib

def _read_view(content_type_dir: str) -> str:
    """Read a view.html from the atlas ui directory."""
    view_path = _pathlib.Path(__file__).parent / "ui" / "application" / content_type_dir / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://atlas/vnd.agience.workspace.html")
async def workspace_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.workspace+json (bindings editor)."""
    return _read_view("vnd.agience.workspace+json")


@mcp.resource("ui://atlas/vnd.agience.history.html")
async def history_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.history+json."""
    return _read_view("vnd.agience.history+json")


@mcp.resource("ui://atlas/vnd.agience.provenance.html")
async def provenance_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.provenance+json."""
    return _read_view("vnd.agience.provenance+json")



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting agience-server-atlas � transport=%s", MCP_TRANSPORT)
    if MCP_TRANSPORT == "streamable-http":
        import uvicorn
        uvicorn.run(create_server_app(), host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()
