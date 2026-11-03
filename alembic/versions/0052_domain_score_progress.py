"""domain score progress — full-site coverage, failures + heartbeat

The SEO Score now audits every indexable page (not a 25-page sample),
throttled to stay polite. A long run needs two extra fields on the job row so
the UI can show real progress and the server can tell a live run from a dead one:

  pages_failed      pages that could not be fetched/scored (blocked or errored)
  last_progress_at  heartbeat — updated after each page, so a legitimately long
                    throttled run isn't mistaken for a stale 'running' row

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE domain_score_jobs ADD COLUMN pages_failed integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE domain_score_jobs ADD COLUMN last_progress_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE domain_score_jobs DROP COLUMN IF EXISTS last_progress_at")
    op.execute("ALTER TABLE domain_score_jobs DROP COLUMN IF EXISTS pages_failed")
