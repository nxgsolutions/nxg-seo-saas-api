"""content_optimizations: target keyword, country and website

Content Optimization now scores a page against a target keyword (and the
pages ranking for it), so a session records which keyword/country it was run
for and which website it belongs to. All columns are nullable — older
sessions (URL-only) keep working. Private per workspace, like before.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE content_optimizations
            ADD COLUMN keyword varchar(255),
            ADD COLUMN country_code varchar(8),
            ADD COLUMN domain_id uuid REFERENCES domains(id) ON DELETE SET NULL;
        CREATE INDEX ix_content_opt_ws_url ON content_optimizations (workspace_id, url, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_content_opt_ws_url;
        ALTER TABLE content_optimizations
            DROP COLUMN IF EXISTS domain_id,
            DROP COLUMN IF EXISTS country_code,
            DROP COLUMN IF EXISTS keyword;
        """
    )
