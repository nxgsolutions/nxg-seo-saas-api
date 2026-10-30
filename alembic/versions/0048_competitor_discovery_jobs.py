"""competitor_discovery_jobs — async discovery job + live step progress

Competitor discovery runs 3 LLM passes + live-site profiling (~1-2 min). Running
it synchronously makes the request fragile (dev proxies reset long connections).
This table backs an async job: the POST starts a background worker and returns a
job id; the UI polls this row for the current step and completion, then refetches
the competitor list. Mirrors the page_discovery run pattern.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE competitor_discovery_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            status text NOT NULL DEFAULT 'running',
            step integer NOT NULL DEFAULT 0,
            step_label text,
            total_steps integer NOT NULL DEFAULT 5,
            count integer,
            error text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
    """)
    op.execute(
        "CREATE INDEX ix_discovery_jobs_domain ON competitor_discovery_jobs "
        "(domain_id, started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS competitor_discovery_jobs")
