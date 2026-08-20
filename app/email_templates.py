# ruff: noqa: E501

"""Brand-safe transactional email templates for manager.sonnia.ai.

One shared frame with copy slots for the whole Transactional Email spec §8
family (verification, password reset, password changed).  The renderer
intentionally has no transport dependency: callers hand the returned MIME
message to the configured SMTP sender, and rendering stays separate so
previews and tests never send mail.

Email spec §7 technical requirements are met by the shared frame:
bulletproof table button, plain-text alternative, preheader, dark-mode meta,
``lang="en-GB"``, no tracking pixel, decorative images only, well under the
102KB limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from urllib.parse import quote, urlsplit

PREHEADER_TEXT = "A secure action on your Sonnia account — link works for 24 hours."
DEFAULT_SUBJECT = "Action needed on your Sonnia account"

# These are the light, paper-surface values from the Sonnia/Starlight CSS
# custom properties.  Email clients remove external CSS, so the values remain
# explicit in each inline declaration below.
CANVAS = "#F7F4EE"
SURFACE = "#FEFDFA"
LINE = "#E5E0D8"
TEXT = "#1C1917"
MUTED = "#5B554E"
DEEP_GREEN = "#0F4C46"
GOLD = "#9A6B12"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """The two representations required by ``multipart/alternative``."""

    html: str
    plain_text: str

    def as_message(
        self,
        *,
        to: str,
        from_address: str,
        subject: str = DEFAULT_SUBJECT,
    ) -> EmailMessage:
        """Build a real multipart/alternative message without sending it."""

        for header_name, value in (
            ("To", to),
            ("From", from_address),
            ("Subject", subject),
        ):
            if not value or any(character in value for character in "\r\n"):
                raise ValueError(f"{header_name} must be a non-empty single-line value")

        message = EmailMessage()
        message["To"] = to
        message["From"] = from_address
        message["Subject"] = subject
        message.set_content(self.plain_text, subtype="plain", charset="utf-8")
        message.add_alternative(self.html, subtype="html", charset="utf-8")
        return message


def action_url_for_token(
    path_prefix: str, token: str, *, base_url: str = "https://manager.sonnia.ai"
) -> str:
    """Create the untracked action URL from an opaque token.

    ``path_prefix`` is like ``/verify-email/`` or ``/reset-password/``.
    """

    token = token.strip()
    if not token or any(character in token for character in "/?#\\\r\n\t "):
        raise ValueError("action token must be a non-empty URL-safe path segment")
    base = base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or parsed.netloc != "manager.sonnia.ai":
        raise ValueError("action base URL must be https://manager.sonnia.ai")
    return f"{base}{path_prefix}{quote(token, safe='')}"


def render_action_email(
    *,
    first_name: str,
    url: str,
    label: str,
    headline: str,
    body: str,
    button_text: str,
    expiry_text: str,
    ignore_line: str,
    subject: str,
    preheader: str = PREHEADER_TEXT,
    status_line: str = "SECURITY",
    footer_address: str = "",
) -> RenderedEmail:
    """Render one §8 email through the shared brand frame.

    ``url`` may be empty for emails with no action button (for example the
    password-changed notice renders a link to reset instead); the button row
    is omitted in that case.
    """

    first_name = _required_slot(first_name, "first_name")
    label = _required_slot(label, "label")
    headline = _required_slot(headline, "headline")
    body = _required_slot(body, "body")
    button_text = _required_slot(button_text, "button_text")
    expiry_text = _required_slot(expiry_text, "expiry_text")
    ignore_line = _required_slot(ignore_line, "ignore_line")
    footer_address = (footer_address or "").strip()

    first_name_html = escape(first_name)
    label_html = escape(label)
    headline_html = escape(headline)
    body_html = _html_paragraphs(body)
    button_text_html = escape(button_text)
    expiry_html = escape(expiry_text)
    ignore_html = escape(ignore_line)
    status_html = escape(status_line)
    footer_address_html = escape(footer_address) if footer_address else ""
    url_html = escape(url, quote=True) if url else ""

    button_row = ""
    if url:
        button_row = (
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
            "<tr>"
            f'<td align="center" valign="middle" height="44" bgcolor="{DEEP_GREEN}" style="height:44px; min-height:44px; background-color:{DEEP_GREEN}; border-radius:8px;">'
            f'<a href="{url_html}" role="button" style="display:inline-block; min-height:44px; padding:0 24px; font-family:Arial, Helvetica, sans-serif; font-size:15px; line-height:44px; font-weight:bold; color:{SURFACE}; text-decoration:none; white-space:nowrap;">{button_text_html}</a>'
            "</td>"
            "</tr>"
            "</table>"
        )
        fallback_block = (
            f'<p style="margin:24px 0 6px; font-family:Arial, Helvetica, sans-serif; font-size:13px; line-height:20px; color:{MUTED};">If the button does not work, copy and paste this link:</p>'
            f'<p style="margin:0 0 24px; font-family:\'Courier New\', Courier, monospace; font-size:12px; line-height:18px; word-break:break-all;"><a href="{url_html}" style="color:{DEEP_GREEN}; text-decoration:underline;">{url_html}</a></p>'
        )
    else:
        fallback_block = ""

    html = f"""<!doctype html>
