"""Merge the customer-memory and Phase 1 campaign migration heads.

Revision ID: 20260812_merge_son551_campaigns
Revises: 20260811_phase1_campaign_imports, 20260812_son551_customer_memory
"""

from __future__ import annotations

revision = "20260812_merge_son551_campaigns"
down_revision = ("20260811_phase1_campaign_imports", "20260812_son551_customer_memory")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge-only revision: both parent schema changes are independent.
    pass


def downgrade() -> None:
    # Merge-only revision: Alembic walks each parent independently on downgrade.
    pass
