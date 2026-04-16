"""
services/platform_settings_service.py

DB-backed platform configuration with in-memory cache.

All runtime settings are stored in the platform_settings ArangoDB collection.
Settings marked is_secret=True are encrypted at rest using the Fernet
encryption key from key_manager.

Usage:
    from services.platform_settings_service import settings

    # After load_all() has been called at startup:
    value = settings.get("ai.openai_api_key")
    secret = settings.get_secret("auth.google.client_secret")
"""

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet
from arango.database import StandardDatabase

from core.key_manager import get_encryption_key
from db import arango_identity as arango_ws

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Default values — Docker-friendly infrastructure settings
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, str] = {
    # PostgreSQL (bootstrap — also resolved from key files)
    "db.postgres.host": "sql",
    "db.postgres.port": "5432",
    "db.postgres.user": "agience",
    "db.postgres.name": "agience",

    # AI defaults
    "ai.default_provider": "openai",
    "ai.default_model": "gpt-4o-mini",
    "ai.ollama_base_url": "http://localhost:11434",

    # ArangoDB — use env vars so dev mode resolves to 127.0.0.1; Docker sets ARANGO_HOST=graph
    "db.arango.host": os.getenv("ARANGO_HOST", "127.0.0.1"),
    "db.arango.port": os.getenv("ARANGO_PORT", "8529"),
    "db.arango.username": "root",
    "db.arango.database": "agience",

    # OpenSearch — same: env var or localhost
    "search.opensearch.host": os.getenv("OPENSEARCH_HOST", "127.0.0.1"),
    "search.opensearch.port": os.getenv("OPENSEARCH_PORT", "9200"),
    "search.opensearch.use_ssl": "true",   # OpenSearch always runs with SSL in Docker
    "search.opensearch.verify_certs": "false",  # Self-signed cert from demo installer
    "search.opensearch.request_timeout_s": "10",
    "search.opensearch.startup_deadline_s": "120",

    # Search tuning
    "search.refresh_interval": "750ms",
    "search.chunk_size": "1000",
    "search.chunk_overlap": "200",
    "search.field_weights_preset": "description-first",
    "search.bm25_size": "200",
    "search.knn_k": "400",
    "search.knn_num_candidates": "1000",

    # Content storage (S3/MinIO)
    "storage.content_uri": os.getenv("CONTENT_URI", "http://localhost:9000"),
    "storage.content_bucket": os.getenv("CONTENT_BUCKET", "agience-content"),
    "storage.content_download_url_expiry": "300",
    "storage.content_upload_url_expiry": "900",
    "storage.content_multipart_part_url_expiry": "300",

    # Branding
    "branding.title": "Agience",
    "branding.favicon": "favicon.png",
    "branding.frontend_uri": os.getenv("FRONTEND_URI", "http://localhost:5173"),
    "branding.backend_uri": os.getenv("BACKEND_URI", "http://localhost:8081"),

    # Auth
    "auth.password.enabled": "true",
    "auth.password.min_length": "12",
    "auth.password.pbkdf2_iters": "200000",
    "auth.invite_only": "false",

    # Platform
    "platform.log_level": "info",
    "platform.index_queue_max_workers": "16",
    "platform.allow_local_mcp_servers": "false",
    "platform.seed_collection_slugs": "agience-inbox-seeds",
    "platform.setup_complete": "false",

    # Email (not configured by default)
    "email.provider": "",
    "email.from_address": "",
    "email.from_name": "Agience",
}


