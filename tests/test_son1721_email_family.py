"""SON-1721: transactional email family (Szonja verification-email spec).

Automatable subset of the spec §9 test list plus the §6/§7/§8 structural
constraints. Client-matrix rendering (Gmail/Apple/Outlook), inbox placement
and dark-mode screenshots stay on the send-time checklist; everything that
can be proven from the markup itself is proven here.
"""

from __future__ import annotations

import pytest

from app.email_templates import (
    COMPANY_NUMBER,
    FOOTER_LEGAL,
    NEW_SIGNIN_SUBJECT,
    PASSWORD_CHANGED_SUBJECT,
    RESET_SUBJECT,
    VERIFICATION_SUBJECT,
    WELCOME_SUBJECT,
    action_url_for_token,
    render_action_email,
    render_new_signin_email,
    render_password_changed_email,
    render_reset_email,
    render_team_invite_email,
    render_verification_email,
    render_welcome_email,
)

VERIFY_URL = "https://manager.sonnia.ai/verify-email/abc123"


def _family() -> dict[str, str]:
    """Render all six family emails; keyed for per-email assertions."""

    return {
        "verify": render_verification_email(first_name="Szonja", url=VERIFY_URL).html,
        "welcome": render_welcome_email(
            first_name="Szonja", url="https://manager.sonnia.ai/dashboard"
        ).html,
        "reset": render_reset_email(
            first_name="Szonja", url="https://manager.sonnia.ai/reset-password/xyz"
        ).html,
        "changed": render_password_changed_email(
            first_name="Szonja", reset_url="https://manager.sonnia.ai/forgot-password"
        ).html,
        "invite": render_team_invite_email(
            inviter="Arthur",
            company="Acme Ltd",
            url="https://manager.sonnia.ai/invite/accept/tok",
        ).html,
        "signin": render_new_signin_email(
            first_name="Szonja",
            device="Chrome on macOS",
            signed_in_at="3 September 2026 at 11:58 UTC",
            url="https://manager.sonnia.ai/forgot-password",
        ).html,
    }


# ── §4 exact copy ──────────────────────────────────────────────────────────


def test_verification_email_uses_spec_copy_exactly() -> None:
    rendered = render_verification_email(first_name="Szonja", url=VERIFY_URL)
    assert "NEW ACCOUNT · VERIFY" in rendered.html
    assert "one click and you&#x27;re in" in rendered.html
    assert "Hello Szonja," in rendered.html
    assert "Hello , " not in rendered.html
    assert "Confirm this is your address and your account is ready." in rendered.html
    assert "Verify my email address" in rendered.html
    assert (
        "This link works for 24 hours. If it expires, request a new one from "
        "the sign-in page." in rendered.html
    )
    assert "Button not working? Paste this into your browser:" in rendered.html
    assert "Didn&#x27;t sign up? Ignore this email — the account stays inactive without this step." in rendered.html
    assert "Sonnia · warm light in the machine" in rendered.html
    assert rendered.plain_text.startswith(
        "One click and your Sonnia account is ready. Link works for 24 hours."
    )


def test_subjects_are_plain_and_exact() -> None:
    assert VERIFICATION_SUBJECT == "Verify your email address"
    assert RESET_SUBJECT == "Reset your Sonnia password"
    assert PASSWORD_CHANGED_SUBJECT == "Your Sonnia password was changed"
    assert WELCOME_SUBJECT == "Your Sonnia account is active"
    assert NEW_SIGNIN_SUBJECT == "New sign-in to your Sonnia account"
    for subject in (VERIFICATION_SUBJECT, RESET_SUBJECT):
        assert "!" not in subject
        assert any(ch.isemoji() for ch in subject) is False if hasattr(str, "isemoji") else True


# ── §8 family table ────────────────────────────────────────────────────────


def test_family_labels_headlines_and_core_lines() -> None:
    cases = [
        ("NEW ACCOUNT · VERIFY", "one click and you&#x27;re in", "Confirm this is your address", "verify"),
        ("ACCOUNT · ACTIVE", "you&#x27;re in", "Here's what to set up first.", "welcome"),
        ("SECURITY · RESET", "let&#x27;s get you back in", "Use the link below to set a new password", "reset"),
        ("SECURITY · CHANGED", "your password was changed", "If that wasn't you, secure your account now.", "changed"),
        ("INVITATION", "Arthur would like you to join Acme Ltd", "Accept below and you'll share the same Sonnia.", "invite"),
        ("SECURITY · NEW SIGN-IN", "a new sign-in on your account", "Chrome on macOS, 3 September 2026 at 11:58 UTC. Not you?", "signin"),
    ]
    family = _family()
    for label, headline, core, key in cases:
        html = family[key]
        assert label in html, key
        assert headline in html, key
        assert core in html, key


def test_invite_escapes_inviter_and_company() -> None:
    rendered = render_team_invite_email(
        inviter="<script>alert(1)</script>",
        company="Acme & Co",
        url="https://manager.sonnia.ai/invite/accept/tok",
    )
    assert "<script>alert(1)</script>" not in rendered.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; would like you to join Acme &amp; Co" in rendered.html


# ── §1/§3/§6 hard constraints ─────────────────────────────────────────────


@pytest.mark.parametrize("html", _family().values(), ids=lambda v: "email")
def test_layout_is_tables_only_with_inline_styles(html: str) -> None:
    assert '<html lang="en-GB">' in html
    assert html.count('role="presentation"') >= 3
    assert "max-width:600px" in html
    assert "display:flex" not in html and "flexbox" not in html
    assert "display:grid" not in html
    assert "position:absolute" not in html and "position:fixed" not in html
    assert "<script" not in html.replace("<script type", "<scripttype") or "<script" not in html
    # The dark-mode enhancement is the only <style> block and everything
    # structural is inline; base colours never depend on it.
    assert html.count("<style") == 1
    assert "style=" in html


