# ruff: noqa: E501

"""Brand-safe transactional email family for manager.sonnia.ai (SON-1721).

Szonja spec (specs/szonja-spec-2026-09-02, verification-email spec): one
shared partial with slots for label / headline / body / button text / button
URL, six family emails (verify, welcome, reset, password changed, team
invite, new sign-in), tables-only layout, inline styles, 600px max,
multipart plain-text alternative, premium colour-scheme meta, lang=en-GB,
hidden preheader with zwnj run, no tracking pixel, token only in the URL.

Palette and type are the exact values from the canonical token contract
(``starlight/frontend/design-system/sonnia-ai.tokens.css`` v1.0.0, the
verified SON-1151 handoff) — not eyeballed:

* background  --sonnia-color-paper      #f4ead8 (warm cream)
* panel       --sonnia-color-card       #fff7e8
* text        --sonnia-color-ink        #25211d (near-black)
* muted       --sonnia-color-ink-soft   #5f554a
* hairline    --sonnia-border-subtle    #cdb89a
* accent      --sonnia-color-sage-ink   #46663d (deep green, the site's
              numbered-step / sign-in accent; AA on near-white fills)
* status tag  --sonnia-color-yellow-ink #745200
* headline    --sonnia-font-display     'Fraunces', Georgia, serif
* mono        --sonnia-font-mono        'Space Mono', ui-monospace, monospace

The renderer intentionally has no transport dependency: callers hand the
returned MIME message to the configured SMTP sender, and rendering stays
separate so previews and tests never send mail.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from urllib.parse import quote, urlsplit

# ── Canonical palette (sonnia-ai.tokens.css v1.0.0 — do not eyeball) ──────
CANVAS = "#f4ead8"  # --sonnia-color-paper
SURFACE = "#fff7e8"  # --sonnia-color-card
LINE = "#cdb89a"  # --sonnia-border-subtle
TEXT = "#25211d"  # --sonnia-color-ink
MUTED = "#5f554a"  # --sonnia-color-ink-soft
DEEP_GREEN = "#46663d"  # --sonnia-color-sage-ink
BUTTON_TEXT = "#fffdf7"  # spec §6 sample; ≥6:1 on DEEP_GREEN (AA)
STATUS_TAG = "#745200"  # --sonnia-color-yellow-ink
DARK_CANVAS = "#25211d"
DARK_CARD = "#2e2822"
DARK_LINE = "#4a4238"
DARK_TEXT = "#f4ead8"
DARK_MUTED = "#b3a89a"
DARK_LINK = "#9dbb92"  # light sage for dark surfaces

SERIF = "'Fraunces', Georgia, 'Times New Roman', serif"
MONO = "'Space Mono', 'Courier New', Courier, monospace"

COMPANY_NAME = "Sonnia AI Limited"
REGISTERED_OFFICE = "Flat 1 Niemann Court, Louis Close, London, N7 0FQ"
COMPANY_NUMBER = "17173059"
FOOTER_LEGAL = (
    f"{COMPANY_NAME} · {REGISTERED_OFFICE} · "
    "Registered in England & Wales No. "
    f"{COMPANY_NUMBER}"
)

# ── Family subjects (spec §4: plain, no emoji, no exclamation marks) ──────
VERIFICATION_SUBJECT = "Verify your email address"
WELCOME_SUBJECT = "Your Sonnia account is active"
RESET_SUBJECT = "Reset your Sonnia password"
PASSWORD_CHANGED_SUBJECT = "Your Sonnia password was changed"
NEW_SIGNIN_SUBJECT = "New sign-in to your Sonnia account"

ZWNJ_RUN = "&#8204;&nbsp;&#8204;&nbsp;&#8204;&nbsp;&#8204;&nbsp;"


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
        subject: str = "",
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
    The token is the only user-specific data in the URL (spec §7).
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
    expiry_text: str = "",
    ignore_line: str,
    subject: str,
    preheader: str,
    status_line: str = "",
    status_chip: str = "",
    footer_address: str = "",
) -> RenderedEmail:
    """Render one §8 family email through the shared brand partial.

    Slots: ``label``, ``headline``, ``body``, ``button_text``, ``url``
    (plus the decorative ``status_chip`` band and header ``status_line``).
    ``url`` may be empty for notice emails; the button row is omitted and
    the fallback block shrinks accordingly. Everything essential is live
    text — the email is fully usable with images blocked (there are none).
    """

    first_name = _required_slot(first_name, "first_name")
    label = _required_slot(label, "label")
    headline = _required_slot(headline, "headline")
    body = _required_slot(body, "body")
    button_text = _required_slot(button_text, "button_text")
    ignore_line = _required_slot(ignore_line, "ignore_line")
    footer_address = (footer_address or "").strip()

    first_name_html = escape(first_name)
    label_html = escape(label)
    headline_html = escape(headline)
    body_html = _html_paragraphs(body)
    button_text_html = escape(button_text)
    ignore_html = escape(ignore_line)
    status_html = escape(status_line)
    chip_html = escape(status_chip, quote=True) if status_chip else ""
    footer_address_html = escape(footer_address) if footer_address else ""
    url_html = escape(url, quote=True) if url else ""

    button_row = ""
    fallback_html = ""
    fallback_text = ""
    if url:
        # Bulletproof table button (spec §6): padded <a> inside a coloured
        # <td>, never a bare styled link. ≥44px tap target.
        button_row = (
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:28px 0 0;">'
            "<tr>"
            f'<td align="center" valign="middle" height="48" bgcolor="{DEEP_GREEN}" '
            f'style="height:48px; min-height:48px; background-color:{DEEP_GREEN}; border-radius:8px;">'
            f'<a href="{url_html}" role="button" '
            f'style="display:inline-block; padding:14px 32px; font-family:{SERIF}; font-size:16px; '
            f'line-height:20px; color:{BUTTON_TEXT}; text-decoration:none; border-radius:8px; white-space:nowrap;">'
            f"{button_text_html}</a>"
            "</td>"
            "</tr>"
            "</table>"
        )
    if url:
        fallback_html = (
            f'<p style="margin:26px 0 6px; font-family:{MONO}; font-size:12px; line-height:19px; color:{MUTED};">Button not working? Paste this into your browser:</p>'
            f'<p style="margin:0; font-family:{MONO}; font-size:12px; line-height:19px; word-break:break-all;"><a href="{url_html}" class="u-link" style="color:{DEEP_GREEN}; text-decoration:underline;">{url_html}</a></p>'
        )
        fallback_text = f"Button not working? Paste this into your browser:\n{url}"
    expiry_html = (
        (
            f'<p style="margin:24px 0 0; font-family:{MONO}; font-size:13px; line-height:21px; color:{MUTED};">{escape(expiry_text)}</p>'
        )
        if expiry_text
        else ""
    )

    chip_row = (
        f'<p style="margin:0 0 28px;"><span class="u-chip" style="display:inline-block; font-family:{MONO}; font-size:11px; line-height:16px; letter-spacing:0.14em; color:{DEEP_GREEN}; border:1px solid {LINE}; border-radius:999px; padding:7px 14px; background-color:{CANVAS};">{chip_html}</span></p>'
        if chip_html
        else ""
    )
    status_cell = (
        f'<td align="right" class="u-muted" style="font-family:{MONO}; font-size:11px; line-height:16px; letter-spacing:0.08em; text-transform:uppercase; color:{STATUS_TAG};">{status_html}</td>'
        if status_html
        else ""
    )
    dark_style = (
        "<style type=\"text/css\">\n"
        "@media (prefers-color-scheme: dark) {\n"
        f"  .u-canvas {{ background-color:{DARK_CANVAS} !important; }}\n"
        f"  .u-card {{ background-color:{DARK_CARD} !important; border-color:{DARK_LINE} !important; }}\n"
        f"  .u-ink {{ color:{DARK_TEXT} !important; }}\n"
        f"  .u-muted {{ color:{DARK_MUTED} !important; }}\n"
        f"  .u-line {{ border-color:{DARK_LINE} !important; }}\n"
        f"  .u-link {{ color:{DARK_LINK} !important; }}\n"
        f"  .u-chip {{ color:{DARK_LINK} !important; border-color:{DARK_LINE} !important; background-color:{DARK_CARD} !important; }}\n"
        "}\n"
        "</style>"
    )

    html = f"""<!doctype html>
