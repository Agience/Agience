"""
core/config.py

Platform configuration.

All runtime settings are stored in ArangoDB (platform_settings collection) and
loaded into module-level variables at boot. Operators may override select
values via .env. Environment values serve as defaults when the corresponding
DB setting is absent.

Consumer modules access them as ``config.SOME_VALUE`` via
``from core import config``.

Boot phases:
  Phase 1  (import time)  — static constants, safe defaults for all variables.
  Phase 1.5               — load_bootstrap_settings(): reads key files for
                            encryption key.
  Phase 2                 — load_settings_from_db(): rebinds all module
                            variables from the PlatformSettingsService cache.

IMPORTANT: Consumer modules must use ``from core import config`` and access
values as ``config.X`` in function bodies — NOT ``from core.config import X``
(which snapshots the value at import time and misses Phase 2 rebinding).
"""

import os
import uuid as _uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse as _urlparse

from dotenv import load_dotenv as _load_dotenv

# ---------------------------------------------------------------------------
#  Phase 0: Load .env into os.environ (before any os.getenv calls)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_load_dotenv(_REPO_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
#  Phase 1: Static constants (safe at import time, never change)
# ---------------------------------------------------------------------------

BASE_DIR = _REPO_ROOT
KEYS_DIR = Path(os.getenv("KEYS_DIR", str(BASE_DIR / ".data" / "keys")))

# Platform identity — deterministic UUID, never changes.
AGIENCE_PLATFORM_USER_ID = str(_uuid.uuid5(_uuid.NAMESPACE_URL, "agience://platform"))


# ---------------------------------------------------------------------------
#  Phase 1: DB-backed variables — initialized to safe defaults.
#  These are rebound in Phase 2 after settings are loaded from Postgres.
# ---------------------------------------------------------------------------

# AI
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")

# ArangoDB
ARANGO_HOST: str = os.getenv("ARANGO_HOST", "127.0.0.1")
ARANGO_PORT: int = 8529
ARANGO_USERNAME: str = "root"
ARANGO_PASSWORD: str = "root"
ARANGO_DATABASE: str = "agience"

# OAuth providers (all optional, configured via settings UI)
GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None
GOOGLE_OAUTH_CLIENT_SECRET: Optional[str] = None
GOOGLE_OAUTH_REDIRECT_URI: Optional[str] = None

MICROSOFT_ENTRA_TENANT: str = "common"
MICROSOFT_ENTRA_CLIENT_ID: Optional[str] = None
MICROSOFT_ENTRA_CLIENT_SECRET: Optional[str] = None
MICROSOFT_ENTRA_REDIRECT_URI: Optional[str] = None

AUTH0_DOMAIN: Optional[str] = None
AUTH0_CLIENT_ID: Optional[str] = None
AUTH0_CLIENT_SECRET: Optional[str] = None
AUTH0_REDIRECT_URI: Optional[str] = None

CUSTOM_OIDC_NAME: Optional[str] = None
CUSTOM_OIDC_METADATA_URL: Optional[str] = None
CUSTOM_OIDC_CLIENT_ID: Optional[str] = None
CUSTOM_OIDC_CLIENT_SECRET: Optional[str] = None
CUSTOM_OIDC_REDIRECT_URI: Optional[str] = None
CUSTOM_OIDC_SCOPES: str = "openid email profile"

# Password auth
PASSWORD_AUTH_ENABLED: bool = True
PASSWORD_MIN_LENGTH: int = 12
PASSWORD_PBKDF2_ITERS: int = 200000

# JWT
JWT_KEY_ID: str = "s1"

# URIs & identity
FRONTEND_URI: str = "http://localhost:5173"
BACKEND_URI: str = "http://localhost:8081"
PLATFORM_CLIENT_ID: str = "agience-client"
AUTHORITY_DOMAIN: str = "localhost"
AUTHORITY_ISSUER: str = "http://localhost:8081"

# Features
ALLOW_LOCAL_MCP_SERVERS: bool = False

# Billing enforcement (SaaS only — when False, all gate checks pass)
BILLING_ENFORCEMENT_ENABLED: bool = False

# Desktop relay
DESKTOP_RELAY_DOWNLOAD_BASE_URL: str = "https://github.com/ikailo/agience/releases/latest/download"

# Logging
BACKEND_LOG_LEVEL: str = "info"

# Event logger (optional)
EVENT_LOGGER_URI: Optional[str] = None
EVENT_LOGGER_USERNAME: Optional[str] = None
EVENT_LOGGER_PASSWORD: Optional[str] = None

