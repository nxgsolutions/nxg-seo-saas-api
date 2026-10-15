"""saved_keywords.keyword_id — link saved keywords to the master library

saved_keywords stored only the term text, with no FK to `keywords` (every other
keyword table links via keyword_id). This adds a nullable keyword_id FK and
back-fills it by matching normalized_keyword, so saved keywords can join metrics
and relate to opportunities like the rest. Additive + nullable → non-breaking.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-12
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE saved_keywords
            ADD COLUMN keyword_id uuid REFERENCES keywords(id) ON DELETE SET NULL;
        CREATE INDEX ix_saved_keywords_keyword_id ON saved_keywords (keyword_id);
        UPDATE saved_keywords s
            SET keyword_id = k.id
            FROM keywords k
            WHERE k.normalized_keyword = s.normalized_keyword
              AND s.keyword_id IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_saved_keywords_keyword_id;
        ALTER TABLE saved_keywords DROP COLUMN IF EXISTS keyword_id;
        """
    )
