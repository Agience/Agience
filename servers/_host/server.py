"""
servers-host — Unified MCP server host
================================================
Mounts all Agience server personas into a single FastAPI process.

Endpoints (each FastMCP app serves at /<name>/mcp):
  /aria/mcp    — Aria   (Presentation & Interface)     port 8083 → /aria/mcp
  /sage/mcp   — Sage   (Research & Retrieval)         port 8084 → /sage/mcp
  /atlas/mcp   — Atlas  (Provenance & Lineage)         port 8085 → /atlas/mcp
  /nexus/mcp   — Nexus  (Connectivity & Execution)     port 8086 → /nexus/mcp
  /astra/mcp   — Astra  (Ingestion & Indexing)         port 8087 → /astra/mcp
  /verso/mcp   — Verso  (Reasoning & Workflow)         port 8088 → /verso/mcp
  /seraph/mcp  — Seraph (Security & Governance)        port 8089 → /seraph/mcp
  /ophan/mcp   — Ophan  (Economic Operations)          port 8090 → /ophan/mcp

Astra also serves SRS stream webhook routes at /astra/stream/*.

All reachable on a single port (default: 8082).

Config:
  MCP_HOST               — Bind host (default: 0.0.0.0)
  MCP_PORT               — Bind port (default: 8082)
  LOG_LEVEL              — Logging level (default: INFO)
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType

import uvicorn
from fastapi import FastAPI

log = logging.getLogger("servers-host")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

# Suppress noisy per-request httpx/uvicorn logs (dozens of events/emit per chat turn)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8082"))
HOST_DIR = Path(__file__).resolve().parent

# Load PLATFORM_INTERNAL_SECRET from mounted file if not already in the
# environment.  This must happen before importing server modules because each
# module reads the env var at import time.
_secret_path = Path("/run/secrets/keys/platform_internal.secret")
if not os.getenv("PLATFORM_INTERNAL_SECRET") and _secret_path.exists():
    os.environ["PLATFORM_INTERNAL_SECRET"] = _secret_path.read_text().strip()
    log.info("Loaded PLATFORM_INTERNAL_SECRET from %s", _secret_path)

# Make the shared auth module importable before any server module is loaded.
_shared_dir = str(HOST_DIR.parent / "_shared")
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)

# ---------------------------------------------------------------------------
# Import each server module.
#
# Two layouts are supported:
#   - Local dev:  servers/_host/server.py  →  parent has servers/<name>/server.py
#   - Docker:     /app/server.py           →  flat /app/<name>_server.py files
#
# Detect which layout we're in by checking if the subdirectory structure exists.
# ---------------------------------------------------------------------------

_SERVERS_DIR_CANDIDATE = HOST_DIR.parent
_LOCAL_LAYOUT = (
    HOST_DIR.name == "_host"
    and (_SERVERS_DIR_CANDIDATE / "aria" / "server.py").exists()
)

if _LOCAL_LAYOUT:
    SERVERS_DIR = _SERVERS_DIR_CANDIDATE

    def _load_module(module_name: str, file_path: Path, *extra_paths: Path) -> ModuleType:
        search_paths = [file_path.parent, *extra_paths]
        for path in reversed(search_paths):
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)

        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load module {module_name!r} from {file_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    log.info("Local layout detected — loading server modules from %s", SERVERS_DIR)
    aria_server = _load_module("servers_host_aria_server", SERVERS_DIR / "aria" / "server.py")
    atlas_server = _load_module("servers_host_atlas_server", SERVERS_DIR / "atlas" / "server.py")
    astra_server = _load_module("servers_host_astra_server", SERVERS_DIR / "astra" / "server.py")
    sage_server = _load_module("servers_host_sage_server", SERVERS_DIR / "sage" / "server.py")
    nexus_server = _load_module("servers_host_nexus_server", SERVERS_DIR / "nexus" / "server.py")
    ophan_server = _load_module("servers_host_ophan_server", SERVERS_DIR / "ophan" / "server.py")
    seraph_server = _load_module("servers_host_seraph_server", SERVERS_DIR / "seraph" / "server.py")
    verso_server = _load_module("servers_host_verso_server", SERVERS_DIR / "verso" / "server.py")
else:
    log.info("Flat layout detected — importing server modules directly")
    import aria_server    # type: ignore[import-not-found]  # noqa: E402
    import atlas_server   # type: ignore[import-not-found]  # noqa: E402
    import astra_server   # type: ignore[import-not-found]  # noqa: E402
    import sage_server   # type: ignore[import-not-found]  # noqa: E402
    import nexus_server   # type: ignore[import-not-found]  # noqa: E402
    import ophan_server   # type: ignore[import-not-found]  # noqa: E402
    import seraph_server  # type: ignore[import-not-found]  # noqa: E402
    import verso_server   # type: ignore[import-not-found]  # noqa: E402


# ---------------------------------------------------------------------------
# FastAPI host
# ---------------------------------------------------------------------------

# Build the Starlette sub-apps and collect their session managers so we can
# explicitly run them during the host lifespan.  Mounted sub-apps' lifespans
# are not reliably triggered by the parent FastAPI app, which causes FastMCP's
# StreamableHTTPSessionManager task group to never start (RuntimeError:
# "Task group is not initialized").

_MCP_MODULES = [
    aria_server, atlas_server, astra_server, sage_server,
    nexus_server, ophan_server, seraph_server, verso_server,
]

_sub_apps: dict[str, object] = {}
_session_managers: list[object] = []
_startup_fns: list[object] = []

for _mod in _MCP_MODULES:
    # Individual server FastMCP instances default to host="127.0.0.1", which
    # causes MCP SDK >=1.23 to auto-enable DNS rebinding protection (only
    # allowing 127.0.0.1/localhost as Host headers).  In host mode *this*
    # process binds on 0.0.0.0 and is reached via Docker DNS ("servers:8082"),
    # so the per-server restriction must be cleared before we build the app.
    if getattr(_mod.mcp.settings, "transport_security", None) is not None:
        _mod.mcp.settings.transport_security = None
    create_fn = getattr(_mod, "create_server_app", None)
    _sub_app = create_fn() if create_fn else _mod.mcp.streamable_http_app()
    _name = _mod.mcp.name.replace("agience-server-", "")
    _sub_apps[_name] = _sub_app
    if _mod.mcp._session_manager is not None:
        _session_managers.append(_mod.mcp._session_manager)
    startup_fn = getattr(_mod, "server_startup", None)
    if startup_fn is not None:
        _startup_fns.append(startup_fn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for startup_fn in _startup_fns:
        await startup_fn()
    async with contextlib.AsyncExitStack() as stack:
        for sm in _session_managers:
            await stack.enter_async_context(sm.run())
        yield


app = FastAPI(title="Agience Servers", lifespan=lifespan)

# Astra needs both stream webhook routes AND MCP protocol endpoints under
# /astra.  Wrap them in a single FastAPI app so both are reachable:
#   /astra/stream/*  → stream routes (SRS webhooks)
#   /astra/mcp       → MCP protocol
_astra_combined = FastAPI()
try:
    if _LOCAL_LAYOUT:
        _stream_routes_mod = _load_module(
            "servers_host_astra_stream_routes",
            SERVERS_DIR / "astra" / "stream_routes.py",
        )
    else:
        import stream_routes as _stream_routes_mod  # type: ignore[import-not-found]
    _astra_combined.include_router(_stream_routes_mod.router)
    log.info("Astra stream routes loaded")
except Exception as exc:
    log.warning("Astra stream routes not loaded: %s", exc)
_astra_combined.mount("/", _sub_apps["astra"])

app.mount("/aria",   _sub_apps["aria"])
app.mount("/sage",   _sub_apps["sage"])
app.mount("/atlas",  _sub_apps["atlas"])
app.mount("/nexus",  _sub_apps["nexus"])
app.mount("/astra",  _astra_combined)
app.mount("/verso",  _sub_apps["verso"])
app.mount("/seraph", _sub_apps["seraph"])
app.mount("/ophan",  _sub_apps["ophan"])


@app.get("/")
def index():
    return {
        "service": "servers",
        "port": MCP_PORT,
        "servers": [
            {"name": "aria",   "endpoint": "/aria/mcp",   "role": "Presentation & Interface"},
            {"name": "sage",   "endpoint": "/sage/mcp",   "role": "Research & Retrieval"},
            {"name": "atlas",  "endpoint": "/atlas/mcp",  "role": "Provenance & Lineage"},
            {"name": "nexus",  "endpoint": "/nexus/mcp",  "role": "Connectivity & Execution"},
            {"name": "astra",  "endpoint": "/astra/mcp",  "role": "Ingestion & Indexing"},
            {"name": "verso",  "endpoint": "/verso/mcp",  "role": "Reasoning & Workflow"},
            {"name": "seraph", "endpoint": "/seraph/mcp", "role": "Security & Governance"},
            {"name": "ophan",  "endpoint": "/ophan/mcp",  "role": "Economic Operations"},
        ],
    }


if __name__ == "__main__":
    log.info("Starting servers-host — host=%s port=%s", MCP_HOST, MCP_PORT)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, access_log=True)
