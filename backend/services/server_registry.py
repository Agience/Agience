"""First-party MCP server registry — manifest-driven.

Reads ``servers/manifest.json`` at import time to learn *which* first-party
servers exist.  UUIDs are NOT in the manifest — they are generated at
bootstrap and stored in ``platform_topology``.

Two-phase lifecycle:
  1. **Import time** — manifest metadata loaded (name, path, client_id, …).
     ``get_entry(name)`` and ``all_client_ids()`` work immediately.
  2. **Post-bootstrap** — ``populate_ids()`` pulls stable UUIDs from
     ``platform_topology``.  ``get_id(name)``, ``is_builtin_id(uuid)``,
     and ``build_http_config(name)`` require this phase.

Public API
----------
Metadata (available immediately):
- ``get_entry(name)``        → ``ManifestEntry``
- ``all_entries()``          → list of ``ManifestEntry``
- ``all_names()``            → list of str
- ``all_client_ids()``       → ``frozenset[str]``
- ``get_entry_by_client_id`` → ``ManifestEntry | None``

ID resolution (after ``populate_ids``):
- ``get_id(name)``           → str (UUID)
- ``get_name_by_id(uuid)``   → str | None
- ``is_builtin_id(uuid)``   → bool
- ``build_http_config(name)`` → ``MCPServerConfig``

Name→ID convenience:
- ``resolve_name_to_id(name)`` → str  (raises on unknown)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional
from urllib.parse import urlunsplit, urlsplit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestEntry:
    """Immutable record for one first-party MCP server (manifest only)."""
    name: str          # human-readable name — "aria", "verso", …
    title: str         # display title
    path: str          # HTTP path on the unified host (e.g. "/aria/mcp")
    client_id: str     # JWT audience / credential identity
    role: str          # one-line role description
    summary: str       # longer description for seed artifacts


# ---------------------------------------------------------------------------
# Manifest loading (import time)
# ---------------------------------------------------------------------------

def _load_manifest() -> List[ManifestEntry]:
    """Load and validate ``servers/manifest.json``."""
    candidates = [
        Path(__file__).resolve().parents[2] / "servers" / "manifest.json",   # services/ → backend/ → repo root
        Path("/servers/manifest.json"),                                       # Docker
    ]
    for p in candidates:
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            break
    else:
        raise FileNotFoundError(
            f"servers/manifest.json not found. Searched: {[str(c) for c in candidates]}"
        )

    entries: List[ManifestEntry] = []
    seen_names: set[str] = set()
    for item in raw:
        name = item["name"]
        if name in seen_names:
            raise ValueError(f"Duplicate server name in manifest: {name}")
        seen_names.add(name)
        entries.append(ManifestEntry(
            name=name,
            title=item["title"],
            path=item["path"],
            client_id=item["client_id"],
            role=item["role"],
            summary=item["summary"],
        ))
    return entries


# Module-level singletons — metadata populated at import time.
_ENTRIES: List[ManifestEntry] = _load_manifest()
_BY_NAME: Dict[str, ManifestEntry] = {e.name: e for e in _ENTRIES}
_BY_CLIENT_ID: Dict[str, ManifestEntry] = {e.client_id: e for e in _ENTRIES}
_ALL_CLIENT_IDS: FrozenSet[str] = frozenset(e.client_id for e in _ENTRIES)

# UUID indexes — populated after bootstrap via populate_ids().
_ID_BY_NAME: Dict[str, str] = {}        # name → UUID
_NAME_BY_ID: Dict[str, str] = {}        # UUID → name


# ---------------------------------------------------------------------------
# Metadata API (available immediately)
# ---------------------------------------------------------------------------

def get_entry(name: str) -> Optional[ManifestEntry]:
    """Look up manifest metadata by server name."""
    return _BY_NAME.get(name)


def get_entry_by_client_id(client_id: str) -> Optional[ManifestEntry]:
    """Look up manifest metadata by client_id."""
    return _BY_CLIENT_ID.get(client_id)


def all_entries() -> List[ManifestEntry]:
    """Return all manifest entries."""
    return list(_ENTRIES)


def all_names() -> List[str]:
    """Return all server names."""
    return [e.name for e in _ENTRIES]


def all_client_ids() -> FrozenSet[str]:
    """Return the frozenset of all first-party client_ids.

    Used by auth/gate fast-path to skip DB lookup for kernel servers.
    """
    return _ALL_CLIENT_IDS


# ---------------------------------------------------------------------------
# ID population (post-bootstrap)
# ---------------------------------------------------------------------------

def populate_ids() -> None:
    """Pull UUIDs from ``platform_topology`` for all manifest servers.

    Called once after ``pre_resolve_platform_ids()`` completes.
    Servers not yet registered in the topology are skipped (they will
    be populated on the next startup after seeding).
    """
    from services.platform_topology import get_id_optional as topo_get_id

    _ID_BY_NAME.clear()
    _NAME_BY_ID.clear()
    for entry in _ENTRIES:
        slug = f"agience-server-{entry.name}"
        uuid = topo_get_id(slug)
        if uuid:
            _ID_BY_NAME[entry.name] = uuid
            _NAME_BY_ID[uuid] = entry.name
    logger.info("Server registry populated: %d servers", len(_ID_BY_NAME))


# ---------------------------------------------------------------------------
# ID resolution API (requires populate_ids)
# ---------------------------------------------------------------------------

def get_id(name: str) -> Optional[str]:
    """Return the bootstrap-assigned UUID for a server name, or None."""
    return _ID_BY_NAME.get(name)


def get_name_by_id(server_id: str) -> Optional[str]:
    """Reverse lookup: UUID → server name. None if not a builtin."""
    return _NAME_BY_ID.get(server_id)


def is_builtin_id(server_id: str) -> bool:
    """True if ``server_id`` is a bootstrap-assigned UUID of a manifest server."""
    return server_id in _NAME_BY_ID


def resolve_name_to_id(name: str) -> str:
    """Resolve a server name to its bootstrap UUID.

    Raises ``ValueError`` if the name is not in the manifest or IDs have
    not been populated yet.
    """
    uuid = _ID_BY_NAME.get(name)
    if uuid is None:
        if name not in _BY_NAME:
            raise ValueError(f"Server name '{name}' is not in the manifest")
        raise ValueError(
            f"Server '{name}' is in the manifest but IDs have not been populated. "
            "Ensure bootstrap has completed before calling this function."
        )
    return uuid


# ---------------------------------------------------------------------------
# Transport config builder
# ---------------------------------------------------------------------------

def _derive_servers_host_uri() -> str:
    """Determine the base URI for the unified _host process."""
    explicit = (os.getenv("AGIENCE_SERVER_HOST_URI") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    from core import config as _cfg

    try:
        parsed = urlsplit(_cfg.BACKEND_URI)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname or "localhost"
        netloc = f"{hostname}:8082"
        return urlunsplit((scheme, netloc, "", "", "")).rstrip("/")
    except Exception:
        return "http://localhost:8082"


def build_http_config(name: str):
    """Build an ``MCPServerConfig`` for a manifest server by name.

    Raises ``ValueError`` if the name is not in the manifest.
    """
    entry = _BY_NAME.get(name)
    if entry is None:
        raise ValueError(f"Server '{name}' is not in the manifest")

    from mcp_client.contracts import MCPServerConfig, MCPServerTransport

    base_uri = _derive_servers_host_uri()
    server_id = _ID_BY_NAME.get(entry.name)
    if server_id is None:
        raise ValueError(
            f"Server '{name}' UUID not populated. "
            "Ensure bootstrap has completed before calling this function."
        )
    return MCPServerConfig(
        id=server_id,
        label=entry.title,
        transport=MCPServerTransport(type="http", well_known=f"{base_uri}{entry.path}"),
        notes=f"Built-in: {entry.role}",
    )


def build_http_config_by_id(server_id: str):
    """Build an ``MCPServerConfig`` for a manifest server by UUID.

    Raises ``ValueError`` if the UUID is not a known builtin.
    """
    name = _NAME_BY_ID.get(server_id)
    if name is None:
        raise ValueError(f"Server UUID '{server_id}' is not a known builtin")
    return build_http_config(name)