<html lang="en-GB">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <title>{headline_html}</title>
  {dark_style}
</head>
<body class="u-canvas" style="margin:0; padding:0; background-color:{CANVAS}; color:{TEXT};">
  <div style="display:none; max-height:0; max-width:0; overflow:hidden; opacity:0; mso-hide:all; font-size:1px; line-height:1px; color:{CANVAS};">
    {escape(preheader)}{ZWNJ_RUN}
  </div>
  <table role="presentation" class="u-canvas" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{CANVAS}" style="width:100%; margin:0; padding:0; background-color:{CANVAS};">
    <tr>
      <td align="center" style="padding:24px 12px 40px;">
        <table role="presentation" class="u-card" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="{SURFACE}" style="width:100%; max-width:600px; background-color:{SURFACE}; border:1px solid {LINE}; border-radius:16px;">
          <tr>
            <td class="u-ink" style="padding:28px 28px 18px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td class="u-ink" style="font-family:{MONO}; font-size:14px; line-height:20px; font-weight:bold; letter-spacing:0.2em; color:{TEXT};">&#10022; SONNIA</td>
                  {status_cell}
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td class="u-line" style="padding:0 28px;"><div style="border-top:1px solid {LINE}; font-size:0; line-height:0;">&nbsp;</div></td>
          </tr>
          <tr>
            <td class="u-ink" style="padding:30px 28px 8px;">
              {chip_row}
              <p class="u-muted" style="margin:0 0 14px; font-family:{MONO}; font-size:12px; line-height:18px; letter-spacing:0.16em; text-transform:uppercase; color:{DEEP_GREEN};">{label_html}</p>
              <h1 class="u-ink" style="margin:0 0 22px; font-family:{SERIF}; font-size:40px; line-height:46px; font-weight:normal; letter-spacing:-0.01em; color:{TEXT};">{headline_html}</h1>
              <p class="u-ink" style="margin:0 0 18px; font-family:{MONO}; font-size:16px; line-height:26px; color:{TEXT};">Hello {first_name_html},</p>
              {body_html}
              {button_row}
              {expiry_html}
              {fallback_html}
            </td>
          </tr>
          <tr>
            <td class="u-ink" style="padding:26px 28px 30px;">
              <div class="u-line" style="border-top:1px solid {LINE}; font-size:0; line-height:0; margin:0 0 20px;">&nbsp;</div>
              <p class="u-muted" style="margin:0; font-family:{MONO}; font-size:13px; line-height:21px; color:{MUTED};">{ignore_html}</p>
            </td>
          </tr>
          <tr>
            <td class="u-line" style="padding:20px 28px 28px; border-top:1px solid {LINE};">
              <p class="u-muted" style="margin:0 0 5px; font-family:{MONO}; font-size:12px; line-height:18px; color:{MUTED};">Sonnia · warm light in the machine</p>
              <p style="margin:0 0 5px; font-family:{MONO}; font-size:12px; line-height:18px;"><a href="https://sonnia.ai" class="u-link" style="color:{DEEP_GREEN}; text-decoration:underline;">sonnia.ai</a></p>
              <p class="u-muted" style="margin:0; font-family:{MONO}; font-size:11px; line-height:18px; color:{MUTED};">{footer_address_html or FOOTER_LEGAL}</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    plain_parts = [preheader, "", label, headline, "", f"Hello {first_name},", "", body]
    plain_parts += ["", button_text + ":", url] if url else []
    if expiry_text:
        plain_parts += ["", expiry_text]
    if fallback_text:
        plain_parts += ["", fallback_text]
    plain_parts += ["", ignore_line, "", "Sonnia · warm light in the machine", "sonnia.ai", footer_address_html or FOOTER_LEGAL, ""]
    plain_text = "\n".join(plain_parts)
    return RenderedEmail(html=html, plain_text=plain_text)


