"""SON-877: password policy, branded email templates, phone E.164 (unit)."""

from __future__ import annotations

import asyncio

import pytest

from app.email_templates import (
    action_url_for_token,
    render_password_changed_email,
    render_reset_email,
    render_verification_email,
)
from app.password_policy import (
    PasswordCheckContext,
    common_password_violations,
    composition_violations,
    password_violations,
    pattern_violations,
    personal_violations,
)
from app.schemas import _normalise_phone


def test_composition_violations_are_affirmative_and_complete() -> None:
    violations = composition_violations("SHORT1")
    assert any("uppercase letter" in v for v in violations) is False
    assert any("lowercase letter" in v for v in violations)
    assert any("number" in v for v in violations) is False
    assert any("special character" in v for v in violations)
    assert composition_violations("Harbour-42!lantern") == []


def test_common_password_rejected_with_substitutions() -> None:
    assert common_password_violations("password")[0].startswith("Choose a password")
    assert common_password_violations("p@ssw0rd")  # leetspeak normalisation
    assert common_password_violations("Password1!")  # embedded common entry
    assert common_password_violations("Sonnia-Rocks-2026")  # brand string


def test_personal_information_rejected() -> None:
    context = PasswordCheckContext(
        display_name="Jane Harbour",
        email="jane.harbour@example.com",
        company_name="Harbour & Co",
    )
    assert personal_violations("Jane-Harbour-42!", context)
    assert personal_violations("janeharbour42", context)
    assert personal_violations("HarbourCo-2026", context)
    assert personal_violations("Unrelated-Pass-42", context) == []


def test_patterns_rejected() -> None:
    assert pattern_violations("qwerty12345")
    assert pattern_violations("AAAAAAAA")
    assert pattern_violations("abcdefgh")
    assert pattern_violations("Harbour-42!lantern") == []


@pytest.mark.asyncio
async def test_password_violations_accept_strong_password() -> None:
    violations = await password_violations(
        "Candlelight-42!Lantern",
        PasswordCheckContext(display_name="Jane Smith", email="jane@example.com"),
        check_hibp=False,
    )
    assert violations == []


@pytest.mark.asyncio
async def test_password_violations_reject_common_with_hibp_fail_open() -> None:
    # The top-10k check runs locally; HIBP is skipped (fail open) so the test
    # never depends on the network.
    violations = await password_violations("Password1!", check_hibp=False)
    assert any("common" in v for v in violations)


def test_phone_normalised_to_e164_via_libphonenumber() -> None:
    assert _normalise_phone("+65 8123 4567") == "+6581234567"
    assert _normalise_phone("+44 7911 123456") == "+447911123456"
    with pytest.raises(ValueError):
        _normalise_phone("+65 123")  # not a valid number
    with pytest.raises(ValueError):
        _normalise_phone("8123 4567")  # no country code


def test_action_url_is_https_single_anchor() -> None:
    url = action_url_for_token("/reset-password/", "abc.123")
    assert url.startswith("https://manager.sonnia.ai/reset-password/abc.123")
    assert url.count("#") == 0
    with pytest.raises(ValueError):
        action_url_for_token("/reset-password/", "has space")
    with pytest.raises(ValueError):
        action_url_for_token("/reset-password/", "bad?query=1")


def test_reset_email_branded_slots() -> None:
    rendered = render_reset_email(first_name="Jane", url="https://manager.sonnia.ai/reset-password/abc")
    assert "SECURITY · RESET" in rendered.html
    assert "let&#x27;s get you back in" in rendered.html
    assert "works for one hour" in rendered.html
    assert "ignore this email" in rendered.html
    assert 'lang="en-GB"' in rendered.html
    assert "Reset password" in rendered.plain_text


def test_password_changed_email_branded_slots() -> None:
    rendered = render_password_changed_email(
        first_name="Jane", reset_url="https://manager.sonnia.ai/forgot-password"
    )
    assert "SECURITY · CHANGED" in rendered.html
    assert "your password was changed" in rendered.html
    assert "Wasn&#x27;t you?" in rendered.html
    assert "signed out" in rendered.html
    assert "SECURITY · CHANGED" in rendered.plain_text


def test_verification_email_branded_slots() -> None:
    rendered = render_verification_email(
        first_name="Jane", url="https://manager.sonnia.ai/verify-email/abc"
    )
    assert "NEW ACCOUNT · VERIFY" in rendered.html
    assert "one click and you&#x27;re in" in rendered.html
    assert "works for 24 hours" in rendered.html
    assert "ACCOUNT · PENDING" in rendered.html


def test_rendered_email_under_102kb() -> None:
    for renderer in (
        lambda: render_verification_email(first_name="Jane", url="https://manager.sonnia.ai/verify-email/abc"),
        lambda: render_reset_email(first_name="Jane", url="https://manager.sonnia.ai/reset-password/abc"),
        lambda: render_password_changed_email(
            first_name="Jane", reset_url="https://manager.sonnia.ai/forgot-password"
        ),
    ):
        rendered = renderer()
        assert len(rendered.html.encode("utf-8")) < 102 * 1024
        assert len(rendered.plain_text.encode("utf-8")) < 102 * 1024


def test_rendered_email_message_roundtrip() -> None:
    rendered = render_reset_email(first_name="Jane", url="https://manager.sonnia.ai/reset-password/abc")
    message = rendered.as_message(
        to="jane@example.com", from_address="sonnia@sonnia.ai", subject="Reset your Sonnia password"
    )
    assert message["Subject"] == "Reset your Sonnia password"
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    assert "SECURITY · RESET" in html_part.get_content()


def test_password_violations_are_awaitable_and_ordered() -> None:
    # Guards against the async/await contract regressing.
    async def run() -> list[str]:
        return await password_violations("password", check_hibp=False)

    violations = asyncio.run(run())
    assert violations  # composition + common list both fire
