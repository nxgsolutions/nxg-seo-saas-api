"""domain score — whole-site score job + stored scores

A domain score aggregates a capped sample of a domain's per-page overall scores
plus site-wide technical signals into one 0-100. domain_score_jobs backs the
async run (UI polls it); domain_scores stores each computed score for the trend.

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-29
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE domain_score_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            status text NOT NULL DEFAULT 'running',
            step integer NOT NULL DEFAULT 0,
            step_label text,
            pages_total integer,
            pages_scored integer NOT NULL DEFAULT 0,
            error text,
            result_id uuid,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
    """)
    op.execute("CREATE INDEX ix_domain_score_jobs_domain ON domain_score_jobs (domain_id, started_at DESC)")
    op.execute("""
        CREATE TABLE domain_scores (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            overall integer NOT NULL,
            page_quality integer NOT NULL,
            site_technical integer NOT NULL,
            category_averages jsonb NOT NULL DEFAULT '{}'::jsonb,
            site_checks jsonb NOT NULL DEFAULT '[]'::jsonb,
            pages jsonb NOT NULL DEFAULT '[]'::jsonb,
            pages_total integer NOT NULL DEFAULT 0,
            pages_scored integer NOT NULL DEFAULT 0,
            pages_indexable integer NOT NULL DEFAULT 0,
            weak_share double precision NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_domain_scores_domain ON domain_scores (domain_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS domain_scores")
    op.execute("DROP TABLE IF EXISTS domain_score_jobs")