def render_verification_email(
    *,
    first_name: str,
    url: str,
    footer_address: str = "",
) -> RenderedEmail:
    """Verify email — spec §4 exact copy, §8 row 1."""

    return render_action_email(
        first_name=first_name,
        url=url,
        label="NEW ACCOUNT · VERIFY",
        headline="one click and you're in",
        body="Confirm this is your address and your account is ready.",
        button_text="Verify my email address",
        expiry_text=(
            "This link works for 24 hours. If it expires, request a new one "
            "from the sign-in page."
        ),
        ignore_line=(
            "Didn't sign up? Ignore this email — the account stays inactive "
            "without this step."
        ),
        subject=VERIFICATION_SUBJECT,
        preheader="One click and your Sonnia account is ready. Link works for 24 hours.",
        status_line="sonnia.ai",
        status_chip="LINE_01  STATUS: PENDING → CONNECTED",
        footer_address=footer_address,
    )


def render_welcome_email(
    *,
    first_name: str,
    url: str,
    footer_address: str = "",
) -> RenderedEmail:
    """Welcome email, sent after verification — spec §8 row 2."""

    return render_action_email(
        first_name=first_name,
        url=url,
        label="ACCOUNT · ACTIVE",
        headline="you're in",
        body="Here's what to set up first.",
        button_text="Open your dashboard",
        ignore_line=(
            "You're receiving this because you just verified your email "
            "address. No action is needed."
        ),
        subject=WELCOME_SUBJECT,
        preheader="Your Sonnia account is ready — here's what to set up first.",
        status_line="sonnia.ai",
        status_chip="LINE_01  STATUS: CONNECTED",
        footer_address=footer_address,
    )


