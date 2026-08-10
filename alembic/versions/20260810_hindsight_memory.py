"""Add the deterministic-first Hindsight contact-memory outbox.

Revision ID: 20260810_hindsight_memory
Revises: 20260809_demo_seed, 20260809_son419
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260810_hindsight_memory"
down_revision = ("20260809_demo_seed", "20260809_son419")
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hindsight_sync_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS contact_memory_entries CASCADE")
    op.execute("DROP TABLE IF EXISTS contact_memory_batches CASCADE")
