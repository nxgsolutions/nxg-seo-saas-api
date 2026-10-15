"""domain_analytics_snapshots: whole-site domain metrics from DataForSEO

One row per (target domain, location, language, day) with the DataForSEO Labs
domain_rank_overview organic metrics (estimated traffic, ranking-keyword count,
position distribution, movement) plus the Backlinks bulk_ranks domain rank.

Global (not tenant-scoped): a public domain's metrics are the same for every
workspace, so one fetch is shared by all and history accumulates per day for
trend queries. `raw` keeps the full provider metrics so fields can be re-parsed
later without paying for another call.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE domain_analytics_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            target varchar(255) NOT NULL,
            location_code integer NOT NULL,
            language_code varchar(8) NOT NULL,
            fetched_on date NOT NULL DEFAULT CURRENT_DATE,
            source varchar(32) NOT NULL DEFAULT 'dataforseo_labs',
            organic_etv double precision NOT NULL DEFAULT 0,
            organic_count integer NOT NULL DEFAULT 0,
            pos_1 integer NOT NULL DEFAULT 0,
            pos_2_3 integer NOT NULL DEFAULT 0,
            pos_4_10 integer NOT NULL DEFAULT 0,
            pos_11_20 integer NOT NULL DEFAULT 0,
            is_new integer NOT NULL DEFAULT 0,
            is_up integer NOT NULL DEFAULT 0,
            is_down integer NOT NULL DEFAULT 0,
            is_lost integer NOT NULL DEFAULT 0,
            traffic_cost double precision NOT NULL DEFAULT 0,
            paid_etv double precision NOT NULL DEFAULT 0,
            paid_count integer NOT NULL DEFAULT 0,
            domain_rank integer,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_domain_analytics_day
                UNIQUE (target, location_code, language_code, fetched_on)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_domain_analytics_lookup ON domain_analytics_snapshots "
        "(target, location_code, language_code, fetched_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS domain_analytics_snapshots")
