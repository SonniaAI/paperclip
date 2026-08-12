"""Persist the v2 registration profile fields.

Revision ID: 20260812_registration_v2
Revises: 20260812_son551_customer_memory
Create Date: 2026-08-12
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260812_registration_v2"
down_revision = "20260812_son551_customer_memory"
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
            DROP COLUMN IF EXISTS industry,
            DROP COLUMN IF EXISTS profile_role,
            DROP COLUMN IF EXISTS gender,
            DROP COLUMN IF EXISTS date_of_birth,
            DROP COLUMN IF EXISTS phone;
    """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