def render_reset_email(
    *,
    first_name: str,
    url: str,
    footer_address: str = "",
) -> RenderedEmail:
    """Password reset — spec §8 row 3."""

    return render_action_email(
        first_name=first_name,
        url=url,
        label="SECURITY · RESET",
        headline="let's get you back in",
        body="Use the link below to set a new password. Works for one hour.",
        button_text="Reset password",
        expiry_text="This link expires in 1 hour.",
        ignore_line=(
            "If you did not request a password reset, you can ignore this "
            "email. Your password will stay exactly as it is."
        ),
        subject=RESET_SUBJECT,
        preheader="Reset your Sonnia password — the link works for one hour.",
        status_line="security",
        footer_address=footer_address,
    )


def render_password_changed_email(
    *,
    first_name: str,
    reset_url: str,
    footer_address: str = "",
) -> RenderedEmail:
    """Password-changed notice — spec §8 row 4."""

    return render_action_email(
        first_name=first_name,
        url=reset_url,
        label="SECURITY · CHANGED",
        headline="your password was changed",
        body="If that wasn't you, secure your account now.",
        button_text="Secure your account",
        ignore_line=(
            "Wasn't you? Reset your password immediately with the link above, "
            "then contact support at support@sonnia.ai."
        ),
        subject=PASSWORD_CHANGED_SUBJECT,
        preheader="Your Sonnia password was changed. Wasn't you? Act now.",
        status_line="security",
        footer_address=footer_address,
    )


def render_team_invite_email(
    *,
    inviter: str,
    company: str,
    url: str,
    first_name: str = "there",
    expires_text: str = "This invitation works for 7 days.",
    footer_address: str = "",
) -> RenderedEmail:
    """Team invite — spec §8 row 5. ``inviter``/``company`` are escaped."""

    return render_action_email(
        first_name=first_name,
        url=url,
        label="INVITATION",
        headline=f"{inviter} would like you to join {company}",
        body="Accept below and you'll share the same Sonnia.",
        button_text="Accept your invitation",
        expiry_text=expires_text,
        ignore_line=(
            "Wasn't expecting this? You can ignore this email — the "
            "invitation expires on its own."
        ),
        subject=f"{inviter} invited you to {company} on Sonnia",
        preheader=f"{inviter} would like you to join {company} on Sonnia.",
        status_line="invitation",
        footer_address=footer_address,
    )


def render_new_signin_email(
    *,
    first_name: str,
    device: str,
    signed_in_at: str,
    url: str,
    location: str | None = None,
    footer_address: str = "",
) -> RenderedEmail:
    """New sign-in notice — spec §8 row 6.

    ``signed_in_at`` is a pre-formatted UTC timestamp; ``device`` a short
    client description derived from the request user agent.
    """

    where = f"{device}, {location}, {signed_in_at}" if location else f"{device}, {signed_in_at}"
    return render_action_email(
        first_name=first_name,
        url=url,
        label="SECURITY · NEW SIGN-IN",
        headline="a new sign-in on your account",
        body=f"{where}. Not you?",
        button_text="Secure your account",
        expiry_text="If this wasn't you, reset your password straight away.",
        ignore_line=(
            "You're receiving this because there was a new sign-in on your "
            "Sonnia account."
        ),
        subject=NEW_SIGNIN_SUBJECT,
        preheader="A new sign-in on your Sonnia account. Not you? Secure it now.",
        status_line="security",
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
        f'<p class="u-ink" style="margin:0 0 18px; font-family:{MONO}; font-size:16px; line-height:26px; color:{TEXT};">{escape(paragraph, quote=False).replace(chr(10), "<br>")}</p>'
        for paragraph in paragraphs
    )
