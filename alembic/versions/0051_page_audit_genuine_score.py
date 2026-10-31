"""page audit genuine score — overall + category breakdown + blockers on saved audits

Persist the genuine overall SEO score, the per-category breakdown and the blocking
problems on each saved audit (computed at save time by page_score_service.compose).
This lets one saved audit power the score, the breakdown and the fixes, so the
unified Page Audit page no longer needs separate page-score / optimize calls.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-31
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE page_audits
            ADD COLUMN overall_score integer,
            ADD COLUMN category_scores jsonb,
            ADD COLUMN blockers jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE page_audits
            DROP COLUMN IF EXISTS overall_score,
            DROP COLUMN IF EXISTS category_scores,
            DROP COLUMN IF EXISTS blockers
        """
    )
