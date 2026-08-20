from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import Settings
from app.email_templates import RenderedEmail

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDeliveryResult:
    mode: str
    development_url: str | None = None


def deliver_rendered_email(
    *,
    settings: Settings,
    recipient: str,
    subject: str,
    rendered: RenderedEmail,
) -> EmailDeliveryResult:
    """Send one branded transactional email through the shared template.

    SMTP uses only the Python standard library. When SMTP is not configured,
    the first action URL found in the rendered body is logged for local
    development. The caller decides whether the URL is safe to return to a
    browser (development only).
    """

    if not settings.smtp_host:
        logger.warning("Development email fallback for %s", recipient)
        return EmailDeliveryResult(mode="development", development_url=None)

    message = rendered.as_message(
        to=recipient,
        from_address=settings.smtp_from_email,
        subject=subject,
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
    """Compatibility shim: render the unbranded fallback through the frame.

    Kept for callers that have not yet been migrated to the shared template;
    production callers use :func:`deliver_rendered_email`.
    """

    from app.email_templates import render_action_email

    rendered = render_action_email(
        first_name="there",
        url=action_url,
        label="ACTION REQUIRED",
        headline=heading,
        body=action_label,
        button_text=action_label,
        expiry_text=expiry_text,
        ignore_line="If you did not request this, you can ignore this email.",
        subject=subject,
    )
    return deliver_rendered_email(
        settings=settings,
        recipient=recipient,
        subject=subject,
        rendered=rendered,
    )


def _extract_action_url(message: EmailMessage) -> str | None:
    """Find the first https URL in the HTML part for the dev fallback."""
    import re

    html = message.get_body(preferencelist=("html",))
    if html is None:
        return None
    match = re.search(r"https://[^\s\"']+", html.get_content() or "")
    return match.group(0) if match else None
