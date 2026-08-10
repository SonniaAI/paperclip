"""Install the SON-419 feature slice: versioned instructions, personal/team
task lists, durable contact-import runs, and organisation onboarding state.

Revision ID: 20260809_son419
Revises: 20260809_feature_data
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260809_son419"
down_revision = "20260809_feature_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    sql = """
        ALTER TABLE organizations
            DROP CONSTRAINT IF EXISTS organizations_onboarding_state_check,
            DROP COLUMN IF EXISTS onboarding_state;
        DROP TABLE IF EXISTS contact_imports CASCADE;
        ALTER TABLE task_lists
            DROP CONSTRAINT IF EXISTS task_lists_visibility_check,
            DROP COLUMN IF EXISTS visibility,
            DROP COLUMN IF EXISTS owner_user_id;
        DROP INDEX IF EXISTS activity_log_feed_idx;
        ALTER TABLE activity_log
            DROP COLUMN IF EXISTS private;
        DROP INDEX IF EXISTS instructions_history_idx;
        DROP INDEX IF EXISTS instructions_active_slug_key;
        ALTER TABLE instructions
            DROP COLUMN IF EXISTS superseded_at,
            DROP COLUMN IF EXISTS effective_from,
            DROP COLUMN IF EXISTS version,
            DROP COLUMN IF EXISTS slug;
        """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
