"""
Contract tests for the SON-419 migration.

These are pure-file checks (no database required): they prove the SON-419
migration adds the versioned-instruction, personal/team task list, contact
import, and onboarding-state schema required by the issue definition of done.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "alembic/versions/20260809_son419.sql").read_text(encoding="utf-8")
FEATURE = (ROOT / "alembic/versions/20260809_feature_data.sql").read_text(encoding="utf-8")


def test_instructions_gain_version_history_and_in_effect_since() -> None:
    assert "ADD COLUMN slug text NOT NULL DEFAULT ''" in SQL
    assert "ADD COLUMN version integer NOT NULL DEFAULT 1" in SQL
    assert "ADD COLUMN effective_from timestamptz NOT NULL DEFAULT now()" in SQL
    assert "ADD COLUMN superseded_at timestamptz" in SQL
    # Exactly one active version per topic is enforced with a partial unique
    # index, so editing creates a new version and supersedes the old one.
    assert "instructions_active_slug_key" in SQL
    assert "WHERE superseded_at IS NULL" in SQL
    assert "instructions_history_idx" in SQL


def test_task_lists_become_personal_or_team() -> None:
    assert "ADD COLUMN owner_user_id uuid" in SQL
    assert "ADD COLUMN visibility varchar(10) NOT NULL DEFAULT 'private'" in SQL
    assert "task_lists_visibility_check" in SQL


def test_contact_imports_are_tenant_scoped_and_forced_rls() -> None:
    assert "CREATE TABLE contact_imports (" in SQL
    assert "org_id uuid NOT NULL" in SQL
    assert "department_id uuid NOT NULL" in SQL
    assert "ALTER TABLE contact_imports ENABLE ROW LEVEL SECURITY;" in SQL
    assert "ALTER TABLE contact_imports FORCE ROW LEVEL SECURITY;" in SQL
    for counter in ("column_count", "row_count", "created_count", "merged_count", "skipped_count"):
        assert f"ADD COLUMN {counter}" in SQL.replace("        ", "") or f"{counter} " in SQL


def test_organisations_carry_onboarding_state() -> None:
    assert "ADD COLUMN onboarding_state text NOT NULL DEFAULT 'pending'" in SQL
    assert "organizations_onboarding_state_check" in SQL


def test_down_revision_removes_son419_schema() -> None:
    py = (ROOT / "alembic/versions/20260809_son419.py").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS contact_imports CASCADE" in py
    assert "DROP COLUMN IF EXISTS onboarding_state" in py
