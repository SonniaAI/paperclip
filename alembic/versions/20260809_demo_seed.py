"""Add durable provenance for explicitly flagged demo seed data.

Revision ID: 20260809_demo_seed
Revises: 20260809_feature_data
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260809_demo_seed"
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
    op.execute("DROP FUNCTION IF EXISTS app.is_demo_seed_record(text, uuid)")
    op.execute("DROP TABLE IF EXISTS demo_seed_records CASCADE")
    op.execute("DROP TABLE IF EXISTS demo_seed_runs CASCADE")
