"""site_pages edited content — user-saved edits from the content editor

Stores the title/meta/body the user explicitly SAVES in the Page Discovery
content editor, kept separate from `content_html` (the fetched cache). A live
re-fetch rebuilds `content_html` but must never touch the saved edit, so the
editor always reopens the saved version. Additive and nullable (null = no saved
edit) -> non-breaking.

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-06
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE site_pages ADD COLUMN edited_content_html text;")
    op.execute("ALTER TABLE site_pages ADD COLUMN content_edited_at timestamptz;")


def downgrade() -> None:
    op.execute("ALTER TABLE site_pages DROP COLUMN IF EXISTS edited_content_html;")
    op.execute("ALTER TABLE site_pages DROP COLUMN IF EXISTS content_edited_at;")
