"""Install the Phase 1 feature data model and webhook inbox."""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260809_feature_data"
down_revision = "20260809_gate0"
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
        DROP TABLE IF EXISTS telnyx_webhook_events CASCADE;
        DROP TABLE IF EXISTS security_events CASCADE;
        DROP TABLE IF EXISTS integrations CASCADE;
        DROP TABLE IF EXISTS activity_log CASCADE;
        DROP TABLE IF EXISTS instructions CASCADE;
        DROP TABLE IF EXISTS test_sessions CASCADE;
        DROP TABLE IF EXISTS test_suites CASCADE;
        DROP TABLE IF EXISTS test_personas CASCADE;
        DROP TABLE IF EXISTS campaign_reads CASCADE;
        DROP TABLE IF EXISTS org_balance CASCADE;
        DROP TABLE IF EXISTS spend_ledger CASCADE;
        DROP TABLE IF EXISTS extracted_claims CASCADE;
        DROP TABLE IF EXISTS campaign_materials CASCADE;
        DROP TABLE IF EXISTS brief_chats CASCADE;
        DROP TABLE IF EXISTS campaign_briefs CASCADE;
        DROP TABLE IF EXISTS campaigns CASCADE;
        DROP TABLE IF EXISTS tasks CASCADE;
        DROP TABLE IF EXISTS task_lists CASCADE;
        DROP TABLE IF EXISTS notes CASCADE;
        DROP TABLE IF EXISTS follow_ups CASCADE;
        DROP TABLE IF EXISTS actions CASCADE;
        DROP TABLE IF EXISTS promises CASCADE;
        DROP TABLE IF EXISTS call_summaries CASCADE;
        DROP TABLE IF EXISTS transcript_segments CASCADE;
        DROP TABLE IF EXISTS transcripts CASCADE;
        DROP TABLE IF EXISTS recordings CASCADE;
        ALTER TABLE calls
            DROP CONSTRAINT IF EXISTS calls_status_check,
            DROP CONSTRAINT IF EXISTS calls_direction_check,
            DROP CONSTRAINT IF EXISTS calls_org_id_id_key,
            DROP COLUMN IF EXISTS updated_at,
            DROP COLUMN IF EXISTS metadata,
            DROP COLUMN IF EXISTS ended_at,
            DROP COLUMN IF EXISTS started_at,
            DROP COLUMN IF EXISTS to_phone_e164,
            DROP COLUMN IF EXISTS from_phone_e164,
            DROP COLUMN IF EXISTS telnyx_call_session_id,
            DROP COLUMN IF EXISTS telnyx_call_control_id,
            DROP COLUMN IF EXISTS status,
            DROP COLUMN IF EXISTS direction,
            DROP COLUMN IF EXISTS company_id,
            DROP COLUMN IF EXISTS contact_id;
        DROP TABLE IF EXISTS contact_emails CASCADE;
        DROP TABLE IF EXISTS contact_phones CASCADE;
        DROP TABLE IF EXISTS contacts CASCADE;
        DROP TABLE IF EXISTS companies CASCADE;
        """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
