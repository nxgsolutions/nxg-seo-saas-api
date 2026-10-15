"""keyword_researches: add workspace_id, make domain_id nullable

Lets a keyword-only search (no domain) be recorded and scoped to a workspace —
the "giant" model where keywords live globally and a search is just a workspace
record. Domain-scoped runs keep working (workspace_id derived from the domain).

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-10
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE keyword_researches ADD COLUMN workspace_id UUID;
        -- Backfill any existing domain-scoped rows from the domain's workspace.
        UPDATE keyword_researches r
        SET workspace_id = d.workspace_id
        FROM domains d
        WHERE d.id = r.domain_id AND r.workspace_id IS NULL;
        ALTER TABLE keyword_researches
            ALTER COLUMN workspace_id SET NOT NULL,
            ADD CONSTRAINT keyword_researches_workspace_id_fkey
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
            ALTER COLUMN domain_id DROP NOT NULL;
        CREATE INDEX ix_keyword_researches_workspace_id
            ON keyword_researches (workspace_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_keyword_researches_workspace_id;
        -- Drop rows with no domain (they cannot exist under the old NOT NULL).
        DELETE FROM keyword_researches WHERE domain_id IS NULL;
        ALTER TABLE keyword_researches
            ALTER COLUMN domain_id SET NOT NULL,
            DROP CONSTRAINT keyword_researches_workspace_id_fkey,
            DROP COLUMN workspace_id;
        """
    )
