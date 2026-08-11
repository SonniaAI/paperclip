"""Add email-first registration, verification, reset, and login controls.

Revision ID: 20260811_auth_flow
Revises: 20260810_hindsight_memory
Create Date: 2026-08-11
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260811_auth_flow"
down_revision = "20260810_hindsight_memory"
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
        DROP FUNCTION IF EXISTS app.revoke_user_sessions(uuid, uuid);
        DROP FUNCTION IF EXISTS app.resolve_auth_scope(text);
        DROP TABLE IF EXISTS app.auth_login_attempts;
        DROP INDEX IF EXISTS app_users_global_email_key;
        ALTER TABLE app_users
            DROP COLUMN IF EXISTS password_reset_expires_at,
            DROP COLUMN IF EXISTS password_reset_token_digest,
            DROP COLUMN IF EXISTS email_verification_expires_at,
            DROP COLUMN IF EXISTS email_verification_token_digest,
            DROP COLUMN IF EXISTS email_verified_at;
        ALTER TABLE organizations
            DROP COLUMN IF EXISTS country,
            DROP COLUMN IF EXISTS account_type;
    """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
