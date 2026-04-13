from datetime import datetime, timezone
import asyncio
import json
import os
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from mcp_server.server import create_mcp_app
from schemas.arango.loader import init_arango_db, check_arango_health
from search.init_search import init_search_indices, check_search_health, shutdown_search
from search.ingest.index_queue import start_worker as start_index_worker, stop_worker as stop_index_worker
from routers.auth_router import auth_router as auth_router, root_router as auth_root_router
from routers.secrets_router import router as secrets_router
from routers.api_keys_router import router as api_keys_router
from routers.server_credentials_router import router as server_credentials_router
from routers.mcp_router import router as mcp_router
from routers.relay_router import router as relay_router
from routers.types_router import router as types_router
from routers.platform_router import platform_router
from routers.setup_router import setup_router
from routers.passkey_router import passkey_router
from routers.otp_router import otp_router
from routers.artifacts_router import router as artifacts_router
from routers.agents_router import router as agents_router
from routers.grants_router import router as grants_router
from routers.gate_router import gate_router
from routers.events_router import router as events_router
from routers.auth_router import reload_oauth_providers
from core import config

# ----------------------------
# Logging setup (pre-Phase 2 — uses hardcoded defaults until config loads)
# ----------------------------
debug_level_map = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
# Initial log level from default; reconfigured after Phase 2
logging.Formatter.converter = time.gmtime
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03dZ %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger("agience.api")

logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("openai").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)


class _MCPClosedResourceFilter(logging.Filter):
    """Suppress benign ClosedResourceError noise from MCP SDK."""
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc = record.exc_info[1]
            if type(exc).__name__ == "ClosedResourceError":
                return False
        return True


