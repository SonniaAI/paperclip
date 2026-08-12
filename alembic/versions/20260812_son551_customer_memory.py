"""Harden SON-551 customer-memory provenance and erasure behavior.

Revision ID: 20260812_son551_customer_memory
Revises: 20260811_auth_claims
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260812_son551_customer_memory"
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
    op.execute("DROP TABLE IF EXISTS contact_memory_deletions CASCADE")
    op.execute("ALTER TABLE contact_memory_batches DROP COLUMN IF EXISTS extraction_provenance")
    op.execute("ALTER TABLE contact_memory_batches DROP COLUMN IF EXISTS speaker")
    op.execute("ALTER TABLE contact_memory_batches DROP COLUMN IF EXISTS source_occurred_at")
    op.execute("ALTER TABLE contact_memory_batches DROP COLUMN IF EXISTS source_event_id")
