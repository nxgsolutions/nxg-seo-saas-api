"""page overall score — store the weighted 0-100 + category breakdown on snapshots

The overall page SEO score combines the existing technical/on-page audit, the
content-quality engine and Core Web Vitals into one weighted 0-100. A "score"
run records a page_snapshots row like any other fetch; these two columns hold
its overall number and per-category breakdown so historical score comparison
works through the same page-history path.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE page_snapshots ADD COLUMN overall_score integer")
    op.execute("ALTER TABLE page_snapshots ADD COLUMN category_scores jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE page_snapshots DROP COLUMN IF EXISTS category_scores")
    op.execute("ALTER TABLE page_snapshots DROP COLUMN IF EXISTS overall_score")
