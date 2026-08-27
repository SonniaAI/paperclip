"""Tests for the dev-only auth-state gallery (SON-883 / SON-1357).

Covers the non-negotiables from the spec:
- server-side gating: production answers 404 (never 403, never a login wall)
- every Auth Flow prompt §3 state appears, labelled, on one page
- dummy data only — the sample persona, never a real user's details
- no auth/roles on the route, and the surface stays out of the public schema
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.dev_preview as dev_preview_module
from app.config import Settings
from app.dev_states import AUTH_FLOW_GROUPS, auth_state_slugs

# The §3 table, exactly as specified. Counts are asserted per flow so an
# accidentally dropped state fails loudly instead of silently shrinking the
# gallery.
_SECTION3_EXPECTATION: dict[str, set[str]] = {
    "register": {
        "register-empty",
        "register-validating",
        "register-field-errors",
        "register-server-error",
        "register-submitting",
        "register-success",
    },
    "login": {
        "login-empty",
        "login-wrong-credentials",
        "login-unverified",
        "login-rate-limited",
        "login-2fa",
    },
    "forgot": {
        "forgot-empty",
        "forgot-submitting",
        "forgot-confirmation-sent",
    },
    "reset": {
        "reset-empty",
        "reset-rules-unmet",
        "reset-rules-met",
        "reset-not-matching",
        "reset-expired-token",
        "reset-used-token",
        "reset-success",
    },
    "check-email": {
        "check-email-default",
        "check-email-resend-cooldown",
        "check-email-resend-failed",
    },
}


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


def test_production_gating_answers_404_on_dev_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production_like_settings(monkeypatch)
    from app.main import app

    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        response = client.get("/dev/states")
        assert response.status_code == 404
        assert response.status_code not in {401, 403}


def test_non_production_renders_the_gallery() -> None:
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/dev/states")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "auth state gallery" in response.text


def test_dev_states_route_has_no_auth_dependencies() -> None:
    from fastapi.routing import APIRoute

    from app.main import app

    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/dev/states"
    ]
    assert routes, "/dev/states must be registered"
    for route in routes:
        dependencies = [getattr(dep.dependency, "__name__", "") for dep in route.dependencies]
        assert dependencies == ["_require_non_production"], route.path
        assert all("auth" not in name.lower() for name in dependencies), route.path


def test_dev_states_hidden_from_public_schema() -> None:
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert "/dev/states" not in schema.json().get("paths", {})


# ---------------------------------------------------------------------------
# §3 completeness — every state, labelled, on one page
# ---------------------------------------------------------------------------


def test_gallery_matches_the_section3_table_exactly() -> None:
    by_flow: dict[str, set[str]] = {}
    for slug in auth_state_slugs():
        flow, state = slug.split("/", 1)
        by_flow.setdefault(flow, set()).add(state)
    assert by_flow == _SECTION3_EXPECTATION
    assert sum(len(states) for states in _SECTION3_EXPECTATION.values()) == 24


def test_every_state_renders_labelled_on_the_page() -> None:
    from app.dev_states import dev_states_index_html

    page = dev_states_index_html()
    for group in AUTH_FLOW_GROUPS:
        assert f'id="{group.slug}"' in page, group.slug
        for state in group.states:
            assert f'data-state="{group.slug}/{state.slug}"' in page
            assert state.label in page
            assert state.note in page


def test_page_declares_scope_and_gating_up_front() -> None:
    from app.dev_states import dev_states_index_html

    page = dev_states_index_html()
    for marker in (
        "Auth Flow prompt §3",
        "404 in production",
        "24 states across 5 flows",
        "/dev/emails",
    ):
        assert marker in page, marker


# ---------------------------------------------------------------------------
# Dummy data only (non-negotiable: never a real user's email/name/token)
# ---------------------------------------------------------------------------


def test_gallery_uses_only_the_dummy_persona() -> None:
    from app.dev_states import dev_states_index_html

    page = dev_states_index_html()
    assert "alex@example.com" in page  # the dummy persona, visibly fake
    assert "sample-preview-token" not in page  # no tokens at all, even dummies
    # No real sender/production addresses rendered as recipient data.
    assert "no-reply@sonnia.ai" not in page
    assert "manager.sonnia.ai" not in page
