from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDeliveryResult:
    mode: str
    development_url: str | None = None


def deliver_action_email(
    *,
    settings: Settings,
    recipient: str,
    subject: str,
    heading: str,
    action_label: str,
    action_url: str,
    expiry_text: str,
) -> EmailDeliveryResult:
    """Send one transactional email or expose the explicit local fallback.

    SMTP uses only the Python standard library. When it is not configured, the
    URL is logged for local development. The caller decides whether the URL is
    safe to return to a browser (development only).
    """

    if not settings.smtp_host:
        logger.warning("Development email fallback for %s: %s", recipient, action_url)
        return EmailDeliveryResult(mode="development", development_url=action_url)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email
    message["To"] = recipient
    message.set_content(
        f"{heading}\n\n{action_label}: {action_url}\n\nThis link expires in {expiry_text}."
    )
    message.add_alternative(
        (
            f"<h1>{heading}</h1>"
            f'<p><a href="{action_url}">{action_label}</a></p>'
            f"<p>This link expires in {expiry_text}.</p>"
        ),
        subtype="html",
    )

    try:
        with smtplib.SMTP(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
        ) as client:
            if settings.smtp_use_tls:
                client.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password or "")
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        logger.exception("Transactional email delivery failed for %s", recipient)
        return EmailDeliveryResult(mode="failed")

    return EmailDeliveryResult(mode="smtp")