class PlatformSettingsService:
    """DB-backed platform configuration with in-memory cache."""

    def __init__(self):
        self._cache: dict[str, str] = {}
        self._secret_flags: dict[str, bool] = {}
        self._loaded = False

    def load_all(self, db: StandardDatabase) -> None:
        """Load all settings from ArangoDB platform_settings collection into memory cache."""
        rows = arango_ws.get_all_platform_settings(db)
        cache = {}
        secret_flags = {}
        for row in rows:
            key = row.get("id")
            if not key:
                continue
            cache[key] = row.get("value", "")
            secret_flags[key] = row.get("is_secret", False)
        self._cache = cache
        self._secret_flags = secret_flags
        self._loaded = True
        logger.info("Platform settings loaded: %d entries", len(cache))

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a setting value. Returns from cache, then DEFAULTS, then default arg."""
        value = self._cache.get(key)
        if value is not None:
            # Auto-decrypt if it's a secret
            if self._secret_flags.get(key, False):
                return self._decrypt(value)
            return value
        return DEFAULTS.get(key, default)

    def get_secret(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a setting value, decrypting if stored as secret."""
        value = self._cache.get(key)
        if value is not None:
            if self._secret_flags.get(key, False):
                return self._decrypt(value)
            return value
        return DEFAULTS.get(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key)
        if value is None:
            return default
        return value.lower() in ("true", "1", "yes")

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def get_csv_list(self, key: str, default: Optional[list[str]] = None) -> list[str]:
        value = self.get(key)
        if not value:
            return default or []
        return [item.strip() for item in value.split(",") if item.strip()]

    def needs_setup(self) -> bool:
        """Check if the platform setup wizard has been completed."""
        return self.get("platform.setup_complete") != "true"

    def set_setting(
        self,
        db: StandardDatabase,
        key: str,
        value: str,
        category: str,
        is_secret: bool = False,
        updated_by: Optional[str] = None,
    ) -> None:
        """Write a single setting to ArangoDB and update the cache."""
        stored_value = self._encrypt(value) if is_secret else value

        arango_ws.set_platform_setting(
            db,
            key=key,
            value=stored_value,
            category=category,
            is_secret=is_secret,
            updated_by=updated_by,
        )

        # Update cache
        self._cache[key] = stored_value
        self._secret_flags[key] = is_secret

    def set_many(
        self,
        db: StandardDatabase,
        settings: list[dict],
        updated_by: Optional[str] = None,
    ) -> int:
        """
        Batch write settings. Each dict must have: key, value, category.
        Optional: is_secret (default False).
        Returns count of settings written.
        """
        count = 0
        for s in settings:
            key = s["key"]
            value = s["value"]
            category = s["category"]
            is_secret = s.get("is_secret", False)
            stored_value = self._encrypt(value) if is_secret else value

            arango_ws.set_platform_setting(
                db,
                key=key,
                value=stored_value,
                category=category,
                is_secret=is_secret,
                updated_by=updated_by,
            )
            count += 1

        # Reload cache
        self.load_all(db)
        return count

    def delete_keys(self, db: StandardDatabase, keys: list[str]) -> int:
        """Delete platform settings by key. Reloads the cache afterwards."""
        count = 0
        coll = db.collection("platform_settings")
        for key in keys:
            if coll.has(key):
                coll.delete(key)
                count += 1
        self.load_all(db)
        return count

    def get_all_by_category(self, category: Optional[str] = None) -> dict[str, list[dict]]:
        """
        Return all settings grouped by category.
        Secret values are returned as None (masked).
        DB values take precedence; DEFAULTS fill in any key not yet in the DB so
        the settings UI always shows the current effective value rather than blank fields.
        """
        grouped: dict[str, list[dict]] = {}
        seen_keys: set[str] = set()

        # DB values first (authoritative)
        for key, value in self._cache.items():
            is_secret = self._secret_flags.get(key, False)
            cat = key.split(".")[0] if "." in key else "platform"
            if category and cat != category:
                continue
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append({
                "key": key,
                "value": None if is_secret else value,
                "is_secret": is_secret,
            })
            seen_keys.add(key)

        # DEFAULTS for keys not yet written to DB (always non-secret)
        for key, default_value in DEFAULTS.items():
            if key in seen_keys:
                continue
            cat = key.split(".")[0] if "." in key else "platform"
            if category and cat != category:
                continue
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append({
                "key": key,
                "value": default_value,
                "is_secret": False,
            })

        return grouped

    def invalidate_cache(self) -> None:
        """Clear the in-memory cache. Next get() will use defaults only."""
        self._cache = {}
        self._secret_flags = {}
        self._loaded = False

    # -- Encryption helpers ------------------------------------------------

    def _encrypt(self, plaintext: str) -> str:
        cipher = Fernet(get_encryption_key().encode())
        return cipher.encrypt(plaintext.encode()).decode()

    def _decrypt(self, ciphertext: str) -> str:
        cipher = Fernet(get_encryption_key().encode())
        return cipher.decrypt(ciphertext.encode()).decode()


# Module-level singleton
settings = PlatformSettingsService()
