"""
services/email_service.py

Transactional email service supporting multiple providers.

Providers: SMTP (generic), AWS SES, SendGrid, Resend.
Configured via platform_settings (email.provider, email.smtp.*, etc.).
"""

import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import aiosmtplib
import httpx

logger = logging.getLogger(__name__)


def _get_provider_config() -> dict:
    """Read email provider configuration from platform settings."""
    from services.platform_settings_service import settings

    return {
        "provider": settings.get("email.provider", ""),
        "from_address": settings.get("email.from_address", ""),
        "from_name": settings.get("email.from_name", "Agience"),
        # SMTP
        "smtp_host": settings.get("email.smtp.host", ""),
        "smtp_port": settings.get_int("email.smtp.port", 587),
        "smtp_username": settings.get_secret("email.smtp.username", ""),
        "smtp_password": settings.get_secret("email.smtp.password", ""),
        "smtp_use_tls": settings.get_bool("email.smtp.use_tls", True),
        # SES
        "ses_region": settings.get("email.ses.region", "us-east-1"),
        "ses_access_key_id": settings.get_secret("email.ses.access_key_id", ""),
        "ses_secret_access_key": settings.get_secret("email.ses.secret_access_key", ""),
        # SendGrid
        "sendgrid_api_key": settings.get_secret("email.sendgrid.api_key", ""),
        # Resend
        "resend_api_key": settings.get_secret("email.resend.api_key", ""),
    }


def is_configured() -> bool:
    """Check if any email provider is configured."""
    from services.platform_settings_service import settings
    provider = settings.get("email.provider", "")
    return bool(provider)


async def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
) -> bool:
    """
    Send a transactional email via the configured provider.

    Returns True on success, False on failure (logged but not raised).
    """
    cfg = _get_provider_config()
    provider = cfg["provider"]

    if not provider:
        logger.warning("Email not configured — cannot send to %s", to_email)
        return False

    try:
        if provider == "smtp":
            return await _send_smtp(cfg, to_email, subject, html_body, text_body)
        elif provider == "ses":
            return await _send_ses(cfg, to_email, subject, html_body, text_body)
        elif provider == "sendgrid":
            return await _send_sendgrid(cfg, to_email, subject, html_body, text_body)
        elif provider == "resend":
            return await _send_resend(cfg, to_email, subject, html_body, text_body)
        else:
            logger.error("Unknown email provider: %s", provider)
            return False
    except Exception:
        logger.exception("Failed to send email to %s via %s", to_email, provider)
        return False


async def send_otp(to_email: str, code: str) -> bool:
    """Send an OTP verification code."""
    from services.platform_settings_service import settings
    platform_name = settings.get("branding.title", "Agience")

    subject = f"Your {platform_name} verification code"
    html_body = f"""
    <div style="font-family: system-ui, sans-serif; max-width: 400px; margin: 0 auto;">
        <h2 style="color: #1a1a1a;">Verification code</h2>
        <p style="color: #666; font-size: 14px;">Enter this code to sign in to {platform_name}:</p>
        <div style="background: #f5f5f5; border-radius: 8px; padding: 20px; text-align: center; margin: 20px 0;">
            <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #1a1a1a;">{code}</span>
        </div>
        <p style="color: #999; font-size: 12px;">This code expires in 10 minutes. If you didn't request this, ignore this email.</p>
    </div>
    """
    text_body = f"Your {platform_name} verification code is: {code}\n\nThis code expires in 10 minutes."

    return await send_email(to_email, subject, html_body, text_body)


async def send_password_reset(to_email: str, reset_url: str) -> bool:
    """Send a password reset link."""
    from services.platform_settings_service import settings
    platform_name = settings.get("branding.title", "Agience")

    subject = f"Reset your {platform_name} password"
    html_body = f"""
    <div style="font-family: system-ui, sans-serif; max-width: 400px; margin: 0 auto;">
        <h2 style="color: #1a1a1a;">Reset your password</h2>
        <p style="color: #666; font-size: 14px;">Click the link below to reset your {platform_name} password:</p>
        <a href="{reset_url}" style="display: inline-block; background: #1a1a1a; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; margin: 20px 0;">Reset Password</a>
        <p style="color: #999; font-size: 12px;">This link expires in 1 hour. If you didn't request this, ignore this email.</p>
    </div>
    """
    text_body = f"Reset your {platform_name} password: {reset_url}\n\nThis link expires in 1 hour."

    return await send_email(to_email, subject, html_body, text_body)


