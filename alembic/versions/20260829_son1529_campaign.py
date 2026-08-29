"""Persist campaign schedule + tone dials per the SON-1493 ruling.

Revision ID: 20260829_son1529_campaign
Revises: 20260820_son877_auth_second_pass

Thirteen typed nullable columns on `campaigns` (8 schedule + 5 tone dials).
NULL = not configured; the app resolves defaults at read time. Revision id is
deliberately ≤ 32 chars: prod's alembic_version is still varchar(32) (the
255-char widening rides the unmerged SON-1477 branch).
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260829_son1529_campaign"
down_revision = "20260820_son877_auth_second_pass"
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
        ALTER TABLE campaigns
            DROP CONSTRAINT IF EXISTS campaigns_active_days_check,
            DROP CONSTRAINT IF EXISTS campaigns_tone_depth_check,
            DROP CONSTRAINT IF EXISTS campaigns_tone_warmth_check,
            DROP CONSTRAINT IF EXISTS campaigns_tone_persistence_check,
            DROP CONSTRAINT IF EXISTS campaigns_tone_pace_check,
            DROP CONSTRAINT IF EXISTS campaigns_tone_formality_check,
            DROP CONSTRAINT IF EXISTS campaigns_max_total_calls_check,
            DROP CONSTRAINT IF EXISTS campaigns_daily_call_cap_check,
            DROP CONSTRAINT IF EXISTS campaigns_retry_delay_minutes_check,
            DROP CONSTRAINT IF EXISTS campaigns_max_attempts_per_lead_check;
        ALTER TABLE campaigns
            DROP COLUMN IF EXISTS tone_depth,
            DROP COLUMN IF EXISTS tone_warmth,
            DROP COLUMN IF EXISTS tone_persistence,
            DROP COLUMN IF EXISTS tone_pace,
            DROP COLUMN IF EXISTS tone_formality,
            DROP COLUMN IF EXISTS max_total_calls,
            DROP COLUMN IF EXISTS daily_call_cap,
            DROP COLUMN IF EXISTS retry_delay_minutes,
            DROP COLUMN IF EXISTS max_attempts_per_lead,
            DROP COLUMN IF EXISTS active_days,
            DROP COLUMN IF EXISTS call_window_end,
            DROP COLUMN IF EXISTS call_window_start,
            DROP COLUMN IF EXISTS schedule_timezone;
        """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
