#!/usr/bin/env python3
"""
Agience Ingest Pipeline Installer
===================================
Creates (or idempotently updates) the Transform artifacts that compose the
multi-modal document ingestion pipeline in an Agience workspace, and creates
relationship edges linking each transform to its target MCP server.

Usage
-----
    python install.py \\
        --api-url https://your-agience.example.com \\
        --api-key  sk-... \\
        --workspace-id <workspace-uuid>

    # Or via environment variables:
    AGIENCE_API_URL=...  AGIENCE_API_KEY=...  AGIENCE_WORKSPACE_ID=... python install.py

After running, the workspace will contain five Transform artifacts with stable
slugs that you can invoke via POST /artifacts/{id}/invoke or chain in a workflow:

    ingest-dedup              — SHA-256 deduplication check
    ingest-pdf-extract        — PDF text extraction (built-in PyPDF; swap to Docling)
    ingest-extract-metadata   — LLM-based metadata extraction
    ingest-apply-metadata     — Write extracted metadata back to source artifact
    ingest-pipeline           — Full pipeline orchestrator (runs all four steps)

Each transform is linked to its MCP server via a relationship edge (not a
context field). The ``server_name`` or ``orchestrator_name`` in each transform
definition is resolved to a server artifact UUID and linked via a
``relationship="server"`` or ``relationship="orchestrator"`` edge.

To ingest a PDF, invoke the "Ingest Pipeline" transform with the PDF artifact:

    POST /artifacts/{ingest-pipeline-id}/invoke
    {
        "workspace_id": "<workspace-uuid>",
        "artifacts": ["<pdf-artifact-id>"]
    }

To swap the PDF extractor to a different MCP server, delete the existing
server relationship edge and create a new one pointing at your registered
third-party server artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Install with: pip install httpx", file=sys.stderr)
    sys.exit(1)

from transforms import TRANSFORMS


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _find_by_slug(api_url: str, api_key: str, workspace_id: str, slug: str) -> Optional[dict]:
    """Return the first artifact in the workspace with the given slug, or None."""
    resp = httpx.get(
        f"{api_url}/artifacts/list",
        headers=_headers(api_key),
        params={"container_id": workspace_id},
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or data.get("artifacts") or (data if isinstance(data, list) else [])
    return next((item for item in items if item.get("slug") == slug), None)


def _find_server_by_name(api_url: str, api_key: str, name: str) -> Optional[dict]:
    """Search for an MCP server artifact by name.

    Searches across all containers for an artifact whose context.title or
    slug matches the given server name.
    """
    resp = httpx.post(
        f"{api_url}/artifacts/search",
        headers=_headers(api_key),
        json={
            "query": name,
            "content_types": ["application/vnd.agience.mcp-server+json"],
            "limit": 10,
        },
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or data.get("results") or []
    # Match by slug or title
    for item in items:
        if item.get("slug") == name:
            return item
        ctx = item.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = {}
        if ctx.get("title", "").lower() == name.lower():
            return item
    return items[0] if items else None


def _create_artifact(api_url: str, api_key: str, workspace_id: str, slug: str, context: dict) -> dict:
    payload = {
        "container_id": workspace_id,
        "slug": slug,
        "context": json.dumps(context),
        "content": "",
        "content_type": context.get("content_type"),
    }
    resp = httpx.post(
        f"{api_url}/artifacts",
        headers=_headers(api_key),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _update_artifact(api_url: str, api_key: str, artifact_id: str, context: dict) -> dict:
    payload = {
        "context": json.dumps(context),
        "content_type": context.get("content_type"),
    }
    resp = httpx.patch(
        f"{api_url}/artifacts/{artifact_id}",
        headers=_headers(api_key),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _create_relationship(
    api_url: str, api_key: str, source_id: str, target_id: str, relationship: str,
) -> dict:
    """Create a relationship edge from source to target."""
    resp = httpx.post(
        f"{api_url}/artifacts/{source_id}/relationships",
        headers=_headers(api_key),
        json={"target_id": target_id, "relationship": relationship},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install(api_url: str, api_key: str, workspace_id: str, *, dry_run: bool = False) -> None:
    api_url = api_url.rstrip("/")
    created = 0
    updated = 0
    skipped = 0
    edges_created = 0

    print(f"Installing {len(TRANSFORMS)} transform(s) into workspace {workspace_id}")
    print(f"API: {api_url}\n")

    # Phase 1: Resolve all referenced server names to artifact UUIDs
    server_names: set[str] = set()
    for t in TRANSFORMS:
        if t.get("server_name"):
            server_names.add(t["server_name"])
        if t.get("orchestrator_name"):
            server_names.add(t["orchestrator_name"])

    server_ids: dict[str, str] = {}
    for name in server_names:
        server = _find_server_by_name(api_url, api_key, name)
        if not server:
            print(f"  ERROR: Server '{name}' not found. Register it before installing.", file=sys.stderr)
            sys.exit(1)
        sid = server.get("root_id") or server.get("id")
        server_ids[name] = sid
        print(f"  [server]  {name} → {sid}")

    print()

    # Phase 2: Create/update transform artifacts
    artifact_ids: dict[str, str] = {}  # slug → artifact uuid

    for transform in TRANSFORMS:
        slug = transform["slug"]
        context = transform["context"]

        existing = _find_by_slug(api_url, api_key, workspace_id, slug)

        if existing:
            artifact_id = existing.get("root_id") or existing.get("id")
            existing_ctx_raw = existing.get("context") or {}
            if isinstance(existing_ctx_raw, str):
                try:
                    existing_ctx = json.loads(existing_ctx_raw)
                except json.JSONDecodeError:
                    existing_ctx = {}
            else:
                existing_ctx = existing_ctx_raw

            if existing_ctx == context:
                print(f"  [skip]    {slug} — already up to date ({artifact_id})")
                artifact_ids[slug] = artifact_id
                skipped += 1
                continue

            if dry_run:
                print(f"  [dry-run] {slug} — would update ({artifact_id})")
                artifact_ids[slug] = artifact_id
                updated += 1
                continue

            result = _update_artifact(api_url, api_key, artifact_id, context)
            artifact_ids[slug] = result.get("root_id") or result.get("id") or artifact_id
            print(f"  [updated] {slug} ({artifact_ids[slug]})")
            updated += 1

        else:
            if dry_run:
                print(f"  [dry-run] {slug} — would create")
                created += 1
                continue

            result = _create_artifact(api_url, api_key, workspace_id, slug, context)
            artifact_id = result.get("root_id") or result.get("id")
            artifact_ids[slug] = artifact_id
            print(f"  [created] {slug} ({artifact_id})")
            created += 1

    # Phase 3: Create relationship edges from transforms to servers
    print()
    for transform in TRANSFORMS:
        slug = transform["slug"]
        art_id = artifact_ids.get(slug)
        if not art_id:
            continue

        for rel_field, rel_type in [("server_name", "server"), ("orchestrator_name", "orchestrator")]:
            name = transform.get(rel_field)
            if not name:
                continue
            target_id = server_ids.get(name)
            if not target_id:
                continue

            if dry_run:
                print(f"  [dry-run] {slug} —{rel_type}→ {name} ({target_id})")
                edges_created += 1
                continue

            try:
                _create_relationship(api_url, api_key, art_id, target_id, rel_type)
                print(f"  [edge]    {slug} —{rel_type}→ {name} ({target_id})")
                edges_created += 1
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    print(f"  [skip]    {slug} —{rel_type}→ {name} (already exists)")
                else:
                    raise

    print(f"\nDone. created={created} updated={updated} skipped={skipped} edges={edges_created}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install Agience ingest pipeline Transform artifacts into a workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("AGIENCE_API_URL"),
        help="Agience API base URL (env: AGIENCE_API_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("AGIENCE_API_KEY"),
        help="Agience API key or bearer token (env: AGIENCE_API_KEY)",
    )
    parser.add_argument(
        "--workspace-id",
        default=os.getenv("AGIENCE_WORKSPACE_ID"),
        help="Target workspace UUID (env: AGIENCE_WORKSPACE_ID)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created/updated without making changes",
    )

    args = parser.parse_args()

    missing = [name for name, val in [
        ("--api-url", args.api_url),
        ("--api-key", args.api_key),
        ("--workspace-id", args.workspace_id),
    ] if not val]

    if missing:
        parser.error(f"Missing required arguments: {', '.join(missing)}")

    try:
        install(
            args.api_url,
            args.api_key,
            args.workspace_id,
            dry_run=args.dry_run,
        )
    except httpx.HTTPStatusError as exc:
        print(f"\nERROR: API returned {exc.response.status_code}: {exc.response.text[:300]}", file=sys.stderr)
        sys.exit(1)
    except httpx.RequestError as exc:
        print(f"\nERROR: Request failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