# Email
PLATFORM_EMAIL_ADDRESS: str = ""

# Access control
ALLOWED_EMAILS: list[str] = []
ALLOWED_DOMAINS: list[str] = []
ALLOWED_GOOGLE_IDS: list[str] = []

# Content storage
CONTENT_URI: str = "http://localhost:9000"
CONTENT_BUCKET: str = "agience-content"
CONTENT_DOWNLOAD_URL_EXPIRY: int = 300
CONTENT_UPLOAD_URL_EXPIRY: int = 900
CONTENT_MULTIPART_PART_URL_EXPIRY: int = 300

# OpenSearch
OPENSEARCH_HOST: str = os.getenv("OPENSEARCH_HOST", "127.0.0.1")
OPENSEARCH_PORT: int = 9200
OPENSEARCH_USE_SSL: bool = True   # OpenSearch always runs with SSL in Docker (demo installer enables it)
OPENSEARCH_VERIFY_CERTS: bool = False  # Self-signed cert from demo installer
OPENSEARCH_USERNAME: str = "admin"
OPENSEARCH_PASSWORD: str = ""
OPENSEARCH_REQUEST_TIMEOUT_S: float = 10.0
OPENSEARCH_STARTUP_DEADLINE_S: float = 120.0

# Search
SEARCH_REFRESH_INTERVAL: str = "750ms"
SEARCH_CHUNK_SIZE: int = 1000
SEARCH_CHUNK_OVERLAP: int = 200
SEARCH_FIELD_WEIGHTS_PRESET: str = "description-first"

# Seed collections
SEED_COLLECTION_SLUGS: list[str] = ["agience-inbox-seeds"]

# Indexing
INDEX_QUEUE_MAX_WORKERS: int = 16

# Encryption (set in Phase 1.5 from key file)
PLATFORM_ENCRYPTION_KEY: str = ""

# Licensing (managed by key_manager, no longer env-configurable)
LICENSING_PUBLIC_KEYS_PATH: str = ""
LICENSING_PRIVATE_KEY_PATH: str = ""
LICENSING_SIGNING_KEY_ID: str = ""

# Platform internal secret (set in Phase 1.5 from key file)
PLATFORM_INTERNAL_SECRET: Optional[str] = None

# Inbound nonce HMAC secret — loaded from key file in Phase 1.5.
# Generated by the init container as inbound_nonce.secret.
INBOUND_NONCE_SECRET: str = ""


# ---------------------------------------------------------------------------
#  Phase 1.5: Bootstrap settings from key files
#  Called after key_manager init, before DB connections.
# ---------------------------------------------------------------------------

def load_bootstrap_settings() -> None:
    """
    Load encryption key and infrastructure credentials from key files.
    Must be called after key_manager.init_*() functions.
    """
    global PLATFORM_ENCRYPTION_KEY, PLATFORM_INTERNAL_SECRET
    global ARANGO_PASSWORD, OPENSEARCH_PASSWORD, INBOUND_NONCE_SECRET

    from core.key_manager import (
        get_encryption_key,
        get_platform_internal_secret,
        get_arango_password,
        get_opensearch_password,
        get_nonce_secret,
    )

    PLATFORM_ENCRYPTION_KEY = get_encryption_key()
    PLATFORM_INTERNAL_SECRET = get_platform_internal_secret()
    ARANGO_PASSWORD = get_arango_password()
    OPENSEARCH_PASSWORD = get_opensearch_password()
    INBOUND_NONCE_SECRET = get_nonce_secret()


# ---------------------------------------------------------------------------
#  Phase 2: Load all settings from DB
#  Called after PlatformSettingsService.load_all() has populated the cache.
# ---------------------------------------------------------------------------

