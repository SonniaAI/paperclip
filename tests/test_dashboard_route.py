from __future__ import annotations

from app.main import (
    _call_stats_payload,
    _duration_label,
    _sentiment_score,
    dashboard_overview,
)


def test_dashboard_overview_route_registered_with_auth_dependency() -> None:
    """The live SPA calls GET /api/dashboard/overview?period=... under a session cookie.

    This guard locks in the SON-418 route on the production auth lineage so the
    dashboard never regresses to a 404 (the live bug this test documents):
    unauthenticated callers must see 401, not 404.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    paths = [r.path for r in app.routes]
    assert "/api/dashboard/overview" in paths

    with TestClient(app, base_url="https://testserver") as client:
        # No session cookie -> authenticated-context dependency rejects first.
        response = client.get("/api/dashboard/overview?period=today")
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated"


def test_dashboard_route_is_declared_get_single_query_period() -> None:
    """The endpoint must accept ?period= without extra mandatory query params."""

    # The FastAPI signature directly reflects the URL contract used by the SPA.
    import inspect

    sig = inspect.signature(dashboard_overview)
    assert "period" in sig.parameters
    assert sig.parameters["period"].default == "today"


def test_duration_label_formats_seconds() -> None:
    assert _duration_label(None) == "0m"
    assert _duration_label(0) == "0m"
    assert _duration_label(90) == "1m"
    assert _duration_label(7200) == "2h 0m"
    assert _duration_label(3661) == "1h 1m"


def test_sentiment_score_maps_labels_to_float() -> None:
    assert _sentiment_score("positive") == 0.55
    assert _sentiment_score("neutral") == 0.0
    assert _sentiment_score("negative") == -0.55
    assert _sentiment_score(None) is None
    assert _sentiment_score("") is None


def test_call_stats_payload_is_an_async_callable_accepting_period_dates() -> None:
    """Regression guard: _call_stats_payload must keep its start/end date kwargs,
    since the dashboard route and /api/calls/stats-family share it."""
    import inspect

    sig = inspect.signature(_call_stats_payload)
    params = sig.parameters
    assert "start_date" in params
    assert "end_date" in params
    # start_date/end_date are keyword-only and optional
    assert params["start_date"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["start_date"].default is None