<html lang="en-GB">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <title>{headline_html}</title>
</head>
<body style="margin:0; padding:0; background-color:{CANVAS}; color:{TEXT};">
  <div style="display:none; max-height:0; max-width:0; overflow:hidden; opacity:0; mso-hide:all; font-size:1px; line-height:1px; color:{CANVAS};">
    {escape(preheader)}
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{CANVAS}" style="width:100%; margin:0; padding:0; background-color:{CANVAS};">
    <tr>
      <td align="center" style="padding:24px 12px 40px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="{SURFACE}" style="width:100%; max-width:600px; background-color:{SURFACE}; border:1px solid {LINE}; border-radius:16px;">
          <tr>
            <td style="padding:28px 28px 20px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="font-family:Arial, Helvetica, sans-serif; font-size:14px; line-height:20px; font-weight:bold; letter-spacing:0.12em; color:{DEEP_GREEN};">
                    SONNIA
                  </td>
                  <td align="right" style="font-family:'Courier New', Courier, monospace; font-size:11px; line-height:16px; letter-spacing:0.08em; color:{GOLD};">
                    {status_html}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 28px 36px;">
              <p style="margin:0 0 16px; font-family:'Courier New', Courier, monospace; font-size:12px; line-height:18px; letter-spacing:0.12em; text-transform:uppercase; color:{DEEP_GREEN};">{label_html}</p>
              <h1 style="margin:0 0 24px; font-family:Georgia, 'Times New Roman', Times, serif; font-size:42px; line-height:46px; font-weight:normal; letter-spacing:-0.02em; color:{TEXT};">{headline_html}</h1>
              <p style="margin:0 0 18px; font-family:Arial, Helvetica, sans-serif; font-size:16px; line-height:25px; color:{TEXT};">Hello, {first_name_html},</p>
              {body_html}
              <p style="margin:0 0 24px; font-family:Arial, Helvetica, sans-serif; font-size:14px; line-height:22px; color:{MUTED};">{expiry_html}</p>
              {button_row}
              {fallback_block}
              <p style="margin:0; font-family:Arial, Helvetica, sans-serif; font-size:13px; line-height:20px; color:{MUTED};">{ignore_html}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 28px 28px; border-top:1px solid {LINE};">
              <p style="margin:0 0 5px; font-family:Arial, Helvetica, sans-serif; font-size:12px; line-height:18px; color:{MUTED};">Sonnia · warm light in the machine</p>
              <p style="margin:0 0 5px; font-family:Arial, Helvetica, sans-serif; font-size:12px; line-height:18px;"><a href="https://sonnia.ai" style="color:{DEEP_GREEN}; text-decoration:underline;">sonnia.ai</a></p>
              <p style="margin:0; font-family:Arial, Helvetica, sans-serif; font-size:12px; line-height:18px; color:{MUTED};">Sonnia AI Limited{footer_address_html and " · " + footer_address_html or ""}</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    plain_text = "\n".join(
        part
        for part in (
            preheader,
            "",
            label,
            headline,
            "",
            f"Hello, {first_name},",
            "",
            body,
            "",
            expiry_text,
            "",
            f"{button_text}: {url}" if url else "",
            f"If the button does not work, copy and paste this link: {url}" if url else "",
            "",
            ignore_line,
            "",
            "Sonnia · warm light in the machine",
            "sonnia.ai",
            f"Sonnia AI Limited{(' · ' + footer_address) if footer_address else ''}",
            "",
        )
    )
    return RenderedEmail(html=html, plain_text=plain_text)


