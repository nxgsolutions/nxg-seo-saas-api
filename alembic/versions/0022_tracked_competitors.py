"""tracked_competitors (user-editable competitor list per domain)

Curated list of competitors the user tracks for a domain. Seeded from
serp_snapshots (source='auto') on first refresh; the user can add ('manual') or
remove entries. competitor_rank_snapshots captures the time series for whoever
is listed here.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tracked_competitors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            competitor varchar(255) NOT NULL,
            label varchar(255),
            source varchar(16) NOT NULL DEFAULT 'manual',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_tracked_competitor UNIQUE (domain_id, competitor)
        );
        CREATE INDEX ix_tracked_competitors_domain_id
            ON tracked_competitors (domain_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE tracked_competitors;")
