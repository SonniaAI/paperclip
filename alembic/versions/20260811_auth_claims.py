"""Persist single-use two-factor login challenges.

Revision ID: 20260811_auth_claims
Revises: 20260811_auth_flow
Create Date: 2026-08-11
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260811_auth_claims"
down_revision = "20260811_auth_flow"
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
            DROP COLUMN IF EXISTS two_factor_challenge_expires_at,
            DROP COLUMN IF EXISTS two_factor_challenge_token_digest;
    """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
