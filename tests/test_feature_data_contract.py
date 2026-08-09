from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "alembic/versions/20260809_feature_data.sql").read_text(encoding="utf-8")

FEATURE_TABLES = (
    "companies",
    "contacts",
    "contact_phones",
    "contact_emails",
    "recordings",
    "transcripts",
    "transcript_segments",
    "call_summaries",
    "promises",
    "actions",
    "follow_ups",
    "notes",
    "task_lists",
    "tasks",
    "campaigns",
    "campaign_briefs",
    "brief_chats",
    "campaign_materials",
    "extracted_claims",
    "spend_ledger",
    "org_balance",
    "campaign_reads",
    "test_personas",
    "test_suites",
    "test_sessions",
    "instructions",
    "activity_log",
    "integrations",
    "security_events",
    "telnyx_webhook_events",
)


def _definition(table: str) -> str:
    return SQL.split(f"CREATE TABLE {table} (", 1)[1].split(");", 1)[0]


def test_feature_tables_are_tenant_scoped_and_forced_rls() -> None:
    for table in FEATURE_TABLES:
        definition = _definition(table)
        assert "org_id uuid NOT NULL" in definition
        assert "department_id uuid NOT NULL" in definition
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in SQL
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in SQL


def test_transcript_timing_and_raw_webhook_contracts_are_explicit() -> None:
    segment = _definition("transcript_segments")
    assert "start_ms integer NOT NULL" in segment
    assert "end_ms integer NOT NULL" in segment
    assert "markers jsonb NOT NULL" in segment

    webhook = _definition("telnyx_webhook_events")
    assert "event_id text NOT NULL UNIQUE" in webhook
    assert "raw_payload jsonb NOT NULL" in webhook
    assert "payload_sha256 char(64) NOT NULL" in webhook
    assert "status varchar(20) NOT NULL DEFAULT 'received'" in webhook


def test_recordings_have_no_public_url_and_are_private_only() -> None:
    recording = _definition("recordings")
    assert "storage_bucket text NOT NULL" in recording
    assert "storage_key text NOT NULL" in recording
    assert "public_url" not in recording
    assert "CHECK (is_private = true)" in recording
