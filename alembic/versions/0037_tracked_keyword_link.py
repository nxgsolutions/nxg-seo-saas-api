"""tracked_keywords.keyword_id — link the Keyword List to the shared keyword library

Adds a nullable FK from tracked_keywords to the global keywords table so a
tracked term can join keyword_metrics (volume / difficulty / intent). Existing
rows are backfilled by matching the normalized term.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-06
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tracked_keywords
            ADD COLUMN keyword_id uuid REFERENCES keywords(id) ON DELETE SET NULL;
        CREATE INDEX ix_tracked_keywords_keyword_id ON tracked_keywords (keyword_id);

        -- Backfill: link each existing tracked term to its canonical keyword row
        -- (match on the normalized term). Leaves keyword_id NULL when no match.
        UPDATE tracked_keywords t
           SET keyword_id = k.id
          FROM keywords k
         WHERE t.keyword_id IS NULL
           AND k.normalized_keyword = lower(btrim(t.term));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_tracked_keywords_keyword_id;
        ALTER TABLE tracked_keywords DROP COLUMN IF EXISTS keyword_id;
        """
    )