# Mapping: setting key -> (module variable name, type converter)
_SETTING_MAP: dict[str, tuple[str, type]] = {
    # AI
    "ai.openai_api_key": ("OPENAI_API_KEY", str),

    # ArangoDB
    "db.arango.host": ("ARANGO_HOST", str),
    "db.arango.port": ("ARANGO_PORT", int),
    "db.arango.username": ("ARANGO_USERNAME", str),
    "db.arango.database": ("ARANGO_DATABASE", str),

    # Google OAuth
    "auth.google.client_id": ("GOOGLE_OAUTH_CLIENT_ID", str),
    "auth.google.client_secret": ("GOOGLE_OAUTH_CLIENT_SECRET", str),
    "auth.google.redirect_uri": ("GOOGLE_OAUTH_REDIRECT_URI", str),

    # Microsoft Entra
    "auth.microsoft.tenant": ("MICROSOFT_ENTRA_TENANT", str),
    "auth.microsoft.client_id": ("MICROSOFT_ENTRA_CLIENT_ID", str),
    "auth.microsoft.client_secret": ("MICROSOFT_ENTRA_CLIENT_SECRET", str),
    "auth.microsoft.redirect_uri": ("MICROSOFT_ENTRA_REDIRECT_URI", str),

    # Auth0
    "auth.auth0.domain": ("AUTH0_DOMAIN", str),
    "auth.auth0.client_id": ("AUTH0_CLIENT_ID", str),
    "auth.auth0.client_secret": ("AUTH0_CLIENT_SECRET", str),
    "auth.auth0.redirect_uri": ("AUTH0_REDIRECT_URI", str),

    # Custom OIDC
    "auth.oidc.name": ("CUSTOM_OIDC_NAME", str),
    "auth.oidc.metadata_url": ("CUSTOM_OIDC_METADATA_URL", str),
    "auth.oidc.client_id": ("CUSTOM_OIDC_CLIENT_ID", str),
    "auth.oidc.client_secret": ("CUSTOM_OIDC_CLIENT_SECRET", str),
    "auth.oidc.redirect_uri": ("CUSTOM_OIDC_REDIRECT_URI", str),
    "auth.oidc.scopes": ("CUSTOM_OIDC_SCOPES", str),

    # Password auth
    "auth.password.enabled": ("PASSWORD_AUTH_ENABLED", lambda v: v.lower() in ("true", "1", "yes")),
    "auth.password.min_length": ("PASSWORD_MIN_LENGTH", int),
    "auth.password.pbkdf2_iters": ("PASSWORD_PBKDF2_ITERS", int),

    # URIs & identity
    "branding.frontend_uri": ("FRONTEND_URI", str),
    "branding.backend_uri": ("BACKEND_URI", str),
    "platform.client_id": ("PLATFORM_CLIENT_ID", str),

    # Features
    "platform.allow_local_mcp_servers": ("ALLOW_LOCAL_MCP_SERVERS", lambda v: v.lower() in ("true", "1", "yes")),

    # Desktop relay
    "platform.desktop_relay_download_base_url": ("DESKTOP_RELAY_DOWNLOAD_BASE_URL", str),

    # Logging
    "platform.log_level": ("BACKEND_LOG_LEVEL", lambda v: v.lower()),

    # Event logger
    "platform.event_logger_uri": ("EVENT_LOGGER_URI", str),
    "platform.event_logger_username": ("EVENT_LOGGER_USERNAME", str),
    "platform.event_logger_password": ("EVENT_LOGGER_PASSWORD", str),

    # Email
    "email.from_address": ("PLATFORM_EMAIL_ADDRESS", str),

    # Access control (these are CSV lists, handled specially)
    "auth.allowed_emails": ("ALLOWED_EMAILS", None),
    "auth.allowed_domains": ("ALLOWED_DOMAINS", None),
    "auth.allowed_google_ids": ("ALLOWED_GOOGLE_IDS", None),

    # Content storage
    "storage.content_uri": ("CONTENT_URI", str),
    "storage.content_bucket": ("CONTENT_BUCKET", str),
    "storage.content_download_url_expiry": ("CONTENT_DOWNLOAD_URL_EXPIRY", int),
    "storage.content_upload_url_expiry": ("CONTENT_UPLOAD_URL_EXPIRY", int),
    "storage.content_multipart_part_url_expiry": ("CONTENT_MULTIPART_PART_URL_EXPIRY", int),

    # OpenSearch
    "search.opensearch.host": ("OPENSEARCH_HOST", str),
    "search.opensearch.port": ("OPENSEARCH_PORT", int),
    "search.opensearch.use_ssl": ("OPENSEARCH_USE_SSL", lambda v: v.lower() in ("true", "1", "yes")),
    "search.opensearch.verify_certs": ("OPENSEARCH_VERIFY_CERTS", lambda v: v.lower() in ("true", "1", "yes")),
    "search.opensearch.request_timeout_s": ("OPENSEARCH_REQUEST_TIMEOUT_S", float),
    "search.opensearch.startup_deadline_s": ("OPENSEARCH_STARTUP_DEADLINE_S", float),

    # Search tuning
    "search.refresh_interval": ("SEARCH_REFRESH_INTERVAL", str),
    "search.chunk_size": ("SEARCH_CHUNK_SIZE", int),
    "search.chunk_overlap": ("SEARCH_CHUNK_OVERLAP", int),
    "search.field_weights_preset": ("SEARCH_FIELD_WEIGHTS_PRESET", str),

    # Seed collections (CSV list, handled specially)
    "platform.seed_collection_slugs": ("SEED_COLLECTION_SLUGS", None),

    # Indexing
    "platform.index_queue_max_workers": ("INDEX_QUEUE_MAX_WORKERS", int),

    # Billing enforcement
    "platform.billing_enforcement_enabled": ("BILLING_ENFORCEMENT_ENABLED", lambda v: str(v).lower() in ("true", "1", "yes")),
}