def render_verification_email(
    *,
    first_name: str,
    url: str,
    footer_address: str = "",
) -> RenderedEmail:
    """Verify-your-address email (Transactional Email spec §8)."""

    return render_action_email(
        first_name=first_name,
        url=url,
        label="NEW ACCOUNT · VERIFY",
        headline="one click and you're in",
        body=(
            "Your Sonnia account has been created, but it is inactive until you "
            "verify your email address."
        ),
        button_text="Verify my email address",
        expiry_text="This link works for 24 hours.",
        ignore_line=(
            "If you did not sign up for Sonnia, you can ignore this email. "
            "Your account has been created, but it will remain inactive until verified."
        ),
        subject="Verify your Sonnia account",
        preheader="One click and your Sonnia account is ready. Link works for 24 hours.",
        status_line="ACCOUNT · PENDING",
        footer_address=footer_address,
    )


def render_reset_email(
    *,
    first_name: str,
    url: str,
    footer_address: str = "",
) -> RenderedEmail:
    """Password-reset email (Transactional Email spec §8; Auth Flow prompt §5)."""

    return render_action_email(
        first_name=first_name,
        url=url,
        label="SECURITY · RESET",
        headline="let's get you back in",
        body=(
            "Use the link below to set a new password. For your security, "
            "the link works for one hour and can be used once."
        ),
        button_text="Reset password",
        expiry_text="This link expires in 1 hour.",
        ignore_line=(
            "If you did not request a password reset, you can ignore this email. "
            "Your password will stay exactly as it is."
        ),
        subject="Reset your Sonnia password",
        preheader="Reset your Sonnia password — the link works for one hour.",
        status_line="SECURITY · RESET",
        footer_address=footer_address,
    )


def render_password_changed_email(
    *,
    first_name: str,
    reset_url: str,
    footer_address: str = "",
) -> RenderedEmail:
    """Password-changed notice (Registration & Password spec §4.4)."""

    return render_action_email(
        first_name=first_name,
        url=reset_url,
        label="SECURITY · CHANGED",
        headline="your password was changed",
        body=(
            "Your Sonnia password was just changed, and any other signed-in "
            "sessions have been signed out."
        ),
        button_text="Reset password",
        expiry_text="If this was not you, use the link below to secure your account.",
        ignore_line=(
            "Wasn't you? Reset your password immediately with the link above, "
            "then contact support at support@sonnia.ai."
        ),
        subject="Your Sonnia password was changed",
        preheader="Your Sonnia password was changed. Wasn't you? Act now.",
        status_line="SECURITY · CHANGED",
        footer_address=footer_address,
    )


def _required_slot(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _html_paragraphs(value: str) -> str:
    paragraphs = [part.strip() for part in value.split("\n\n") if part.strip()]
    return "\n".join(
        f'<p style="margin:0 0 18px; font-family:Arial, Helvetica, sans-serif; font-size:16px; line-height:25px; color:{TEXT};">{escape(paragraph, quote=False).replace(chr(10), "<br>")}</p>'
        for paragraph in paragraphs
    )