async def send_invite(
    to_email: str,
    from_name: str,
    resource_name: str,
    claim_url: str,
    message: Optional[str] = None,
) -> bool:
    """Send a workspace-share invite email.

    The email body identifies the inviter by name --- this is safe because
    delivery is to a verified address. The claim page itself withholds
    inviter identity until the recipient authenticates.
    """
    from services.platform_settings_service import settings
    platform_name = settings.get("branding.title", "Agience")

    message_block_html = ""
    message_block_text = ""
    if message:
        message_block_html = (
            '<p style="color:#444;font-size:14px;margin:16px 0;padding:12px;'
            'background:#f9f9f9;border-radius:6px;">'
            f'&ldquo;{message}&rdquo;</p>'
        )
        message_block_text = f'\n"{message}"\n'

    subject = f"{from_name} invited you to collaborate on {platform_name}"
    html_body = f"""
    <div style="font-family: system-ui, sans-serif; max-width: 480px; margin: 0 auto;">
        <h2 style="color: #1a1a1a;">You've been invited</h2>
        <p style="color: #666; font-size: 14px;"><strong>{from_name}</strong> invited you to <strong>{resource_name}</strong> on {platform_name}.</p>
        {message_block_html}
        <a href="{claim_url}" style="display: inline-block; background: #1a1a1a; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; margin: 20px 0;">View in {platform_name}</a>
        <p style="color: #999; font-size: 12px;">If you don't have an account yet, one will be created when you sign in.</p>
    </div>
    """
    text_body = (
        f"{from_name} invited you to {resource_name} on {platform_name}.\n"
        f"{message_block_text}\n"
        f"Open this link to accept: {claim_url}\n\n"
        "If you don't have an account yet, one will be created when you sign in."
    )

    return await send_email(to_email, subject, html_body, text_body)


async def test_connection(provider_config: dict) -> tuple[bool, Optional[str]]:
    """
    Test an email provider configuration without sending a real email.

    Returns (success, error_message).
    """
    provider = provider_config.get("provider", "")

    try:
        if provider == "smtp":
            smtp = aiosmtplib.SMTP(
                hostname=provider_config.get("smtp_host", ""),
                port=int(provider_config.get("smtp_port", 587)),
                use_tls=provider_config.get("smtp_use_tls", True),
            )
            await smtp.connect()
            if provider_config.get("smtp_username"):
                await smtp.login(
                    provider_config["smtp_username"],
                    provider_config.get("smtp_password", ""),
                )
            await smtp.quit()
            return True, None

        elif provider == "sendgrid":
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.sendgrid.com/v3/scopes",
                    headers={"Authorization": f"Bearer {provider_config.get('sendgrid_api_key', '')}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return True, None
                return False, f"SendGrid API returned {resp.status_code}"

        elif provider == "resend":
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.resend.com/domains",
                    headers={"Authorization": f"Bearer {provider_config.get('resend_api_key', '')}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return True, None
                return False, f"Resend API returned {resp.status_code}"

        elif provider == "ses":
            # SES test requires boto3
            import boto3
            ses = boto3.client(
                "ses",
                region_name=provider_config.get("ses_region", "us-east-1"),
                aws_access_key_id=provider_config.get("ses_access_key_id", ""),
                aws_secret_access_key=provider_config.get("ses_secret_access_key", ""),
            )
            ses.get_send_quota()
            return True, None

        else:
            return False, f"Unknown provider: {provider}"

    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
#  Provider implementations
# ---------------------------------------------------------------------------

async def _send_smtp(
    cfg: dict, to: str, subject: str, html: str, text: Optional[str]
) -> bool:
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{cfg['from_name']} <{cfg['from_address']}>"
    msg["To"] = to
    msg["Subject"] = subject

    if text:
        msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    smtp = aiosmtplib.SMTP(
        hostname=cfg["smtp_host"],
        port=cfg["smtp_port"],
        use_tls=cfg["smtp_use_tls"],
    )
    await smtp.connect()
    if cfg["smtp_username"]:
        await smtp.login(cfg["smtp_username"], cfg["smtp_password"])
    await smtp.send_message(msg)
    await smtp.quit()

    logger.info("Email sent via SMTP to %s", to)
    return True


async def _send_ses(
    cfg: dict, to: str, subject: str, html: str, text: Optional[str]
) -> bool:
    import boto3

    ses = boto3.client(
        "ses",
        region_name=cfg["ses_region"],
        aws_access_key_id=cfg["ses_access_key_id"],
        aws_secret_access_key=cfg["ses_secret_access_key"],
    )

    body = {"Html": {"Data": html}}
    if text:
        body["Text"] = {"Data": text}

    ses.send_email(
        Source=f"{cfg['from_name']} <{cfg['from_address']}>",
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject},
            "Body": body,
        },
    )

    logger.info("Email sent via SES to %s", to)
    return True


async def _send_sendgrid(
    cfg: dict, to: str, subject: str, html: str, text: Optional[str]
) -> bool:
    content = [{"type": "text/html", "value": html}]
    if text:
        content.insert(0, {"type": "text/plain", "value": text})

    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": cfg["from_address"], "name": cfg["from_name"]},
        "subject": subject,
        "content": content,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.sendgrid.com/v3/mail/send",
            json=payload,
            headers={
                "Authorization": f"Bearer {cfg['sendgrid_api_key']}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()

    logger.info("Email sent via SendGrid to %s", to)
    return True


async def _send_resend(
    cfg: dict, to: str, subject: str, html: str, text: Optional[str]
) -> bool:
    payload = {
        "from": f"{cfg['from_name']} <{cfg['from_address']}>",
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={
                "Authorization": f"Bearer {cfg['resend_api_key']}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()

    logger.info("Email sent via Resend to %s", to)
    return True