# CSV list keys — need special handling
_CSV_LIST_KEYS = {"auth.allowed_emails", "auth.allowed_domains", "auth.allowed_google_ids", "platform.seed_collection_slugs"}


def _csv_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_settings_from_db() -> None:
    """
    Rebind all module-level variables from PlatformSettingsService cache.
    Must be called after PlatformSettingsService.load_all().
    """
    global OPENAI_API_KEY
    global ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, ARANGO_DATABASE
    global GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI
    global MICROSOFT_ENTRA_TENANT, MICROSOFT_ENTRA_CLIENT_ID, MICROSOFT_ENTRA_CLIENT_SECRET, MICROSOFT_ENTRA_REDIRECT_URI
    global AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET, AUTH0_REDIRECT_URI
    global CUSTOM_OIDC_NAME, CUSTOM_OIDC_METADATA_URL, CUSTOM_OIDC_CLIENT_ID, CUSTOM_OIDC_CLIENT_SECRET, CUSTOM_OIDC_REDIRECT_URI, CUSTOM_OIDC_SCOPES
    global PASSWORD_AUTH_ENABLED, PASSWORD_MIN_LENGTH, PASSWORD_PBKDF2_ITERS
    global FRONTEND_URI, BACKEND_URI, PLATFORM_CLIENT_ID
    global AUTHORITY_DOMAIN, AUTHORITY_ISSUER
    global ALLOW_LOCAL_MCP_SERVERS, DESKTOP_RELAY_DOWNLOAD_BASE_URL
    global BACKEND_LOG_LEVEL
    global EVENT_LOGGER_URI, EVENT_LOGGER_USERNAME, EVENT_LOGGER_PASSWORD
    global PLATFORM_EMAIL_ADDRESS
    global ALLOWED_EMAILS, ALLOWED_DOMAINS, ALLOWED_GOOGLE_IDS
    global CONTENT_URI, CONTENT_BUCKET
    global CONTENT_DOWNLOAD_URL_EXPIRY, CONTENT_UPLOAD_URL_EXPIRY, CONTENT_MULTIPART_PART_URL_EXPIRY
    global OPENSEARCH_HOST, OPENSEARCH_PORT, OPENSEARCH_USE_SSL, OPENSEARCH_VERIFY_CERTS
    global OPENSEARCH_REQUEST_TIMEOUT_S, OPENSEARCH_STARTUP_DEADLINE_S
    global SEARCH_REFRESH_INTERVAL, SEARCH_CHUNK_SIZE, SEARCH_CHUNK_OVERLAP, SEARCH_FIELD_WEIGHTS_PRESET
    global SEED_COLLECTION_SLUGS, INDEX_QUEUE_MAX_WORKERS
    global BILLING_ENFORCEMENT_ENABLED

    from services.platform_settings_service import settings

    for setting_key, (var_name, converter) in _SETTING_MAP.items():
        value = settings.get(setting_key)
        if value is None:
            continue

        if setting_key in _CSV_LIST_KEYS:
            globals()[var_name] = _csv_list(value)
        elif converter is not None:
            try:
                globals()[var_name] = converter(value)
            except (ValueError, TypeError):
                pass  # keep default
        else:
            globals()[var_name] = value

    # Derived values
    _backend_uri = BACKEND_URI
    try:
        _hostname = _urlparse(_backend_uri).hostname or "localhost"
    except Exception:
        _hostname = "localhost"
    AUTHORITY_DOMAIN = _hostname
    # Use BACKEND_URI as-is for the issuer — preserves scheme, host, and port exactly.
    # Constructing from hostname alone would strip non-standard ports (e.g. :8081) and
    # force HTTPS which breaks local and non-proxy self-hosted deployments.
    AUTHORITY_ISSUER = _backend_uri
