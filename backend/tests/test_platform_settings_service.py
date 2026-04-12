"""Unit tests for services.platform_settings_service.

Covers the in-memory cache + DEFAULTS fallback chain, type coercion helpers,
secret encryption, needs_setup gate, batch writes, and per-category grouping.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from services.platform_settings_service import (
    DEFAULTS,
    PlatformSettingsService,
)


@pytest.fixture
def svc():
    """A clean service with the test encryption key wired in."""
    s = PlatformSettingsService()
    key = Fernet.generate_key().decode()
    with patch(
        "services.platform_settings_service.get_encryption_key", return_value=key
    ):
        yield s


# ---------------------------------------------------------------------------
# load_all + cache priority
# ---------------------------------------------------------------------------

class TestLoadAll:
    def test_load_all_populates_cache_and_secret_flags(self, svc):
        rows = [
            {"id": "ai.openai.api_key", "value": "ciphertext", "is_secret": True},
            {"id": "branding.title", "value": "MyApp", "is_secret": False},
        ]
        with patch(
            "db.arango_identity.get_all_platform_settings", return_value=rows
        ):
            svc.load_all(MagicMock())

        assert svc.is_loaded is True
        assert svc._cache["branding.title"] == "MyApp"
        assert svc._secret_flags["ai.openai.api_key"] is True

    def test_load_all_skips_rows_with_no_key(self, svc):
        rows = [
            {"value": "x", "is_secret": False},  # no id
            {"id": "platform.log_level", "value": "debug"},
        ]
        with patch(
            "db.arango_identity.get_all_platform_settings", return_value=rows
        ):
            svc.load_all(MagicMock())
        assert "platform.log_level" in svc._cache
        assert len(svc._cache) == 1



# ---------------------------------------------------------------------------
# get / fallback chain
# ---------------------------------------------------------------------------

class TestGet:
    def test_cache_hit_returns_cached_value(self, svc):
        svc._cache["branding.title"] = "Override"
        svc._secret_flags["branding.title"] = False
        assert svc.get("branding.title") == "Override"

    def test_cache_miss_falls_back_to_DEFAULTS(self, svc):
        # branding.title is in DEFAULTS as "Agience"
        assert svc.get("branding.title") == DEFAULTS["branding.title"]

    def test_cache_miss_and_no_default_returns_user_default(self, svc):
        assert svc.get("nonexistent.key", default="fallback") == "fallback"

    def test_cache_miss_returns_none_when_no_default(self, svc):
        assert svc.get("nonexistent.key") is None

    def test_secret_value_is_decrypted_on_get(self, svc):
        cipher = Fernet(
            svc._encrypt.__self__._encrypt.__globals__["get_encryption_key"]().encode()
        )
        del cipher  # not actually using it; encrypt-then-get round-trips below

        encrypted = svc._encrypt("hunter2")
        svc._cache["api.token"] = encrypted
        svc._secret_flags["api.token"] = True
        assert svc.get("api.token") == "hunter2"


# ---------------------------------------------------------------------------
# Type coercion helpers
# ---------------------------------------------------------------------------

class TestCoercion:
    def test_get_bool_truthy_values(self, svc):
        for v in ("true", "1", "yes", "TRUE"):
            svc._cache["k"] = v
            assert svc.get_bool("k") is True

    def test_get_bool_falsy_values(self, svc):
        for v in ("false", "0", "no", ""):
            svc._cache["k"] = v
            assert svc.get_bool("k") is False

    def test_get_bool_default_when_missing(self, svc):
        assert svc.get_bool("missing", default=True) is True

    def test_get_int_parses_valid(self, svc):
        svc._cache["k"] = "42"
        assert svc.get_int("k") == 42

    def test_get_int_default_on_garbage(self, svc):
        svc._cache["k"] = "not-a-number"
        assert svc.get_int("k", default=99) == 99

    def test_get_float_parses_valid(self, svc):
        svc._cache["k"] = "3.14"
        assert svc.get_float("k") == 3.14

    def test_get_float_default_on_garbage(self, svc):
        svc._cache["k"] = "x"
        assert svc.get_float("k", default=1.0) == 1.0

    def test_get_csv_list_splits_and_strips(self, svc):
        svc._cache["k"] = "a, b ,c,,d"
        assert svc.get_csv_list("k") == ["a", "b", "c", "d"]

    def test_get_csv_list_default_when_empty(self, svc):
        assert svc.get_csv_list("missing", default=["x"]) == ["x"]


# ---------------------------------------------------------------------------
# needs_setup
# ---------------------------------------------------------------------------

class TestNeedsSetup:
    def test_true_when_setup_complete_not_set(self, svc):
        # Default in DEFAULTS is "false"
        assert svc.needs_setup() is True

    def test_false_when_setup_complete_true(self, svc):
        svc._cache["platform.setup_complete"] = "true"
        svc._secret_flags["platform.setup_complete"] = False
        assert svc.needs_setup() is False


# ---------------------------------------------------------------------------
# set_setting / set_many / delete_keys
# ---------------------------------------------------------------------------

class TestWriters:
    def test_set_setting_encrypts_secret_before_persisting(self, svc):
        captured = {}

        def fake_set(db, **kwargs):
            captured.update(kwargs)

        with patch(
            "db.arango_identity.set_platform_setting", side_effect=fake_set
        ):
            svc.set_setting(
                MagicMock(),
                key="api.token",
                value="hunter2",
                category="api",
                is_secret=True,
            )

        # Persisted value is NOT plaintext.
        assert captured["value"] != "hunter2"
        # Cache is updated with the encrypted blob.
        assert svc._cache["api.token"] == captured["value"]
        # get() round-trips through decryption.
        assert svc.get("api.token") == "hunter2"

    def test_set_setting_plain_passthrough_for_non_secret(self, svc):
        with patch(
            "db.arango_identity.set_platform_setting"
        ):
            svc.set_setting(
                MagicMock(),
                key="branding.title",
                value="MyApp",
                category="branding",
                is_secret=False,
            )
        assert svc._cache["branding.title"] == "MyApp"

    def test_set_many_writes_each_and_reloads(self, svc):
        with (
            patch("db.arango_identity.set_platform_setting") as set_one,
            patch(
                "db.arango_identity.get_all_platform_settings", return_value=[]
            ),
        ):
            count = svc.set_many(
                MagicMock(),
                [
                    {
                        "key": "branding.title",
                        "value": "X",
                        "category": "branding",
                    },
                    {
                        "key": "ai.openai.api_key",
                        "value": "sk-1",
                        "category": "ai",
                        "is_secret": True,
                    },
                ],
            )
        assert count == 2
        assert set_one.call_count == 2
        # The reload was triggered (cache cleared by load_all on empty rows).
        assert svc.is_loaded is True

    def test_delete_keys_removes_from_db_and_reloads(self, svc):
        coll = MagicMock()
        coll.has.side_effect = lambda k: k in ("k1", "k2")

        db = MagicMock()
        db.collection.return_value = coll

        with patch(
            "db.arango_identity.get_all_platform_settings", return_value=[]
        ):
            count = svc.delete_keys(db, ["k1", "k2", "missing"])
        assert count == 2
        assert coll.delete.call_count == 2


# ---------------------------------------------------------------------------
# Encryption round-trip + invalid token tolerance
# ---------------------------------------------------------------------------

class TestEncryption:
    def test_round_trip(self, svc):
        ct = svc._encrypt("plaintext")
        assert ct != "plaintext"
        assert svc._decrypt(ct) == "plaintext"

    def test_decrypt_invalid_token_raises(self, svc):
        import pytest
        with pytest.raises(Exception):
            svc._decrypt("not-a-fernet-token")


# ---------------------------------------------------------------------------
# get_all_by_category
# ---------------------------------------------------------------------------

class TestGetAllByCategory:
    def test_db_values_take_precedence_over_defaults(self, svc):
        svc._cache["branding.title"] = "Custom"
        svc._secret_flags["branding.title"] = False
        grouped = svc.get_all_by_category()
        branding = next(g for g in grouped["branding"] if g["key"] == "branding.title")
        assert branding["value"] == "Custom"

    def test_secret_values_are_masked(self, svc):
        svc._cache["api.token"] = "ciphertext"
        svc._secret_flags["api.token"] = True
        grouped = svc.get_all_by_category(category="api")
        token = next(g for g in grouped["api"] if g["key"] == "api.token")
        assert token["value"] is None
        assert token["is_secret"] is True

    def test_defaults_filled_in_for_missing_keys(self, svc):
        grouped = svc.get_all_by_category(category="branding")
        keys = {g["key"] for g in grouped["branding"]}
        # branding.title comes from DEFAULTS even with empty cache.
        assert "branding.title" in keys

    def test_invalidate_cache_resets(self, svc):
        svc._cache["x"] = "y"
        svc._loaded = True
        svc.invalidate_cache()
        assert svc._cache == {}
        assert svc.is_loaded is False
