"""
services/otp_service.py

Email OTP (one-time password) generation, sending, and verification.

Codes are 6-digit numeric, hashed with bcrypt, and expire after 10 minutes.
Rate-limited: max 3 verification attempts per code, 5-minute lockout after
5 failed attempts for the same email.
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from arango.database import StandardDatabase

from db import arango_identity as arango_ws

logger = logging.getLogger(__name__)

_OTP_EXPIRY_MINUTES = 10
_MAX_ATTEMPTS_PER_CODE = 3
_LOCKOUT_WINDOW_MINUTES = 5
_MAX_FAILED_IN_WINDOW = 5


def _generate_code() -> str:
    """Generate a random 6-digit code."""
    return f"{secrets.randbelow(1000000):06d}"


def _hash_code(code: str) -> str:
    """Hash a code with bcrypt."""
    return bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode()


def _verify_code_hash(code: str, code_hash: str) -> bool:
    """Verify a code against its bcrypt hash."""
    try:
        return bcrypt.checkpw(code.encode(), code_hash.encode())
    except Exception:
        return False


async def request_otp(db: StandardDatabase, email: str) -> bool:
    """
    Generate an OTP code and send it via email.

    Returns True if the code was sent, False if rate-limited or email not configured.
    """
    from services import email_service

    # Rate limiting: check recent failed attempts
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_LOCKOUT_WINDOW_MINUTES)).isoformat()
    recent_failed = arango_ws.get_recent_failed_otp_count(db, email, cutoff, _MAX_ATTEMPTS_PER_CODE)

    if recent_failed >= _MAX_FAILED_IN_WINDOW:
        logger.warning("OTP rate limit exceeded for %s", email)
        return False

    # Generate and store code
    code = _generate_code()
    otp_id = str(uuid.uuid4())
    arango_ws.create_otp_code(db, {
        "id": otp_id,
        "email": email,
        "code_hash": _hash_code(code),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=_OTP_EXPIRY_MINUTES)).isoformat(),
        "attempts": 0,
        "used": False,
        "created_time": datetime.now(timezone.utc).isoformat(),
    })

    # Send via email
    sent = await email_service.send_otp(email, code)
    if not sent:
        logger.warning("Failed to send OTP email to %s", email)
        return False

    logger.info("OTP sent to %s", email)
    return True


def verify_otp(db: StandardDatabase, email: str, code: str) -> Optional[str]:
    """Verify an OTP code for an email address.

    Returns the person_id on success, None on failure.
    Consumes the code on success (marks as used).

    **Auto-creates the person** if the email is unknown, mirroring the OIDC
    login flow (``get_or_create_user_by_oidc_identity``). Access-control gate
    (``is_person_allowed``) is enforced inside ``get_or_create_user_by_email``.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # Find all unexpired, unused codes for this email
    candidates = arango_ws.get_valid_otp_codes(db, email, now_iso, _MAX_ATTEMPTS_PER_CODE)

    for otp in candidates:
        otp_id = otp["id"]
        arango_ws.increment_otp_attempts(db, otp_id)

        if _verify_code_hash(code, otp["code_hash"]):
            arango_ws.mark_otp_used(db, otp_id)

            # Look up or create the person. Aligns with OIDC auto-create so
            # invite links work for first-time users who sign in via OTP.
            from services.person_service import get_or_create_user_by_email
            try:
                person = get_or_create_user_by_email(db, email)
            except (ValueError, PermissionError) as exc:
                logger.warning(
                    "OTP verified but person creation denied for %s: %s", email, exc
                )
                return None
            logger.info("OTP verified for %s", email)
            return person.id

    logger.warning("OTP verification failed for %s", email)
    return None


def cleanup_expired(db: StandardDatabase) -> int:
    """Delete expired OTP codes. Returns count deleted."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return arango_ws.delete_expired_otp_codes(db, now_iso)
