"""site_pages content cache — on-demand article HTML for the content editor

Stores the cleaned main-article HTML fetched when a page is opened in the Page
Discovery content editor, plus when it was fetched. Populated lazily (only for
pages the user opens), so large sites with many dynamic pages stay cheap to scan.
Additive and nullable (null = not fetched yet) -> non-breaking.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-09
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE site_pages ADD COLUMN content_html text;")
    op.execute("ALTER TABLE site_pages ADD COLUMN content_fetched_at timestamptz;")


def downgrade() -> None:
    op.execute("ALTER TABLE site_pages DROP COLUMN IF EXISTS content_html;")
    op.execute("ALTER TABLE site_pages DROP COLUMN IF EXISTS content_fetched_at;")
