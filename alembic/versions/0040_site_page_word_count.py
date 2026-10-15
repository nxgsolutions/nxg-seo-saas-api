"""site_pages.word_count — main-content word count from discovery

Stores the clean main-content word count extracted during a page-discovery
scan (via trafilatura), so the UI can show word count and flag thin content.
Additive and nullable (null = not measured yet) → non-breaking.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-09
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE site_pages ADD COLUMN word_count integer;")


def downgrade() -> None:
    op.execute("ALTER TABLE site_pages DROP COLUMN IF EXISTS word_count;")
