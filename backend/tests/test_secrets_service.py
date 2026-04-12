"""Unit tests for services.secrets_service.

Covers Fernet round-trip, the SecretConfig value object, and the
person.preferences.secrets[] CRUD chain (list / add / delete / set_default /
get_secret_value). All DB writes are intercepted at arango_identity.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from services import secrets_service
import services.secrets_service as ss


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cipher():
    """Each test gets a fresh Fernet cipher; the module caches one in _cipher."""
    ss._cipher = None
    key = Fernet.generate_key().decode()
    with patch("services.secrets_service.get_encryption_key", return_value=key):
        yield
    ss._cipher = None


def _person_with_secrets(secrets_list):
    return {"id": "user-1", "preferences": {"secrets": list(secrets_list)}}


# ---------------------------------------------------------------------------
# Encryption round-trip
# ---------------------------------------------------------------------------

class TestEncryptDecrypt:
    def test_round_trip(self):
        ct = secrets_service.encrypt_value("hunter2")
        assert ct != "hunter2"
        assert secrets_service.decrypt_value(ct) == "hunter2"

    def test_decrypt_garbage_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="Failed to decrypt"):
            secrets_service.decrypt_value("not-a-fernet-token")

    def test_unconfigured_key_raises_runtime_error(self):
        ss._cipher = None
        with patch("services.secrets_service.get_encryption_key", return_value=None):
            with pytest.raises(RuntimeError, match="PLATFORM_ENCRYPTION_KEY"):
                secrets_service.encrypt_value("plain")


# ---------------------------------------------------------------------------
# SecretConfig
# ---------------------------------------------------------------------------

class TestSecretConfig:
    def test_round_trip_dict(self):
        s = secrets_service.SecretConfig(
            id="s-1",
            type="llm_key",
            provider="openai",
            label="prod",
            encrypted_value="enc",
            created_time="2026-04-07T00:00:00+00:00",
            is_default=True,
            authorizer_id="authz-1",
            expires_at="",
        )
        d = s.to_dict()
        round = secrets_service.SecretConfig.from_dict(d)
        assert round.id == "s-1"
        assert round.type == "llm_key"
        assert round.is_default is True


    def test_to_dict_omits_expires_at_when_blank(self):
        s = secrets_service.SecretConfig(
            id="s-1",
            type="t",
            provider="p",
            label="l",
            encrypted_value="e",
            created_time="t0",
        )
        assert "expires_at" not in s.to_dict()


# ---------------------------------------------------------------------------
# list / add / delete / default
# ---------------------------------------------------------------------------

class TestSecretsCrud:
    def test_list_filters_by_type_and_provider(self):
        prefs = _person_with_secrets([
            {"id": "s-1", "type": "llm_key", "provider": "openai", "encrypted_value": "a", "created_time": ""},
            {"id": "s-2", "type": "llm_key", "provider": "anthropic", "encrypted_value": "b", "created_time": ""},
            {"id": "s-3", "type": "github_token", "provider": "github", "encrypted_value": "c", "created_time": ""},
        ])
        with patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs):
            llm = secrets_service.list_secrets(object(), "user-1", secret_type="llm_key")
            openai = secrets_service.list_secrets(
                object(), "user-1", secret_type="llm_key", provider="openai"
            )
            by_id = secrets_service.list_secrets(object(), "user-1", secret_id="s-3")
        assert {s.id for s in llm} == {"s-1", "s-2"}
        assert [s.id for s in openai] == ["s-1"]
        assert [s.id for s in by_id] == ["s-3"]

    def test_list_filter_by_authorizer_id(self):
        prefs = _person_with_secrets([
            {"id": "s-1", "type": "bearer_token", "authorizer_id": "authz-A"},
            {"id": "s-2", "type": "bearer_token", "authorizer_id": "authz-B"},
        ])
        with patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs):
            out = secrets_service.list_secrets(
                object(), "user-1", authorizer_id="authz-A"
            )
        assert [s.id for s in out] == ["s-1"]

    def test_list_returns_empty_when_no_person(self):
        with patch("services.secrets_service.arango_ws.get_person_by_id", return_value=None):
            assert secrets_service.list_secrets(object(), "user-1") == []

    def test_add_secret_encrypts_and_persists(self):
        prefs = _person_with_secrets([])
        captured = {}

        def save(db, uid, p):
            captured["prefs"] = p

        with (
            patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs),
            patch(
                "services.secrets_service.arango_ws.update_person_preferences",
                side_effect=save,
            ),
        ):
            out = secrets_service.add_secret(
                object(), "user-1", "llm_key", "openai", "prod", "sk-secret-value"
            )
        assert len(out) == 1
        stored = captured["prefs"]["secrets"][0]
        assert stored["type"] == "llm_key"
        # Encrypted, not raw.
        assert stored["encrypted_value"] != "sk-secret-value"
        assert secrets_service.decrypt_value(stored["encrypted_value"]) == "sk-secret-value"

    def test_add_default_clears_other_defaults_for_same_type_provider(self):
        prefs = _person_with_secrets([
            {
                "id": "s-1",
                "type": "llm_key",
                "provider": "openai",
                "is_default": True,
                "encrypted_value": "old",
                "created_time": "",
            }
        ])
        captured = {}

        def save(db, uid, p):
            captured["prefs"] = p

        with (
            patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs),
            patch(
                "services.secrets_service.arango_ws.update_person_preferences",
                side_effect=save,
            ),
        ):
            secrets_service.add_secret(
                object(),
                "user-1",
                "llm_key",
                "openai",
                "new",
                "new-key",
                is_default=True,
            )
        defaults = [s for s in captured["prefs"]["secrets"] if s.get("is_default")]
        assert len(defaults) == 1
        assert defaults[0]["label"] == "new"

    def test_delete_secret_removes_only_target(self):
        prefs = _person_with_secrets([
            {"id": "s-1", "type": "t", "encrypted_value": "a", "created_time": ""},
            {"id": "s-2", "type": "t", "encrypted_value": "b", "created_time": ""},
        ])
        captured = {}

        def save(db, uid, p):
            captured["prefs"] = p

        with (
            patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs),
            patch(
                "services.secrets_service.arango_ws.update_person_preferences",
                side_effect=save,
            ),
        ):
            out = secrets_service.delete_secret(object(), "user-1", "s-1")
        assert {s.id for s in out} == {"s-2"}
        assert {s["id"] for s in captured["prefs"]["secrets"]} == {"s-2"}

    def test_set_default_promotes_target_demotes_others(self):
        prefs = _person_with_secrets([
            {"id": "s-1", "type": "t", "provider": "p", "is_default": True, "encrypted_value": "", "created_time": ""},
            {"id": "s-2", "type": "t", "provider": "p", "is_default": False, "encrypted_value": "", "created_time": ""},
        ])
        captured = {}

        def save(db, uid, p):
            captured["prefs"] = p

        with (
            patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs),
            patch(
                "services.secrets_service.arango_ws.update_person_preferences",
                side_effect=save,
            ),
        ):
            secrets_service.set_default_secret(object(), "user-1", "s-2")
        out = {s["id"]: s["is_default"] for s in captured["prefs"]["secrets"]}
        assert out == {"s-1": False, "s-2": True}

    def test_set_default_unknown_id_is_noop(self):
        prefs = _person_with_secrets([
            {"id": "s-1", "type": "t", "provider": "p", "is_default": True, "encrypted_value": "", "created_time": ""},
        ])
        with (
            patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs),
            patch("services.secrets_service.arango_ws.update_person_preferences"),
        ):
            out = secrets_service.set_default_secret(object(), "user-1", "missing")
        assert [s.id for s in out] == ["s-1"]


# ---------------------------------------------------------------------------
# get_secret_value resolution
# ---------------------------------------------------------------------------

class TestGetSecretValue:
    def _prefs_with(self, *items):
        return _person_with_secrets(list(items))

    def test_returns_none_when_no_match(self):
        prefs = self._prefs_with()
        with patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs):
            assert (
                secrets_service.get_secret_value(
                    object(), "user-1", "llm_key", provider="openai"
                )
                is None
            )

    def test_exact_id_lookup_wins(self):
        ct = secrets_service.encrypt_value("by-id")
        prefs = self._prefs_with(
            {"id": "s-1", "type": "llm_key", "provider": "openai", "is_default": True, "encrypted_value": secrets_service.encrypt_value("default"), "created_time": ""},
            {"id": "s-2", "type": "llm_key", "provider": "openai", "is_default": False, "encrypted_value": ct, "created_time": ""},
        )
        with patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs):
            v = secrets_service.get_secret_value(
                object(), "user-1", "llm_key", provider="openai", secret_id="s-2"
            )
        assert v == "by-id"

    def test_default_wins_when_no_id_provided(self):
        prefs = self._prefs_with(
            {"id": "s-1", "type": "llm_key", "provider": "openai", "is_default": False, "encrypted_value": secrets_service.encrypt_value("first"), "created_time": ""},
            {"id": "s-2", "type": "llm_key", "provider": "openai", "is_default": True, "encrypted_value": secrets_service.encrypt_value("default"), "created_time": ""},
        )
        with patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs):
            assert (
                secrets_service.get_secret_value(
                    object(), "user-1", "llm_key", provider="openai"
                )
                == "default"
            )

    def test_first_wins_when_no_default(self):
        prefs = self._prefs_with(
            {"id": "s-1", "type": "llm_key", "provider": "openai", "is_default": False, "encrypted_value": secrets_service.encrypt_value("first"), "created_time": ""},
            {"id": "s-2", "type": "llm_key", "provider": "openai", "is_default": False, "encrypted_value": secrets_service.encrypt_value("second"), "created_time": ""},
        )
        with patch("services.secrets_service.arango_ws.get_person_by_id", return_value=prefs):
            assert (
                secrets_service.get_secret_value(
                    object(), "user-1", "llm_key", provider="openai"
                )
                == "first"
            )
