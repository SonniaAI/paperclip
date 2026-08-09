"""Install Gate 0 tenant schema and PostgreSQL RLS policies.

Revision ID: 20260809_gate0
Revises:
Create Date: 2026-08-09
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260809_gate0"
down_revision = None
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
        DROP TABLE IF EXISTS active_dials CASCADE;
        DROP TABLE IF EXISTS do_not_contacts CASCADE;
        DROP TABLE IF EXISTS calls CASCADE;
        DROP TABLE IF EXISTS user_sessions CASCADE;
        DROP TABLE IF EXISTS invites CASCADE;
        DROP TABLE IF EXISTS membership_departments CASCADE;
        DROP TABLE IF EXISTS memberships CASCADE;
        DROP TABLE IF EXISTS app_users CASCADE;
        DROP TABLE IF EXISTS departments CASCADE;
        DROP TABLE IF EXISTS organizations CASCADE;
        DROP FUNCTION IF EXISTS app.resolve_login_scope(text, text);
        DROP FUNCTION IF EXISTS app.current_department_id();
        DROP FUNCTION IF EXISTS app.current_org_id();
        DROP SCHEMA IF EXISTS app;
        """
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)