class _EventsEmitAccessFilter(logging.Filter):
    """Suppress POST /events/emit from uvicorn access log.

    Chat streaming emits dozens of delta events per turn; logging every one
    drowns out useful access log entries.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "POST /events/emit" in msg:
            return False
        return True


logging.getLogger("mcp.server.streamable_http").addFilter(_MCPClosedResourceFilter())
logging.getLogger("uvicorn.access").addFilter(_EventsEmitAccessFilter())
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("openai._base_client").setLevel(logging.ERROR)
logging.getLogger("httpx._client").setLevel(logging.ERROR)
logging.getLogger("opensearch").setLevel(logging.ERROR)

# ----------------------------
# Build Info
# ----------------------------
BUILD_INFO_PATH = os.getenv("BUILD_INFO_PATH", "/app/build_info.json")

def _load_build_info():
    for candidate in [BUILD_INFO_PATH, str(Path(__file__).resolve().parent.parent / "build_info.json")]:
        try:
            return json.loads(Path(candidate).read_text(encoding="utf-8"))
        except Exception:
            continue
    return {"version": "", "build_time": ""}

BUILD_INFO = _load_build_info()

# Track setup mode globally so middleware and routes can check it
_setup_mode = True


def _redact_setup_token(token: str) -> str:
    """Return a short, non-sensitive representation of the setup token."""
    if not token:
        return "<missing>"
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def _run_phase4_core_sync(loop) -> None:
    """Run core Phase 4 initialization (ArangoDB, seeding, operator bootstrap).

    Called from run_phase4_after_setup() via asyncio.to_thread(). Completing
    this function is sufficient to unblock platform routes (_setup_mode = False).
    OpenSearch initialization runs separately in _run_phase4_search_sync.
    """
    from core.key_manager import delete_setup_token as _delete_setup_token
    _delete_setup_token()

    arango_db = init_arango_db()

    try:
        from services.platform_topology import pre_resolve_platform_ids
        pre_resolve_platform_ids(arango_db)
        from services import server_registry
        server_registry.populate_ids()
    except Exception:
        logger.exception("Platform ID pre-resolution failed at startup (fatal)")
        raise

    try:
        from services.seed_content_service import ensure_all_seed_collections
        ensure_all_seed_collections(arango_db)
    except Exception:
        logger.exception("Seed collection setup failed at startup (non-fatal)")

    try:
        from services.authority_content_service import ensure_current_instance_authority
        ensure_current_instance_authority(arango_db)
    except Exception:
        logger.exception("Authority collection setup failed at startup (non-fatal)")

    try:
        from services.host_content_service import ensure_current_instance_host
        ensure_current_instance_host(arango_db)
    except Exception:
        logger.exception("Host collection setup failed at startup (non-fatal)")

    try:
        from services.resources_content_service import ensure_platform_resources
        ensure_platform_resources(arango_db)
    except Exception:
        logger.exception("Resources collection setup failed at startup (non-fatal)")

    try:
        # Phase 7 — Server Artifact Proxy: seed one vnd.agience.mcp-server+json
        # artifact per first-party persona in the all-servers collection.
        from services.servers_content_service import ensure_platform_servers
        ensure_platform_servers(arango_db)
    except Exception:
        logger.exception("Platform MCP servers setup failed at startup (non-fatal)")

    try:
        from services.llm_connections_content_service import ensure_llm_connections_collection
        ensure_llm_connections_collection(arango_db)
    except Exception:
        logger.exception("LLM connections collection setup failed at startup (non-fatal)")

    try:
        from services.inbox_seeds_content_service import ensure_all_seed_sub_collections
        ensure_all_seed_sub_collections(arango_db)
    except Exception:
        logger.exception("Inbox seeds sub-collection setup failed at startup (non-fatal)")

    try:
        _ensure_operator_bootstrapped(arango_db)
    except Exception:
        logger.exception("Operator Arango bootstrap failed at startup (non-fatal)")

    from core import event_bus
    event_bus.set_event_loop(loop)

    # Register built-in operation dispatch handlers (mcp_tool, native, artifact_crud).
    # Part of the Phase 0 Enterprise Eventing refactor: the operation dispatcher
    # reads `operations.*.dispatch.kind` from type.json and looks up the handler here.
    try:
        from services import handler_registry
        handler_registry.register_builtin_handlers()
    except Exception:
        logger.exception("Operation handler registry bootstrap failed at startup (non-fatal)")

    # Ensure the content bucket exists post-setup (config.CONTENT_URI is now set
    # from the settings written by the setup wizard).
    try:
        from services.content_service import reinit_edge_clients, ensure_content_bucket
        reinit_edge_clients()
        ensure_content_bucket()
    except Exception:
        logger.warning("Content bucket provisioning after setup failed (non-fatal)", exc_info=True)


def _run_phase4_search_sync(*, is_post_setup: bool = False) -> None:
    """Run OpenSearch initialization steps (separate from core to avoid blocking platform routes)."""
    try:
        from schemas.opensearch.initialize import init_opensearch_security
        init_opensearch_security()
    except Exception:
        logger.exception("OpenSearch security provisioning failed at startup (non-fatal)")

    init_search_indices()
    try:
        start_index_worker()
    except Exception as e:
        logger.error("Failed to start indexing worker: %s", e)

    # After setup, all seed content was created before OpenSearch was ready.
    # Reindex everything now so search has the full picture from the start.
    if is_post_setup:
        from search.init_search import _reindex_all_artifacts
        logger.info("Post-setup: reindexing all artifacts into OpenSearch ...")
        threading.Thread(target=_reindex_all_artifacts, daemon=True).start()


async def run_phase4_after_setup() -> None:
    """Run Phase 4 initialization in the background after setup completes.

    Called from the setup complete endpoint as a background task instead of
    restarting the process. The MCP server session manager is already running
    from Phase 3 so that step is skipped.

    Sets _setup_mode = False after core init (ArangoDB + seeding) so the
    frontend redirects promptly. OpenSearch initialization continues in the
    background without blocking the platform routes.
    """
    global _setup_mode
    logger.info("Phase 4 initialization starting (post-setup).")
    try:
        # Ensure search indices exist with correct mappings BEFORE seeding.
        # During seeding, artifact creation falls back to synchronous indexing
        # (index worker isn't running yet). Without pre-created indices,
        # OpenSearch auto-creates them with dynamic mapping, mapping UUID
        # fields as "text" instead of "keyword" and breaking ACL filters.
        from search.init_search import ensure_search_indices_exist
        await asyncio.to_thread(ensure_search_indices_exist)

        loop = asyncio.get_running_loop()
        await asyncio.to_thread(_run_phase4_core_sync, loop)
        _setup_mode = False
        logger.info("Phase 4 core complete — platform routes unblocked.")
        # Search init runs in the background; search will be unavailable briefly
        asyncio.create_task(_run_phase4_search_async(is_post_setup=True))
    except Exception:
        logger.exception("Phase 4 initialization failed after setup.")


async def _run_phase4_search_async(*, is_post_setup: bool = False) -> None:
    """Run search initialization in background without blocking platform routes."""
    try:
        await asyncio.to_thread(_run_phase4_search_sync, is_post_setup=is_post_setup)
        logger.info("Phase 4 search initialization complete.")
    except Exception:
        logger.exception("Phase 4 search initialization failed (non-fatal).")


def _ensure_operator_bootstrapped(arango_db) -> None:
    """Bootstrap the operator's Arango resources after setup.

    The setup wizard creates Person + Workspace in Postgres but can't touch
    Arango (it isn't connected during setup mode).  This function runs once
    per Phase 4 startup to fill the gap:

    1. Creates a personal collection if one doesn't exist yet.
    2. Grants admin (write) on every platform collection (idempotent upsert).

    Safe to call on every startup — all operations are idempotent.
    """
    from services.platform_settings_service import settings as platform_settings
    from core.config import AGIENCE_PLATFORM_USER_ID

    operator_id = platform_settings.get("platform.operator_id")
    if not operator_id:
        return  # No operator set (shouldn't happen after setup, but be safe)

    from db.arango import (
        get_collection_by_id,
        upsert_user_collection_grant,
    )
    from services.collection_service import create_new_collection
    from services.platform_topology import get_all_platform_collection_ids

    # 1. Personal collection (id == owner_id by convention)
    if not get_collection_by_id(arango_db, operator_id):
        try:
            create_new_collection(
                db=arango_db,
                owner_id=operator_id,
                name="Personal",
                description="Default personal collection",
                is_personal=True,
            )
            logger.info("Created personal collection for operator %s", operator_id)
        except Exception:
            logger.warning("Could not create personal collection for operator %s", operator_id, exc_info=True)

    # 2. Admin grants on all platform collections (idempotent)
    granted = 0
    for col_id in get_all_platform_collection_ids():
        try:
            _grant, changed = upsert_user_collection_grant(
                arango_db,
                user_id=operator_id,
                collection_id=col_id,
                granted_by=AGIENCE_PLATFORM_USER_ID,
                can_read=True,
                can_update=True,
                name="Platform operator (setup bootstrap)",
            )
            if changed:
                granted += 1
        except Exception:
            logger.warning("Failed to grant operator %s on collection %s", operator_id, col_id, exc_info=True)

    if granted:
        logger.info("Operator %s: created/updated admin grants on %d platform collections", operator_id, granted)

    # 3. Seed content materialization
    try:
        from services.seed_content_service import apply_platform_collections_to_user
        apply_platform_collections_to_user(arango_db, user_id=operator_id)
        logger.info("Seed content materialized for operator %s", operator_id)
    except Exception:
        logger.debug("Seed content materialization skipped for operator %s (non-fatal)", operator_id, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _setup_mode

    # ------------------------------------------------------------------
    # Phase 0: Initialize all key material (filesystem)
    # ------------------------------------------------------------------
    try:
        from core.key_manager import (
            init_jwt_keys, init_licensing_keys,
            init_encryption_key,
            init_platform_secret, init_setup_token,
            get_setup_token,
            init_arango_password, init_opensearch_password, init_minio_password,
            init_nonce_secret,
        )
        init_jwt_keys(key_id=config.JWT_KEY_ID)
        init_licensing_keys()
        init_encryption_key()
        init_platform_secret()
        init_nonce_secret()
        init_setup_token()
        init_arango_password()
        init_opensearch_password()
        init_minio_password()
    except Exception:
        logger.exception("Key initialization failed at startup (fatal)")
        raise

    # ------------------------------------------------------------------
    # Phase 1.5: Load bootstrap settings from key files
    # ------------------------------------------------------------------
    config.load_bootstrap_settings()
    from services.content_service import reinit_edge_clients
    reinit_edge_clients()

    # ------------------------------------------------------------------
    # Phase 2: Connect to databases, load platform settings
    # ------------------------------------------------------------------
    # Initialize ArangoDB (creates collections/indexes) — needed early
    # because platform settings now live in ArangoDB.
    arango_db = init_arango_db()

    # Load platform settings from ArangoDB into cache
    from services.platform_settings_service import settings as platform_settings
    platform_settings.load_all(arango_db)

    # Rebind config module variables from DB settings
    config.load_settings_from_db()

    # Re-initialize edge S3 clients now that config.CONTENT_URI is set from DB,
    # then eagerly ensure the content bucket exists.
    from services.content_service import reinit_edge_clients, ensure_content_bucket
    reinit_edge_clients()
    try:
        ensure_content_bucket()
    except Exception:
        logger.warning("Content bucket check at startup failed (non-fatal)", exc_info=True)

    # Re-register OAuth providers now that config is loaded from DB
    reload_oauth_providers()

    # Reconfigure logging with DB-loaded log level
    log_level = (config.BACKEND_LOG_LEVEL or "info").upper()
    logging.getLogger().setLevel(debug_level_map.get(log_level, logging.INFO))

    # ------------------------------------------------------------------
    # Phase 3: Check if setup is needed
    # ------------------------------------------------------------------
    if platform_settings.needs_setup():
        _setup_mode = True
        token = get_setup_token()
        logger.info("=" * 60)
        logger.info("SETUP REQUIRED")
        if token:
            from core import config as _cfg
            setup_url = f"{_cfg.FRONTEND_URI}/setup?token={token}"
            logger.info("Open this admin setup URL to complete setup:")
            logger.info("  %s", setup_url)
            logger.info("(contains one-time setup token; treat as secret; fingerprint: %s)", _redact_setup_token(token))
        else:
            logger.info("Open the platform in your browser to complete setup.")
        logger.info("=" * 60)
        # In setup mode: only /setup/*, /, /version, /.well-known/* are served.
        # All other routes return 503 via the setup_mode_middleware.
        # We still need to yield to keep the app running.
        from mcp_server.server import mcp as mcp_server_instance
        async with mcp_server_instance.session_manager.run():
            yield
        # Cleanup: if setup completed in-process, run_phase4_after_setup may have
        # started the index worker; stop it gracefully on shutdown.
        try:
            stop_index_worker(drain=True, timeout=5.0)
        except Exception as e:
            logger.error("Failed to stop indexing worker: %s", e)
        shutdown_search()
        return

    # ------------------------------------------------------------------
    # Phase 4: Full startup (setup complete)
    # ------------------------------------------------------------------
    _setup_mode = False

    # init_setup_token() in Phase 0 recreates the file if it was deleted by
    # delete_setup_token() during setup completion.  Clean it up now that we
    # know setup is done so no orphan token lingers on disk between restarts.
    from core.key_manager import delete_setup_token as _delete_setup_token
    _delete_setup_token()

    # arango_db already initialized in Phase 2

    # Pre-resolve platform singleton IDs (in-memory cache, needed on every startup)
    try:
        from services.platform_topology import pre_resolve_platform_ids
        pre_resolve_platform_ids(arango_db)
        from services import server_registry
        server_registry.populate_ids()
    except Exception:
        logger.exception("Platform ID pre-resolution failed at startup (fatal)")
        raise

    # Seeding (collections, authority, host, servers, etc.) only runs during
    # platform setup via run_phase4_after_setup(). Never on subsequent restarts.

    # Event bus — must be set before requests are served
    from core import event_bus
    event_bus.set_event_loop(asyncio.get_event_loop())

    # Register built-in operation dispatch handlers (mcp_tool, native, artifact_crud).
    try:
        from services import handler_registry
        handler_registry.register_builtin_handlers()
    except Exception:
        logger.exception("Operation handler registry bootstrap failed at startup (non-fatal)")

    # Search init in background — don't block startup while OpenSearch warms up
    search_init_task = asyncio.create_task(_run_phase4_search_async())

    # MCP server session manager
    from mcp_server.server import mcp as mcp_server_instance
    async with mcp_server_instance.session_manager.run():
        yield

    # Cleanup on shutdown
    search_init_task.cancel()
    try:
        await search_init_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        stop_index_worker(drain=True, timeout=5.0)
    except Exception as e:
        logger.error(f"Failed to stop indexing worker: {e}")
    shutdown_search()

# ----------------------------
# Create app
# ----------------------------
app = FastAPI(
    title="Agience API",
    version=BUILD_INFO.get("version") or "unknown",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
    swagger_ui_init_oauth={
        "clientId": "agience-docs-client",
        "usePkceWithAuthorizationCodeGrant": True,
    },
)

try:
    app.router.redirect_slashes = False
except Exception:
    pass

# ----------------------------
# Setup mode middleware
# ----------------------------
_SETUP_ALLOWED_PREFIXES = ("/setup", "/version", "/.well-known", "/docs", "/openapi.json", "/auth/token", "/server-credentials")

@app.middleware("http")
async def setup_mode_middleware(request: Request, call_next):
    """When in setup mode, only allow setup-related routes."""
    if _setup_mode:
        path = request.url.path
        if path == "/" or any(path.startswith(prefix) for prefix in _SETUP_ALLOWED_PREFIXES):
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={"detail": "Setup required", "setup_url": "/setup"},
        )
    return await call_next(request)

# ----------------------------
# Error logging handlers
# ----------------------------

# Fields whose values must never appear in logs.
_REDACT_KEYS = frozenset({"password", "secret", "token", "api_key", "apikey",
                          "access_token", "refresh_token", "credential",
                          "passkey_credential", "passkey_challenge"})

def _redact_body(raw: bytes, max_len: int = 2048) -> str:
    """Return a log-safe representation of a request body.

    JSON bodies have sensitive fields replaced with '***'. Non-JSON
    bodies are truncated to *max_len* bytes.
    """
    if not raw:
        return ""
    try:
        import json as _json
        obj = _json.loads(raw)
        if isinstance(obj, dict):
            obj = {k: ("***" if k.lower() in _REDACT_KEYS else v)
                   for k, v in obj.items()}
        return _json.dumps(obj, ensure_ascii=False)[:max_len]
    except Exception:
        return repr(raw[:max_len])


@app.exception_handler(HTTPException)
async def http_exception_logger(request: Request, exc: HTTPException):
    try:
        body = await request.body()
    except Exception:
        body = b""
    logger.warning(
        "HTTP %s %s %s user=%s ws=%s artifact=%s detail=%r body=%s",
        exc.status_code,
        request.method,
        request.url.path,
        getattr(request.state, "user_id", None),
        request.path_params.get("workspace_id"),
        request.path_params.get("artifact_id"),
        exc.detail,
        _redact_body(body),
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(Exception)
async def unhandled_exception_logger(request: Request, exc: Exception):
    try:
        body = await request.body()
    except Exception:
        body = b""
    logger.exception(
        "HTTP 500 %s %s user=%s ws=%s artifact=%s body=%s error=%s",
        request.method,
        request.url.path,
        getattr(request.state, "user_id", None),
        request.path_params.get("workspace_id"),
        request.path_params.get("artifact_id"),
        _redact_body(body),
        repr(exc),
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

# ----------------------------
# CORS & Session
# ----------------------------
# allow_origins=["*"] is safe: all authenticated API calls use Bearer tokens,
# not cookies, so cross-origin requests without credentials pose no CSRF risk.
# The OAuth redirect flow uses session cookies but those are same-site
# (browser navigations, not XHR), so CORS doesn't apply to them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The SessionMiddleware secret is used to sign the OAuth PKCE state cookie.
# We read the platform_internal.secret key file directly here so the real
# secret is available at app construction time (key_manager.init_platform_secret()
# hasn't run yet).  If the file doesn't exist yet (very first boot before
# lifespan has ever run) a stable fallback is used; it will be replaced
# automatically on the first restart after lifespan writes the key file.
def _read_session_secret() -> str:
    from pathlib import Path
    _secret_path = Path(__file__).resolve().parent / "keys" / "platform_internal.secret"
    if _secret_path.exists():
        try:
            return _secret_path.read_text().strip()
        except OSError:
            pass
    return "bootstrap-session-key-replace-after-first-boot"

app.add_middleware(
    SessionMiddleware,
    secret_key=_read_session_secret(),
    max_age=12 * 60 * 60,
)

# ----------------------------
# Routers
# ----------------------------
app.include_router(setup_router)
app.include_router(auth_router)
app.include_router(auth_root_router)
app.include_router(api_keys_router)
app.include_router(server_credentials_router)
app.include_router(secrets_router)
app.include_router(mcp_router)
app.include_router(relay_router)
app.include_router(events_router)
app.include_router(types_router)
app.include_router(platform_router)
app.include_router(passkey_router)
app.include_router(otp_router)
app.include_router(artifacts_router)
app.include_router(agents_router)
app.include_router(grants_router)
app.include_router(gate_router)

# ----------------------------
# MCP Server (Streamable HTTP)
# ----------------------------
app.mount("/mcp", create_mcp_app())


@app.get("/mcp", include_in_schema=False)
@app.post("/mcp", include_in_schema=False)
async def mcp_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/mcp/", status_code=307)


# ----------------------------
# MCP discovery (.well-known)
# ----------------------------

@app.get("/.well-known/mcp.json", include_in_schema=False)
def mcp_well_known():
    """Advertise MCP endpoints for clients that support well-known discovery."""
    base = {
        "name": "Agience MCP",
        "version": BUILD_INFO.get("version") or "unknown",
        "endpoints": {
            "streamable_http": "/mcp",
        },
        "auth": {
            "schemes": ["bearer"],
            "bearer_formats": ["jwt", "api_key"],
        },
    }
    return JSONResponse(content=base, status_code=200, headers={"Cache-Control": "no-store"})

# ----------------------------
# Basic routes
# ----------------------------
def utcnow_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

@app.get("/", include_in_schema=False)
def read_root():
    payload = {
        "status": "ok" if not _setup_mode else "setup_required",
        "version": BUILD_INFO.get("version") or "unknown",
        "server_time": utcnow_z(),
        "links": {
            "self": "/",
            "service-doc": "/docs",
            "service-desc": "/openapi.json",
        },
    }
    return JSONResponse(
        content=payload,
        status_code=200,
        headers={"Cache-Control": "no-store"}
    )

@app.get("/status", include_in_schema=False)
def check_backend_status():
    status = {
        **check_arango_health(),
        **check_search_health(),
    }
    return status

@app.get("/version", include_in_schema=False)
def version():
    return {
        "version": BUILD_INFO.get("version") or "",
        "build_time": BUILD_INFO.get("build_time") or ""
    }

# ----------------------------
# Entrypoint (dev)
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8081,
        reload=True,
        log_level="info",
        log_config="core/uvicorn_log_config.json",
        workers=1,
        server_header=False,
        timeout_graceful_shutdown=3,
    )
