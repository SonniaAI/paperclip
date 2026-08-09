from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from scripts.demo_seed import (
    AUDIO_FIXTURE_COUNT,
    CALL_COUNT,
    CONTACT_COUNT,
    DEMO_LABEL,
    DemoSeedError,
    _require_demo_seed_enabled,
    build_demo_bundle,
    build_parser,
    remove_audio_fixtures,
    write_audio_fixtures,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_SEED_SQL = (ROOT / "alembic/versions/20260809_demo_seed.sql").read_text(encoding="utf-8")


def test_demo_bundle_has_required_volume_variety_and_a_featured_path(tmp_path: Path) -> None:
    run_id = uuid4()
    fixtures = write_audio_fixtures(tmp_path, run_id)
    bundle = build_demo_bundle(
        run_id=run_id,
        org_id=uuid4(),
        department_id=uuid4(),
        audio_fixtures=fixtures,
        reference_time=datetime(2026, 8, 9, 12, tzinfo=UTC),
    )

    assert bundle.counts["contacts"] == CONTACT_COUNT
    assert bundle.counts["calls"] == CALL_COUNT
    assert bundle.counts["recordings"] == AUDIO_FIXTURE_COUNT
    assert bundle.counts["follow_ups"] == 84
    assert bundle.counts["activity_log"] == 18
    assert {row["status"] for row in bundle.records["follow_ups"]} == {
        "scheduled",
        "completed",
        "cancelled",
    }
    assert {row["status"] for row in bundle.records["calls"]} >= {
        "completed",
        "missed",
        "failed",
    }
    featured = [
        row
        for row in bundle.records["calls"]
        if json.loads(row["metadata"])["featured_demo_path"]
    ]
    assert [row["id"] for row in featured] == [bundle.featured_call_id]
    assert all(json.loads(row["metadata"])["demo_seed"] for row in bundle.records["calls"])
    assert all(path.local_path.read_bytes()[:4] == b"RIFF" for path in fixtures)
    assert remove_audio_fixtures(tmp_path, run_id)


def test_seed_is_explicitly_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANAGER_DEMO_SEED_ENABLED", raising=False)
    with pytest.raises(DemoSeedError, match="MANAGER_DEMO_SEED_ENABLED=true"):
        _require_demo_seed_enabled()

    monkeypatch.setenv("MANAGER_DEMO_SEED_ENABLED", "true")
    _require_demo_seed_enabled()


def test_cli_requires_the_matching_confirmation_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "seed",
            "--org-id",
            str(uuid4()),
            "--department-id",
            str(uuid4()),
        ]
    )
    assert args.confirm_demo_seed is False


def test_provenance_tables_are_forced_rls_and_analytics_is_security_invoker() -> None:
    for table in ("demo_seed_runs", "demo_seed_records"):
        assert f"CREATE TABLE {table} (" in DEMO_SEED_SQL
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in DEMO_SEED_SQL
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in DEMO_SEED_SQL
    assert DEMO_LABEL in DEMO_SEED_SQL
    assert "CREATE FUNCTION app.is_demo_seed_record" in DEMO_SEED_SQL
    assert "SECURITY INVOKER" in DEMO_SEED_SQL
