"""Contract tests for SON-1529 campaign schedule + tone persistence (SON-1493).

Database-free: payload validation, the schedule/tone column mapping, the
response truth blocks, route registration, and the migration revision wiring.
Live SQL round-trip semantics run against the staging leg (SON-1529 receipts).
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.phase1_contracts import (
    CampaignCreatePayload,
    CampaignPatchPayload,
    _campaign_response,
    _normalise_active_days,
    _parse_wall_clock,
    _schedule_tone_columns,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _paths() -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def _methods(path: str) -> set[str]:
    found = set()
    for route in app.routes:
        if getattr(route, "path", "") == path:
            found.update(getattr(route, "methods", set()) or set())
    return found


def _base_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Solar follow-ups",
        "objective": "Book site assessments",
        "target_ids": [str(uuid4())],
    }
    payload.update(overrides)
    return payload


def test_campaign_patch_route_is_registered() -> None:
    """Included routers materialise lazily (starlette ≥1.6) — probe by request."""

    base = f"/api/campaigns/{uuid4()}"
    with TestClient(app, base_url="https://testserver") as client:
        # Routed to the authenticated context, not a 404 miss.
        assert client.patch(base, json={"timezone": "UTC"}).status_code in {401, 403}
        assert client.patch(base, json={}).status_code in {401, 403}
        assert client.put(base, json={}).status_code == 405
        assert client.delete(base).status_code == 405
        assert client.patch(f"{base}/launch", json={}).status_code == 405


def test_wrong_verbs_hit_method_guards() -> None:
    base = f"/api/campaigns/{uuid4()}"
    with TestClient(app, base_url="https://testserver") as client:
        assert client.put(base, json={}).status_code == 405
        assert client.patch(f"{base}/launch", json={}).status_code == 405


def test_create_payload_accepts_schedule_and_tone() -> None:
    payload = CampaignCreatePayload(
        **_base_payload(  # type: ignore[arg-type]
            timezone="Asia/Singapore",
            business_hours_start="09:00",
            business_hours_end="17:00",
            call_window_days="1,3,5",
            max_calls_per_day=40,
            max_attempts_per_lead=4,
            retry_minutes_voicemail=60,
            retry_minutes_no_answer=60,
            max_total_calls=500,
            tone_formality=1,
            tone_pace=-1,
            tone_persistence=1,
            tone_warmth=2,
            tone_depth=2,
        )
    )
    columns = _schedule_tone_columns(payload)
    assert columns["schedule_timezone"] == "Asia/Singapore"
    assert columns["call_window_start"] == time(9, 0)
    assert columns["call_window_end"] == time(17, 0)
    assert columns["active_days"] == ["mon", "wed", "fri"]
    assert columns["daily_call_cap"] == 40
    assert columns["max_attempts_per_lead"] == 4
    assert columns["retry_delay_minutes"] == 60
    assert columns["max_total_calls"] == 500
    assert columns["tone_formality"] == 1
    assert columns["tone_pace"] == -1
    assert columns["tone_persistence"] == 1
    assert columns["tone_warmth"] == 2
    assert columns["tone_depth"] == 2


def test_absent_schedule_fields_are_not_written() -> None:
    payload = CampaignCreatePayload(**_base_payload())  # type: ignore[arg-type]
    assert _schedule_tone_columns(payload) == {}


def test_retry_minutes_must_unify() -> None:
    with pytest.raises(ValidationError):
        CampaignCreatePayload(
            **_base_payload(retry_minutes_voicemail=60, retry_minutes_no_answer=90)  # type: ignore[arg-type]
        )
    # A single side alone is fine and unifies.
    payload = CampaignPatchPayload(retry_minutes_no_answer=90)
    assert _schedule_tone_columns(payload) == {"retry_delay_minutes": 90}


def test_call_window_days_validation() -> None:
    assert _normalise_active_days("1,3,3,5") == ["mon", "wed", "fri"]
    assert _normalise_active_days([2, 7]) == ["tue", "sun"]
    assert _normalise_active_days("") is None
    assert _normalise_active_days(None) is None
    with pytest.raises(ValidationError):
        CampaignPatchPayload(call_window_days="1,9")
    with pytest.raises(ValidationError):
        CampaignPatchPayload(call_window_days=[0])
    with pytest.raises(ValidationError):
        CampaignPatchPayload(call_window_days="mon")


def test_timezone_validation() -> None:
    payload = CampaignPatchPayload(timezone="Asia/Singapore")
    assert _schedule_tone_columns(payload) == {"schedule_timezone": "Asia/Singapore"}
    with pytest.raises(ValidationError):
        CampaignPatchPayload(timezone="Mars/Olympus_Mons")


def test_wall_clock_validation() -> None:
    assert _parse_wall_clock("09:00") == time(9, 0)
    assert _parse_wall_clock("17:30:00") == time(17, 30)
    assert _parse_wall_clock(None) is None
    assert _parse_wall_clock("") is None
    with pytest.raises(ValueError):
        _parse_wall_clock("25:00")
    with pytest.raises(ValueError):
        _parse_wall_clock("later")


def test_tone_dial_ranges() -> None:
    ok = CampaignPatchPayload(
        tone_formality=-2,
        tone_pace=2,
        tone_persistence=0,
        tone_warmth=1,
        tone_depth=2,
    )
    columns = _schedule_tone_columns(ok)
    assert columns["tone_formality"] == -2
    assert columns["tone_depth"] == 2
    with pytest.raises(ValidationError):
        CampaignPatchPayload(tone_persistence=2)
    with pytest.raises(ValidationError):
        CampaignPatchPayload(tone_warmth=-3)
    with pytest.raises(ValidationError):
        CampaignCreatePayload(**_base_payload(tone_formality=3))  # type: ignore[arg-type]


def test_patch_null_preserving_semantics() -> None:
    # Explicit null clears…
    clearing = CampaignPatchPayload(timezone=None, max_calls_per_day=None)
    columns = _schedule_tone_columns(clearing)
    assert columns["schedule_timezone"] is None
    assert columns["daily_call_cap"] is None
    # …absent keys stay untouched.
    assert _schedule_tone_columns(CampaignPatchPayload(max_calls_per_day=12)) == {
        "daily_call_cap": 12
    }
    assert _schedule_tone_columns(CampaignPatchPayload()) == {}


def test_campaign_response_truth_blocks() -> None:
    row = {
        "id": uuid4(),
        "name": "Solar follow-ups",
        "objective": "Book site assessments",
        "status": "draft",
        "target_ids": [],
        "created_at": datetime(2026, 8, 29, tzinfo=UTC),
        "launched_at": None,
        "updated_at": datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        "schedule_timezone": "Asia/Singapore",
        "call_window_start": time(9, 0),
        "call_window_end": time(17, 0),
        "active_days": ["mon", "tue"],
        "max_attempts_per_lead": None,
        "retry_delay_minutes": 90,
        "daily_call_cap": None,
        "max_total_calls": 100,
        "tone_formality": 1,
        "tone_pace": None,
        "tone_persistence": 1,
        "tone_warmth": None,
        "tone_depth": None,
        "total_calls": 0,
    }
    response = _campaign_response(row)
    # Flat echo fields (the form's legacy contract).
    assert response["timezone"] == "Asia/Singapore"
    assert response["business_hours_start"] == "09:00"
    assert response["business_hours_end"] == "17:00"
    assert response["call_window_days"] == "1,2"
    assert response["max_calls_per_day"] is None
    assert response["max_attempts_per_lead"] is None
    assert response["retry_minutes_voicemail"] == 90
    assert response["retry_minutes_no_answer"] == 90
    assert response["max_total_calls"] == 100
    assert response["updated_at"] is not None
    # Truth blocks with configured/value/resolved.
    schedule = response["schedule"]
    assert schedule["timezone"] == {
        "configured": True,
        "value": "Asia/Singapore",
        "resolved": "Asia/Singapore",
    }
    assert schedule["max_attempts_per_lead"]["configured"] is False
    assert schedule["max_attempts_per_lead"]["resolved"] == 3
    assert schedule["retry_delay_minutes"]["resolved"] == 90
    assert schedule["daily_call_cap"]["resolved"] is None
    assert schedule["active_days"]["value"] == ["mon", "tue"]
    tone = response["tone"]
    assert tone["formality"] == {"configured": True, "value": 1, "resolved": 1}
    assert tone["pace"]["configured"] is False
    assert tone["depth"]["resolved"] is None


def test_campaign_response_resolves_defaults_when_unset() -> None:
    row = {
        "id": uuid4(),
        "name": "Bare campaign",
        "objective": "Objective",
        "status": "draft",
        "target_ids": [],
        "created_at": datetime(2026, 8, 29, tzinfo=UTC),
        "launched_at": None,
        "updated_at": None,
        "total_calls": 0,
    }
    response = _campaign_response(row)
    assert response["schedule"]["timezone"]["resolved"] == "UTC"
    assert response["schedule"]["max_attempts_per_lead"]["resolved"] == 3
    assert response["schedule"]["retry_delay_minutes"]["resolved"] == 30
    assert response["schedule"]["call_window_start"]["resolved"] is None
    assert response["tone"]["formality"]["configured"] is False
    assert response["timezone"] is None
    assert response["call_window_days"] is None


def test_migration_revision_wiring() -> None:
    module_path = REPO_ROOT / "alembic" / "versions" / "20260829_son1529_campaign.py"
    spec = importlib.util.spec_from_file_location("son1529_migration", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "20260829_son1529_campaign"
    # prod alembic_version is still varchar(32) — keep revision ids within it.
    assert len(module.revision) <= 32
    assert module.down_revision == "20260820_son877_auth_second_pass"
    sql = (REPO_ROOT / "alembic" / "versions" / "20260829_son1529_campaign.sql").read_text()
    for column in (
        "schedule_timezone",
        "call_window_start",
        "call_window_end",
        "active_days",
        "max_attempts_per_lead",
        "retry_delay_minutes",
        "daily_call_cap",
        "max_total_calls",
        "tone_formality",
        "tone_pace",
        "tone_persistence",
        "tone_warmth",
        "tone_depth",
    ):
        assert f"ADD COLUMN {column} " in sql, column
    assert sql.count("ADD CONSTRAINT") == 10
