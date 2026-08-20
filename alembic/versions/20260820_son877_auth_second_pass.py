"""Add marketing-consent evidence columns (SON-877).

Revision ID: 20260820_son877_auth_second_pass
Revises: 20260812_registration_v2
Create Date: 2026-08-20
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260820_son877_auth_second_pass"
down_revision = "20260812_registration_v2"
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
        ALTER TABLE app_users
            DROP COLUMN IF EXISTS marketing_consent_at,
            DROP COLUMN IF EXISTS marketing_consent_source;
    """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