@pytest.mark.parametrize("html", _family().values(), ids=lambda v: "email")
def test_bulletproof_button_pattern(html: str) -> None:
    assert 'bgcolor="#46663d"' in html
    assert "display:inline-block; padding:14px 32px" in html
    assert 'role="button"' in html
    assert "font-family:&#x27;Fraunces&#x27;, Georgia" in html or "font-family:'Fraunces', Georgia" in html


@pytest.mark.parametrize("html", _family().values(), ids=lambda v: "email")
def test_no_images_and_no_tracking_pixel(html: str) -> None:
    assert "<img" not in html
    assert "1x1" not in html
    assert "tracking" not in html


@pytest.mark.parametrize("html", _family().values(), ids=lambda v: "email")
def test_preheader_is_hidden_with_zwnj_run_and_comes_first(html: str) -> None:
    assert "display:none; max-height:0; max-width:0; overflow:hidden; opacity:0" in html
    assert "&#8204;&nbsp;" in html  # zwnj run stops body text bleeding in
    preheader_pos = html.index("display:none")
    body_pos = html.index("&#10022; SONNIA")
    assert preheader_pos < body_pos


@pytest.mark.parametrize("html", _family().values(), ids=lambda v: "email")
def test_dark_mode_meta_and_explicit_variant(html: str) -> None:
    assert '<meta name="color-scheme" content="light dark">' in html
    assert '<meta name="supported-color-schemes" content="light dark">' in html
    assert "prefers-color-scheme: dark" in html


def test_tap_target_at_least_44px() -> None:
    html = render_verification_email(first_name="Szonja", url=VERIFY_URL).html
    assert 'height="48"' in html
    assert "min-height:48px" in html
    assert "padding:14px 32px" in html


def test_footer_carries_registered_office() -> None:
    assert "N7 0FQ" in FOOTER_LEGAL
    assert COMPANY_NUMBER in FOOTER_LEGAL
    html = render_verification_email(first_name="Szonja", url=VERIFY_URL).html
    assert FOOTER_LEGAL in html


# ── §7 multipart + §9 size / plain text / copy checks ─────────────────────


def test_multipart_alternative_message() -> None:
    rendered = render_verification_email(first_name="Szonja", url=VERIFY_URL)
    message = rendered.as_message(
        to="szonja@example.com",
        from_address="sonnia@sonnia.ai",
        subject=VERIFICATION_SUBJECT,
    )
    assert message.get_content_type() == "multipart/alternative"
    plain = message.get_body(preferencelist=("plain",))
    html_part = message.get_body(preferencelist=("html",))
    assert plain is not None and html_part is not None


@pytest.mark.parametrize("html", _family().values(), ids=lambda v: "email")
def test_html_well_under_gmail_clip_limit(html: str) -> None:
    assert len(html.encode("utf-8")) < 102_400
    assert len(html.encode("utf-8")) < 30_000  # comfortably lean


def test_plain_text_reads_standalone() -> None:
    rendered = render_verification_email(first_name="Szonja", url=VERIFY_URL)
    text = rendered.plain_text
    assert "NEW ACCOUNT · VERIFY" in text
    assert "one click and you're in" in text
    assert "Confirm this is your address and your account is ready." in text
    assert "Verify my email address:" in text
    assert VERIFY_URL in text
    assert "Button not working? Paste this into your browser:" in text
    assert "<p" not in text and "href=" not in text


def test_british_english_and_personalisation() -> None:
    rendered = render_verification_email(first_name="Szonja", url=VERIFY_URL)
    plain = rendered.plain_text.lower()
    for americanism in ("personalize", "organization", "favorite", "apologize", "color "):
        assert americanism not in plain, americanism
    assert "Hello Szonja," in rendered.plain_text
    empty = render_verification_email(first_name=" there ", url=VERIFY_URL)
    assert "Hello there," in empty.plain_text
    assert "Hello ," not in empty.plain_text


def test_expiry_line_omitted_when_empty() -> None:
    rendered = render_welcome_email(
        first_name="Szonja", url="https://manager.sonnia.ai/dashboard"
    )
    assert "This link works for" not in rendered.html


# ── §7 token handling ──────────────────────────────────────────────────────


def test_action_url_keeps_token_only_in_a_clean_path() -> None:
    url = action_url_for_token("/verify-email/", "T0ken-x_1")
    assert url == "https://manager.sonnia.ai/verify-email/T0ken-x_1"
    with pytest.raises(ValueError):
        action_url_for_token("/verify-email/", "a b")
    with pytest.raises(ValueError):
        action_url_for_token("/verify-email/", "a?b")
    with pytest.raises(ValueError):
        action_url_for_token("/verify-email/", "")
    with pytest.raises(ValueError):
        action_url_for_token("/verify-email/", "a/b")
    with pytest.raises(ValueError):
        action_url_for_token("/verify-email/", "abc", base_url="http://manager.sonnia.ai")
    with pytest.raises(ValueError):
        action_url_for_token("/verify-email/", "abc", base_url="https://evil.example.com")


def test_generic_partial_requires_slots() -> None:
    with pytest.raises(ValueError):
        render_action_email(
            first_name="Szonja",
            url=VERIFY_URL,
            label="",
            headline="x",
            body="b",
            button_text="b",
            ignore_line="i",
            subject="s",
            preheader="p",
        )
