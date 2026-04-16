"""Unit tests for services.otp_service.

Covers OTP request/verify lifecycle:
  - Code generation format (6 digit numeric)
  - Bcrypt hash round-trip
  - Rate limiting: lockout after _MAX_FAILED_IN_WINDOW failed attempts
  - request_otp success path: code stored, email sent
  - request_otp send failure short-circuit
  - verify_otp success: marks used, returns person_id
  - verify_otp wrong code: increments attempts, returns None
  - verify_otp orphan code (no person): logged warning, returns None
  - cleanup_expired delegates to db layer
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import bcrypt
import pytest

from services import otp_service


class TestCodeHelpers:
    def test_generate_code_is_six_digits(self):
        for _ in range(50):
            code = otp_service._generate_code()
            assert len(code) == 6
            assert code.isdigit()

    def test_hash_round_trip(self):
        h = otp_service._hash_code("123456")
        assert otp_service._verify_code_hash("123456", h)
        assert not otp_service._verify_code_hash("999999", h)

    def test_verify_garbage_hash_returns_false(self):
        assert not otp_service._verify_code_hash("123456", "not-a-hash")


class TestRequestOtp:
    @pytest.mark.asyncio
    async def test_rate_limited_returns_false_without_sending(self):
        with (
            patch(
                "db.arango_identity.get_recent_failed_otp_count",
                return_value=otp_service._MAX_FAILED_IN_WINDOW,
            ),
            patch(
                "services.email_service.send_otp", new=AsyncMock(return_value=True)
            ) as send,
            patch("db.arango_identity.create_otp_code") as create,
        ):
            ok = await otp_service.request_otp(MagicMock(), "u@e.com")
        assert ok is False
        send.assert_not_called()
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_stores_hashed_code_and_sends(self):
        captured = {}

        def fake_create(db, doc):
            captured.update(doc)

        with (
            patch("db.arango_identity.get_recent_failed_otp_count", return_value=0),
            patch("db.arango_identity.create_otp_code", side_effect=fake_create),
            patch(
                "services.email_service.send_otp", new=AsyncMock(return_value=True)
            ) as send,
        ):
            ok = await otp_service.request_otp(MagicMock(), "u@e.com")

        assert ok is True
        assert captured["email"] == "u@e.com"
        # Code is hashed, not stored raw.
        assert "code_hash" in captured and "code" not in captured
        assert captured["used"] is False
        assert captured["attempts"] == 0
        # The plaintext code passed to send_otp matches the stored hash.
        sent_code = send.call_args[0][1]
        assert bcrypt.checkpw(sent_code.encode(), captured["code_hash"].encode())

    @pytest.mark.asyncio
    async def test_send_failure_returns_false(self):
        with (
            patch("db.arango_identity.get_recent_failed_otp_count", return_value=0),
            patch("db.arango_identity.create_otp_code"),
            patch(
                "services.email_service.send_otp", new=AsyncMock(return_value=False)
            ),
        ):
            ok = await otp_service.request_otp(MagicMock(), "u@e.com")
        assert ok is False


class TestVerifyOtp:
    def _otp_doc(self, code: str = "123456") -> dict:
        return {
            "id": "otp-1",
            "email": "u@e.com",
            "code_hash": otp_service._hash_code(code),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "attempts": 0,
            "used": False,
        }

    def test_happy_path_returns_person_id_and_marks_used(self):
        doc = self._otp_doc("123456")
        with (
            patch(
                "db.arango_identity.get_valid_otp_codes", return_value=[doc]
            ),
            patch("db.arango_identity.increment_otp_attempts") as inc,
            patch("db.arango_identity.mark_otp_used") as mark,
            patch(
                "db.arango_identity.get_person_by_email",
                return_value={"id": "person-7"},
            ),
        ):
            uid = otp_service.verify_otp(MagicMock(), "u@e.com", "123456")
        assert uid == "person-7"
        inc.assert_called_once()
        mark.assert_called_once()

    def test_wrong_code_increments_attempts_and_returns_none(self):
        doc = self._otp_doc("123456")
        with (
            patch("db.arango_identity.get_valid_otp_codes", return_value=[doc]),
            patch("db.arango_identity.increment_otp_attempts") as inc,
            patch("db.arango_identity.mark_otp_used") as mark,
            patch("db.arango_identity.get_person_by_email") as get_person,
        ):
            uid = otp_service.verify_otp(MagicMock(), "u@e.com", "999999")
        assert uid is None
        inc.assert_called_once()
        mark.assert_not_called()
        get_person.assert_not_called()

    def test_no_candidates_returns_none(self):
        with patch("db.arango_identity.get_valid_otp_codes", return_value=[]):
            assert otp_service.verify_otp(MagicMock(), "u@e.com", "123456") is None

    def test_verified_code_auto_creates_person(self):
        """Unknown emails auto-create a person (aligns with OIDC auto-create)."""
        from types import SimpleNamespace
        fake_person = SimpleNamespace(id="new-person-id")
        doc = self._otp_doc("123456")
        with (
            patch("db.arango_identity.get_valid_otp_codes", return_value=[doc]),
            patch("db.arango_identity.increment_otp_attempts"),
            patch("db.arango_identity.mark_otp_used"),
            patch(
                "services.person_service.get_or_create_user_by_email",
                return_value=fake_person,
            ) as mock_create,
        ):
            result = otp_service.verify_otp(MagicMock(), "u@e.com", "123456")
        assert result == "new-person-id"
        mock_create.assert_called_once()

    def test_verified_code_returns_none_if_creation_denied(self):
        """PermissionError from the access gate returns None (not 500)."""
        doc = self._otp_doc("123456")
        with (
            patch("db.arango_identity.get_valid_otp_codes", return_value=[doc]),
            patch("db.arango_identity.increment_otp_attempts"),
            patch("db.arango_identity.mark_otp_used"),
            patch(
                "services.person_service.get_or_create_user_by_email",
                side_effect=PermissionError("not allowed"),
            ),
        ):
            assert otp_service.verify_otp(MagicMock(), "u@e.com", "123456") is None

    def test_cleanup_delegates_to_db(self):
        with patch(
            "db.arango_identity.delete_expired_otp_codes", return_value=42
        ) as d:
            n = otp_service.cleanup_expired(MagicMock())
        assert n == 42
        d.assert_called_once()
