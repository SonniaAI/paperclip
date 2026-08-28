"""Tests for the dev-only /dev/emails preview surface (SON-883 / SON-1355).

Locks in the two behaviours the issue calls non-negotiable:
- server-side gating: production answers 404 (never 403, never a login wall)
- honest §8 inventory: built templates render through production builders
  with dummy data; unbuilt ones are listed and marked NOT BUILT.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.dev_preview as dev_preview_module
from app.config import Settings
from app.dev_preview import (
    DEV_EMAIL_TEMPLATES,
    count_remote_images,
    dev_emails_index_html,
    find_dev_email_template,
    format_html_size,
    html_with_images_blocked,
    is_production_environment,
    render_dummy_email,
    rendered_html_size_bytes,
)

_FAMILY_SLUGS = {
    "verify-email",
    "welcome",
    "password-reset",
    "password-changed",
    "team-invite",
    "new-sign-in",
}
_BUILT_SLUGS = {"verify-email", "password-reset", "password-changed"}


def _production_like_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force get_settings() to a valid production Settings for gating tests."""

    production = Settings(
        environment="production",
        session_secret="production-test-secret-value",
        public_app_url="https://manager.sonnia.ai",
        smtp_host="smtp.sonnia.ai",
        smtp_from_email="no-reply@sonnia.ai",
        smtp_username="unit-test",
        smtp_password="unit-test-password",
    )
    monkeypatch.setattr(dev_preview_module, "get_settings", lambda: production)


# ---------------------------------------------------------------------------
# Gating (non-negotiable: production 404s, no auth added anywhere)
# ---------------------------------------------------------------------------


def test_production_gating_answers_404_on_every_dev_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production_like_settings(monkeypatch)
    from app.main import app

    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        for path in (
            "/dev/emails",
            "/dev/emails/verify-email",
            "/dev/emails/verify-email/raw",
            "/dev/emails/not-a-template",
        ):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.status_code not in {401, 403}, path


def test_is_production_environment_reads_live_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not is_production_environment()
    _production_like_settings(monkeypatch)
    assert is_production_environment()


def test_no_route_under_dev_requires_authentication() -> None:
    from fastapi.routing import APIRoute

    from app.main import app

    dev_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/dev/")
    ]
    assert dev_routes, "dev preview routes must be registered"
    for route in dev_routes:
        dependencies = [getattr(dep.dependency, "__name__", "") for dep in route.dependencies]
        assert all("auth" not in name.lower() for name in dependencies), route.path


# ---------------------------------------------------------------------------
# Inventory honesty (§8 family, unbuilt marked NOT BUILT)
# ---------------------------------------------------------------------------


def test_inventory_covers_full_eight_family() -> None:
    slugs = {template.slug for template in DEV_EMAIL_TEMPLATES}
    assert slugs == _FAMILY_SLUGS
    built = {t.slug for t in DEV_EMAIL_TEMPLATES if t.built}
    assert built == _BUILT_SLUGS
    for template in DEV_EMAIL_TEMPLATES:
        if template.built:
            assert template.renderer is not None, template.slug
        else:
            assert template.renderer is None, template.slug
            assert "(unbuilt)" in template.subject or template.subject.startswith("("), (
                template.slug
            )


def test_unbuilt_templates_are_marked_honestly_in_html_and_api() -> None:
    html = dev_emails_index_html()
    assert "NOT BUILT" in html
    for slug in _FAMILY_SLUGS - _BUILT_SLUGS:
        assert slug in html

    unbuilt = find_dev_email_template("welcome")
    assert unbuilt is not None and not unbuilt.built

    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        raw = client.get("/dev/emails/welcome/raw")
        assert raw.status_code == 404
        assert "SON-877" in raw.text


def test_unknown_slug_returns_404() -> None:
    assert find_dev_email_template("not-a-template") is None

    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/dev/emails/not-a-template").status_code == 404
        assert client.get("/dev/emails/not-a-template/raw").status_code == 404


# ---------------------------------------------------------------------------
# Rendering fidelity (dummy data only, real builders, sizes reported honestly)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(_BUILT_SLUGS))
def test_built_templates_render_deterministic_dummy_data(slug: str) -> None:
    template = find_dev_email_template(slug)
    assert template is not None
    first = render_dummy_email(template)
    second = render_dummy_email(template)
    assert first.html == second.html
    assert first.plain_text == second.plain_text

    # Plausible dummy persona only — never live user identifiers.
    assert "Alex" in first.plain_text
    assert "sample-preview-token" in first.html
    # Action links use the production URL shape via action_url_for_token.
    assert "https://manager.sonnia.ai/" in first.html


def test_reported_size_matches_actual_html_bytes() -> None:
    template = find_dev_email_template("verify-email")
    assert template is not None
    rendered = render_dummy_email(template)
    expected_kb = rendered_html_size_bytes(rendered) / 1024
    formatted = format_html_size(rendered)
    assert f"{expected_kb:.1f} KB" in formatted
    assert f"{rendered_html_size_bytes(rendered):,} bytes" in formatted


def test_plain_text_alternative_is_readable_content() -> None:
    slug = "password-reset"
    template = find_dev_email_template(slug)
    assert template is not None and slug in _BUILT_SLUGS
    rendered = render_dummy_email(template)
    assert "Reset password" in rendered.plain_text
    assert rendered.plain_text.strip().startswith(("A secure action", "SECURITY")) or (
        "reset" in rendered.plain_text.lower()
    )


# ---------------------------------------------------------------------------
# Toggles: images blocked stays usable, size/themes surfaced on the viewer
# ---------------------------------------------------------------------------


def test_images_blocked_replaces_remote_sources_with_placeholders() -> None:
    synthetic = (
        '<p>Hi</p><img src="https://cdn.example.com/logo.png" alt="logo">'
        '<img src="/local/asset.png" alt="local">'
    )
    assert count_remote_images(synthetic) == 1
    blocked = html_with_images_blocked(synthetic)
    assert "<img" not in blocked
    assert "[image blocked in preview]" in blocked
    assert "https://cdn.example.com/logo.png" in blocked

    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/dev/emails/password-reset/raw?images=off")
        assert response.status_code == 200
        assert "image blocked in preview" in response.text or "<img" not in response.text


def test_viewer_page_surfaces_toggles_and_facts() -> None:
    template = find_dev_email_template("verify-email")
    assert template is not None
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        viewer = client.get("/dev/emails/verify-email?dark=1&width=375&images=off&mode=text")
        assert viewer.status_code == 200
        body: str = viewer.text
        assert "rendered HTML size:" in body
        assert "remote images referenced:" in body
        assert "Mobile ~375px" in body
        assert "Plain-text view" in body
        assert "FORCED-DARK SIMULATION" in body


def test_mobile_width_param_is_validated() -> None:
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        too_narrow = client.get("/dev/emails/verify-email?width=100")
        assert too_narrow.status_code == 422


def test_raw_serves_html_by_default_and_text_when_requested() -> None:
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        html_response = client.get("/dev/emails/password-changed/raw")
        assert html_response.status_code == 200
        assert "text/html" in html_response.headers["content-type"]
        assert html_response.text.lstrip().startswith("<!doctype html>")

        text_response = client.get("/dev/emails/password-changed/raw?mode=text")
        assert text_response.status_code == 200
        assert "text/plain" in text_response.headers["content-type"]
        assert "your password was changed" in text_response.text.lower()


def test_registry_lookup_helpers_match_public_table() -> None:
    for template in DEV_EMAIL_TEMPLATES:
        assert find_dev_email_template(template.slug) is template
    unknown: Any = None
    assert find_dev_email_template("nope") is unknown
