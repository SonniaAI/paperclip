"""Add the live Phase 1 campaign and customer-list contract tables.

Revision ID: 20260811_phase1_campaign_imports
Revises: 20260811_auth_claims
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260811_phase1_campaign_imports"
down_revision = "20260811_auth_claims"
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
        DROP TABLE IF EXISTS customer_list_import_contacts CASCADE;
        DROP TABLE IF EXISTS customer_list_imports CASCADE;
        DROP TABLE IF EXISTS campaign_targets CASCADE;
        ALTER TABLE campaigns
            DROP COLUMN IF EXISTS launched_at,
            DROP COLUMN IF EXISTS objective;
        """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
