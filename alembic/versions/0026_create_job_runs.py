"""job_runs: audit log for scheduled data-collection jobs

One row per run of a background job (e.g. the nightly GSC sync that grows the
DataCube). Records status, trigger, timing and a JSONB stats blob.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE job_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            job_name varchar(64) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'running',
            triggered_by varchar(16) NOT NULL DEFAULT 'manual',
            stats jsonb NOT NULL DEFAULT '{}'::jsonb,
            error text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT ck_job_runs_status
                CHECK (status IN ('running', 'success', 'partial', 'failed'))
        );
        CREATE INDEX ix_job_runs_job_name ON job_runs (job_name);
        CREATE INDEX ix_job_runs_started_at ON job_runs (started_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_runs;")
