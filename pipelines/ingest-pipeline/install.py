#!/usr/bin/env python3
"""
Agience Ingest Pipeline Installer
===================================
Creates (or idempotently updates) the Transform artifacts that compose the
multi-modal document ingestion pipeline in an Agience workspace.

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

To ingest a PDF, invoke the "Ingest Pipeline" transform with the PDF artifact:

    POST /artifacts/{ingest-pipeline-id}/invoke
    {
        "workspace_id": "<workspace-uuid>",
        "artifacts": ["<pdf-artifact-id>"]
    }

To swap the PDF extractor to a Docling (or other) MCP server, edit the
ingest-pdf-extract artifact in the Agience UI and change run.server / run.tool
to point at your registered third-party server artifact.
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


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install(api_url: str, api_key: str, workspace_id: str, *, dry_run: bool = False) -> None:
    api_url = api_url.rstrip("/")
    created = 0
    updated = 0
    skipped = 0

    print(f"Installing {len(TRANSFORMS)} transform(s) into workspace {workspace_id}")
    print(f"API: {api_url}\n")

    for transform in TRANSFORMS:
        slug = transform["slug"]
        context = transform["context"]

        existing = _find_by_slug(api_url, api_key, workspace_id, slug)

        if existing:
            artifact_id = existing.get("id") or existing.get("root_id")
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
                skipped += 1
                continue

            if dry_run:
                print(f"  [dry-run] {slug} — would update ({artifact_id})")
                updated += 1
                continue

            result = _update_artifact(api_url, api_key, artifact_id, context)
            print(f"  [updated] {slug} ({result.get('id') or artifact_id})")
            updated += 1

        else:
            if dry_run:
                print(f"  [dry-run] {slug} — would create")
                created += 1
                continue

            result = _create_artifact(api_url, api_key, workspace_id, slug, context)
            artifact_id = result.get("id")
            print(f"  [created] {slug} ({artifact_id})")
            created += 1

    print(f"\nDone. created={created} updated={updated} skipped={skipped}")


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
