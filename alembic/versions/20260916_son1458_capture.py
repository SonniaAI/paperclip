"""CP2/CP3 append-only capture tables at the webhook boundary (SON-1458).

Revision ID: 20260916_son1458_capture
Revises: 20260829_son1529_campaign

Two append-only, intentionally non-tenant-scoped audit tables:
webhook_receipt_capture (CP2) and ingestion_trace_capture (CP3).
The application only INSERTs; no backfill is required.  Revision id stays
under 32 chars like its predecessors.
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op

revision = "20260916_son1458_capture"
down_revision = "20260829_son1529_campaign"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    if context.is_offline_mode():
        op.execute(sql.rstrip().removesuffix(";"))
    else:
        op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ingestion_trace_capture")
    op.execute("DROP TABLE IF EXISTS webhook_receipt_capture")
