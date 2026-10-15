"""ai_overviews: cached 3-4 sentence summaries per page / section

One row per (workspace, scope, scope_key) — e.g. ("keyword_search", <research id>),
("keyword_metrics", "in:en:upsc pyq"), ("action_plan", <domain id>). `facts_hash`
is a hash of the stored data the summary was written from, so it is regenerated
only when that data changes (or on an explicit refresh).

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_overviews (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            scope varchar(32) NOT NULL,
            scope_key varchar(512) NOT NULL,
            facts_hash varchar(64) NOT NULL,
            summary text NOT NULL,
            source varchar(16) NOT NULL,
            model varchar(64),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_ai_overview_scope UNIQUE (workspace_id, scope, scope_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_overviews")
