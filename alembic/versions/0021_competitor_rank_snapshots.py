"""competitor rank snapshots (per-competitor SERP standing over time)

Backs the /competitors rank-change time series. Each row is one competitor's
avg/best organic position + keyword overlap for a domain on a captured_on date,
derived from serp_snapshots at refresh time. `is_simulated` flags backfilled
demo history that real refreshes replace.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE competitor_rank_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            competitor varchar(255) NOT NULL,
            captured_on date NOT NULL,
            avg_position double precision NOT NULL,
            best_position integer NOT NULL,
            keywords_shared integer NOT NULL DEFAULT 0,
            country_code varchar(2) NOT NULL DEFAULT 'in',
            language_code varchar(10) NOT NULL DEFAULT 'en',
            is_simulated boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_competitor_domain_date
                UNIQUE (domain_id, competitor, captured_on)
        );
        CREATE INDEX ix_competitor_rank_snapshots_domain_id
            ON competitor_rank_snapshots (domain_id);
        CREATE INDEX ix_competitor_rank_snapshots_domain_captured
            ON competitor_rank_snapshots (domain_id, captured_on);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE competitor_rank_snapshots;")
