"""site_pages content_score — SEO score of the saved edited content

When a user saves edits in the Page Discovery content editor, the deterministic
on-page score (same rules as the live editor score) is recomputed and stored
here, so the saved page carries an up-to-date content score outside the editor
(e.g. in the discovered-pages list). Additive and nullable (null = never scored)
-> non-breaking.

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-12
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE site_pages ADD COLUMN content_score integer;")


def downgrade() -> None:
    op.execute("ALTER TABLE site_pages DROP COLUMN IF EXISTS content_score;")
