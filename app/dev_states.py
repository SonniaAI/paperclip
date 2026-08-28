# ruff: noqa: E501 -- embedded page CSS/HTML follows the dev_preview.py /
# email_templates.py repo precedent where template literals legitimately
# exceed the line budget.

"""Dev-only auth-state gallery (SON-883 / SON-1357).

``/dev/states`` puts every auth state from the Auth Flow prompt §3 table on
one labelled page so Szonja can review register / login / forgot / reset /
check-email states without registering accounts or triggering real flows.
Each card is a static mock with plausible dummy data — these are labelled
state sketches, not the live frontend components, and nothing here restyles
the real auth pages (problems are noted, not fixed — spec §6).

Non-negotiables held by this module:
- Server-side environment gating lives on the route (see app/main.py):
  production answers 404 (not 403), before any handler body runs.
- Dummy data only — the sample persona "Alex" / alex@example.com; no real
  user emails, names, or tokens are ever rendered.
- No database access, no auth/roles, no writes of any kind.
- Colours come exclusively from the SON-919 token set already used by the
  production email builders (amber = attention/error, green = success).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape

_SAMPLE_EMAIL = "alex@example.com"
_DUMMY_TOKEN_NOTE = "sample token (dummy data only — never a real token)"

# ---------------------------------------------------------------------------
# Mock-UI helpers. Everything is inline-styled so a single card can be pasted
# into bug reports or design review without losing fidelity. Tokens only:
# ink #1C1917 · paper #F7F4EE · card #FEFDFA · border #E5E0D8 · text #5B554E
# accent #0F4C46 (success/primary) · amber #9A6B12 (attention/error).
# ---------------------------------------------------------------------------


def _field(
    label: str,
    value: str = "",
    *,
    kind: str = "text",
    hint: str | None = None,
    error: str | None = None,
    focus: bool = False,
    disabled: bool = False,
) -> str:
    border = (
        "1px solid #0F4C46; box-shadow: 0 0 0 1px #0F4C46"
        if focus
        else ("1px solid #9A6B12" if error else "1px solid #E5E0D8")
    )
    shown = f'value="{escape(value)}"' if value else ""
    parts = [
        f'<label style="display:block; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:#5B554E; margin:0 0 4px;">{escape(label)}</label>',
        f'<input type="{kind}" {shown} placeholder="{escape(label)}" '
        f'style="width:100%; box-sizing:border-box; padding:9px 10px; font:13px/1.4 ui-sans-serif, sans-serif; color:#1C1917; background:#FFFFFF; border:{border}; border-radius:6px; outline:none;"'
        + (" disabled" if disabled else "")
        + ">",
    ]
    if error:
        parts.append(
            f'<div style="margin-top:4px; font-size:12px; color:#9A6B12;">{escape(error)}</div>'
        )
    elif hint:
        parts.append(
            f'<div style="margin-top:4px; font-size:12px; color:#5B554E;">{escape(hint)}</div>'
        )
    return f'<div style="margin-bottom:12px;">{"".join(parts)}</div>'


def _button(
    label: str,
    *,
    busy: bool = False,
    disabled: bool = False,
    variant: str = "primary",
) -> str:
    if variant == "quiet":
        base = "background:#FEFDFA; color:#0F4C46; border:1px solid #0F4C46;"
    else:
        base = "background:#0F4C46; color:#FEFDFA; border:1px solid #0F4C46;"
    opacity = "opacity:.55; cursor:default;" if (disabled or busy) else ""
    content = (
        f'<span class="mp_spinner" style="display:inline-block; width:12px; height:12px; margin-right:8px;"></span>{escape(label)}'
        if busy
        else escape(label)
    )
    return (
        f'<button type="button" style="display:flex; align-items:center; justify-content:center; width:100%; padding:10px 12px; '
        f"font:600 13px/1 ui-sans-serif, sans-serif; border-radius:6px; {base} {opacity}\""
        + (" disabled" if (disabled or busy) else "")
        + f">{content}</button>"
    )


def _banner(kind: str, text: str) -> str:
    tone = (
        ("background:#FEFDFA; border:1px solid #9A6B12; color:#9A6B12;")
        if kind == "error"
        else ("background:#FEFDFA; border:1px solid #0F4C46; color:#0F4C46;" if kind == "success" else "background:#FEFDFA; border:1px solid #E5E0D8; color:#5B554E;")
    )
    return (
        f'<div style="margin:0 0 12px; padding:10px 12px; font:13px/1.5 ui-sans-serif, sans-serif; border-radius:6px; {tone}">'
        f"{escape(text)}</div>"
    )


def _rules(met: set[str]) -> str:
    all_rules = ("At least 12 characters", "Contains a number", "Contains a symbol")
    rows = []
    for rule in all_rules:
        mark, tone = ("✓", "#0F4C46") if rule in met else ("✗", "#5B554E")
        rows.append(
            f'<li style="margin:0 0 3px; font-size:12px; color:{tone}; list-style:none;">'
            f"<span style=\"font-family:ui-monospace,monospace;\">{mark}</span> {escape(rule)}</li>"
        )
    return f'<ul style="margin:0 0 12px; padding:0;">{"".join(rows)}</ul>'


def _panel(title: str, body: str, *, tone: str = "info") -> str:
    border = "#0F4C46" if tone == "success" else "#E5E0D8"
    return (
        f'<div style="padding:16px; background:#FEFDFA; border:1px solid {border}; border-radius:8px; text-align:center;">'
        f'<div style="font:600 14px/1.3 ui-sans-serif, sans-serif; color:#1C1917; margin-bottom:8px;">{escape(title)}</div>'
        f'<div style="font:13px/1.6 ui-sans-serif, sans-serif; color:#5B554E;">{body}</div></div>'
    )


def _card(body: str) -> str:
    return (
        f'<div style="width:100%; max-width:340px; padding:18px; background:#FEFDFA; '
        f'border:1px solid #E5E0D8; border-radius:10px;">{body}</div>'
    )


# ---------------------------------------------------------------------------
# The Auth Flow prompt §3 table, as mock renders. Every state is labelled with
# exactly the §3 wording so the page doubles as a checklist.
# ---------------------------------------------------------------------------


def _register_empty() -> str:
    return _card(
        _field("Full name") + _field("Email") + _field("Password", kind="password")
        + _button("Create account")
    )


def _register_validating() -> str:
    return _card(
        _field("Full name", "Alex Szabo")
        + _field("Email", "alex@example.com", focus=True, hint="Checking this email…")
        + _field("Password", "••••••••••", kind="password")
        + _button("Create account", disabled=True)
    )


def _register_field_errors() -> str:
    return _card(
        _field("Full name", "", error="Required")
        + _field("Email", "alex-example", error="Enter a valid email address")
        + _field("Password", "short", kind="password", error="At least 12 characters, a number, a symbol")
        + _button("Create account", disabled=True)
    )


def _register_server_error() -> str:
    return _card(
        _banner("error", "Something went wrong creating your account. Please try again.")
        + _field("Full name", "Alex Szabo")
        + _field("Email", "alex@example.com")
        + _field("Password", "••••••••••", kind="password")
        + _button("Create account")
    )


def _register_submitting() -> str:
    return _card(
        _field("Full name", "Alex Szabo", disabled=True)
        + _field("Email", "alex@example.com", disabled=True)
        + _field("Password", "••••••••••", kind="password", disabled=True)
        + _button("Creating account…", busy=True)
    )


def _register_success() -> str:
    return _card(
        _panel(
            "Check your email",
            "We sent a verification link to <b>alex@example.com</b>.<br>Didn't get it? Check spam or resend from this page.",
            tone="success",
        )
    )


def _login_empty() -> str:
    return _card(_field("Email") + _field("Password", kind="password") + _button("Sign in"))


def _login_wrong_credentials() -> str:
    return _card(
        _banner("error", "Email or password is incorrect.")
        + _field("Email", "alex@example.com")
        + _field("Password", "••••••••••", kind="password")
        + _button("Sign in")
    )


def _login_unverified() -> str:
    return _card(
        _banner("error", "Verify your email before signing in — we sent a link to alex@example.com.")
        + _field("Email", "alex@example.com", disabled=True)
        + _field("Password", "••••••••••", kind="password", disabled=True)
        + _button("Resend verification email", variant="quiet")
    )


def _login_rate_limited() -> str:
    return _card(
        _banner("error", "Too many attempts. Try again in 14 minutes.")
        + _field("Email", "alex@example.com", disabled=True)
        + _field("Password", "", kind="password", disabled=True)
        + _button("Sign in", disabled=True)
    )


def _login_2fa() -> str:
    return _card(
        _banner("info", "Enter the 6-digit code from your authenticator app.")
        + _field("6-digit code", "04_", kind="text", focus=True, hint="Codes expire after 30 seconds")
        + _button("Verify")
        + '<div style="margin-top:10px; text-align:center;"><span style="font:12px/1 ui-sans-serif,sans-serif; color:#0F4C46; text-decoration:underline;">Use a recovery code</span></div>'
    )


def _forgot_empty() -> str:
    return _card(
        _panel("Reset your password", "Enter your account email and we'll send a reset link.")
        + _field("Email")
        + _button("Send reset link")
    )


def _forgot_submitting() -> str:
    return _card(
        _panel("Reset your password", "Enter your account email and we'll send a reset link.")
        + _field("Email", "alex@example.com", disabled=True)
        + _button("Sending…", busy=True)
    )


def _forgot_confirmation_sent() -> str:
    return _card(
        _panel(
            "Check your email",
            "If an account exists for <b>alex@example.com</b>, a reset link is on its way. The link expires in 60 minutes.",
            tone="success",
        )
    )


def _reset_empty() -> str:
    return _card(
        _panel("Choose a new password", "This link is valid for 60 minutes.")
        + _field("New password", kind="password")
        + _field("Confirm password", kind="password")
        + _button("Update password")
    )


def _reset_rules_unmet() -> str:
    return _card(
        _panel("Choose a new password", "This link is valid for 60 minutes.")
        + _field("New password", "correcthorse", kind="password")
        + _rules(set())
        + _button("Update password", disabled=True)
    )


def _reset_rules_met() -> str:
    return _card(
        _panel("Choose a new password", "This link is valid for 60 minutes.")
        + _field("New password", "••••••••••••", kind="password", hint="Looks good")
        + _rules({"At least 12 characters", "Contains a number", "Contains a symbol"})
        + _field("Confirm password", kind="password")
        + _button("Update password")
    )


def _reset_not_matching() -> str:
    return _card(
        _panel("Choose a new password", "This link is valid for 60 minutes.")
        + _field("New password", "••••••••••••", kind="password")
        + _field("Confirm password", "•••••••", kind="password", error="Passwords do not match")
        + _button("Update password", disabled=True)
    )


def _reset_expired_token() -> str:
    return _card(
        _banner("error", "This reset link has expired. Request a new one.")
        + _panel("Reset link expired", "Reset links expire after 60 minutes for security.")
        + _button("Request a new reset link", variant="quiet")
    )


def _reset_used_token() -> str:
    return _card(
        _banner("error", "This reset link was already used. Request a new one.")
        + _panel("Reset link already used", "Each link works once — request a fresh one to try again.")
        + _button("Request a new reset link", variant="quiet")
    )


def _reset_success() -> str:
    return _card(
        _panel(
            "Password updated",
            "Your password has been changed. You can now sign in with your new password.",
            tone="success",
        )
        + _button("Go to sign in")
    )


def _check_email_default() -> str:
    return _card(
        _panel(
            "Verify your email",
            "We sent a verification link to <b>alex@example.com</b>. It expires in 24 hours.",
        )
        + _button("Resend email")
        + '<div style="margin-top:10px; text-align:center;"><span style="font:12px/1 ui-sans-serif,sans-serif; color:#5B554E;">Wrong address? <span style="color:#0F4C46; text-decoration:underline;">Edit it</span></span></div>'
    )


def _check_email_resend_cooldown() -> str:
    return _card(
        _panel(
            "Verify your email",
            "We sent a verification link to <b>alex@example.com</b>. It expires in 24 hours.",
        )
        + _button("Resend available in 00:47", disabled=True)
    )


def _check_email_resend_failed() -> str:
    return _card(
        _banner("error", "Couldn't resend the email. Please try again.")
        + _panel("Verify your email", "The last resend attempt failed.")
        + _button("Try again")
    )


@dataclass(frozen=True, slots=True)
class AuthState:
    """One labelled state card from the Auth Flow prompt §3 table."""

    slug: str
    label: str
    note: str
    render: Callable[[], str]


@dataclass(frozen=True, slots=True)
class AuthFlowGroup:
    """One auth flow and its §3 states."""

    slug: str
    title: str
    lede: str
    states: tuple[AuthState, ...]


AUTH_FLOW_GROUPS: tuple[AuthFlowGroup, ...] = (
    AuthFlowGroup(
        slug="register",
        title="Register",
        lede="Account creation — all six §3 register states.",
        states=(
            AuthState("register-empty", "Empty", "First paint: no interaction yet, submit enabled.", _register_empty),
            AuthState("register-validating", "Validating", "Field-level validation in flight (async email check); submit blocked.", _register_validating),
            AuthState("register-field-errors", "Field errors", "Per-field contract failures (required / email format / password rules); submit blocked.", _register_field_errors),
            AuthState("register-server-error", "Server error", "Non-2xx from the API; values retained so the user can retry.", _register_server_error),
            AuthState("register-submitting", "Submitting", "In-flight; single-submit lock, all fields disabled.", _register_submitting),
            AuthState("register-success", "Success", "Verification email queued — routes to the check-email screen.", _register_success),
        ),
    ),
    AuthFlowGroup(
        slug="login",
        title="Login",
        lede="Sign-in — all five §3 login states.",
        states=(
            AuthState("login-empty", "Empty", "First paint.", _login_empty),
            AuthState("login-wrong-credentials", "Wrong credentials", "401 from the API; deliberately vague message.", _login_wrong_credentials),
            AuthState("login-unverified", "Unverified", "Account exists but email unverified; resend offered inline.", _login_unverified),
            AuthState("login-rate-limited", "Rate-limited", "Too many attempts; cooldown countdown, form locked.", _login_rate_limited),
            AuthState("login-2fa", "2FA", "Second factor: 6-digit TOTP with recovery-code fallback.", _login_2fa),
        ),
    ),
    AuthFlowGroup(
        slug="forgot",
        title="Forgot password",
        lede="Request a reset — all three §3 forgot states.",
        states=(
            AuthState("forgot-empty", "Empty", "First paint.", _forgot_empty),
            AuthState("forgot-submitting", "Submitting", "In-flight; single-submit lock.", _forgot_submitting),
            AuthState("forgot-confirmation-sent", "Confirmation sent", "Deliberately account-agnostic copy either way.", _forgot_confirmation_sent),
        ),
    ),
    AuthFlowGroup(
        slug="reset",
        title="Reset password",
        lede="Set a new password from a reset link — all seven §3 reset states.",
        states=(
            AuthState("reset-empty", "Empty", "First paint from a valid, fresh link.", _reset_empty),
            AuthState("reset-rules-unmet", "Rules unmet", "Live password policy checklist with unmet items.", _reset_rules_unmet),
            AuthState("reset-rules-met", "Rules met", "All policy rules satisfied; confirm field still empty.", _reset_rules_met),
            AuthState("reset-not-matching", "Not matching", "Confirm field disagrees with the new password.", _reset_not_matching),
            AuthState("reset-expired-token", "Expired token", "Link older than its TTL; fresh link offered.", _reset_expired_token),
            AuthState("reset-used-token", "Used token", "Reset links are single-use; fresh link offered.", _reset_used_token),
            AuthState("reset-success", "Success", "Password changed; onward to sign-in.", _reset_success),
        ),
    ),
    AuthFlowGroup(
        slug="check-email",
        title="Check email",
        lede="Post-register verification holding page — all three §3 check-email states.",
        states=(
            AuthState("check-email-default", "Default", "Address shown, resend available.", _check_email_default),
            AuthState("check-email-resend-cooldown", "Resend cooldown", "Resend locked with a visible countdown.", _check_email_resend_cooldown),
            AuthState("check-email-resend-failed", "Resend failed", "Last resend errored; retry offered.", _check_email_resend_failed),
        ),
    ),
)


def auth_state_slugs() -> list[str]:
    """Flat ``flow/state`` list — doubles as the §3 completeness checklist."""

    return [f"{group.slug}/{state.slug}" for group in AUTH_FLOW_GROUPS for state in group.states]


# ---------------------------------------------------------------------------
# Page assembly (tokens and scaffold mirror app.dev_preview).
# ---------------------------------------------------------------------------

_PAGE_CSS = """
  :root { color-scheme: light; }
  body { margin: 0; padding: 24px 16px 48px; background-color: #F7F4EE; color: #1C1917;
         font-family: ui-sans-serif, system-ui, sans-serif; }
  a { color: #0F4C46; }
  h1 { font-size: 22px; line-height: 1.2; margin: 0 0 4px; }
  .eyebrow { font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
             color: #0F4C46; margin: 0 0 6px; }
  p.lede { margin: 0 0 20px; color: #5B554E; font-size: 15px; line-height: 23px; max-width: 70ch; }
  .facts { font-size: 13px; color: #5B554E; line-height: 21px; margin: 0 0 26px;
           padding: 10px 12px; background-color: #FEFDFA; border: 1px solid #E5E0D8;
           border-radius: 8px; max-width: 70ch; }
  h2 { font-size: 16px; margin: 34px 0 4px; }
  p.flowlede { margin: 0 0 14px; color: #5B554E; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
          gap: 18px; align-items: start; }
  .statecard { background: transparent; }
  .statemeta { margin: 0 0 8px; }
  .statelabel { font-size: 13px; font-weight: 600; }
  .stateslug { font-family: ui-monospace, monospace; font-size: 11px; color: #5B554E; }
  .statenote { font-size: 12px; color: #5B554E; line-height: 18px; margin: 2px 0 10px;
               max-width: 44ch; }
  .footnote { margin-top: 30px; color: #5B554E; font-size: 12px; line-height: 19px; max-width: 70ch; }
  .mp_spinner { border: 2px solid rgba(254,253,250,.45); border-top-color: #FEFDFA;
                border-radius: 50%; animation: mp_spin 0.9s linear infinite; }
  @keyframes mp_spin { to { transform: rotate(360deg); } }
"""


def _page(title: str, eyebrow: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title><style>{_PAGE_CSS}</style></head>"
        f'<body><main style="max-width:1180px; margin:0 auto;">'
        f'<p class="eyebrow">{escape(eyebrow)}</p><h1>{escape(title)}</h1>'
        f"{body}</main></body></html>"
    )


def dev_states_index_html() -> str:
    """The whole §3 auth-state table as one labelled page."""

    total = sum(len(group.states) for group in AUTH_FLOW_GROUPS)
    sections = []
    for group in AUTH_FLOW_GROUPS:
        cards = []
        for state in group.states:
            cards.append(
                f'<div class="statecard" data-state="{group.slug}/{state.slug}">'
                f'<p class="statemeta"><span class="statelabel">{escape(state.label)}</span> '
                f'<span class="stateslug">— {group.slug}/{state.slug}</span></p>'
                f'<p class="statenote">{escape(state.note)}</p>'
                f"{state.render()}</div>"
            )
        sections.append(
            f'<section id="{group.slug}"><h2>{escape(group.title)}</h2>'
            f'<p class="flowlede">{escape(group.lede)}</p>'
            f'<div class="grid">{"".join(cards)}</div></section>'
        )
    body = (
        f'<p class="lede">Every auth state from the Auth Flow prompt §3 table, on one '
        f"labelled page. Static mocks with dummy data — not the live components; "
        f"styling problems get reported, not fixed here (spec §6).</p>"
        f'<p class="facts">{total} states across {len(AUTH_FLOW_GROUPS)} flows · '
        f"dummy persona <b>Alex / alex@example.com</b> · no database, no auth, no writes · "
        f"gated server-side: <b>404 in production</b>, dev/staging only.</p>"
        f'{"".join(sections)}'
        f'<p class="footnote">Companion surface: <a href="/dev/emails">/dev/emails</a> '
        f"renders the Transactional Email spec §8 family. {_DUMMY_TOKEN_NOTE}.</p>"
    )
    return _page("/dev/states — auth state gallery", "SON-883 · SON-1357 · dev-only", body)
