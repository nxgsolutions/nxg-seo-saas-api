"""domain_monthly_metrics: 12-month whole-site history from DataForSEO Labs

One row per (target domain, location, language, year, month) with the monthly
organic estimated traffic (etv) from historical_bulk_traffic_estimation and the
monthly ranking-keyword count + position distribution from
historical_rank_overview. Feeds the Competitive Strategy performance graph with
real monthly history, instead of the daily single-snapshot accumulation in
domain_analytics_snapshots (which is left untouched for the KPI table + donut).

Global (not tenant-scoped), same as domain_analytics_snapshots: a public
domain's history is the same for every workspace. `environment` ("live" |
"sandbox") is part of the key so sandbox rows never leak into a live workspace.

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE domain_monthly_metrics (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            environment varchar(10) NOT NULL DEFAULT 'live',
            target varchar(255) NOT NULL,
            location_code integer NOT NULL,
            language_code varchar(8) NOT NULL,
            year integer NOT NULL,
            month integer NOT NULL,
            source varchar(32) NOT NULL DEFAULT 'dfs_historical',
            organic_etv double precision NOT NULL DEFAULT 0,
            organic_count integer NOT NULL DEFAULT 0,
            pos_1 integer,
            pos_2_3 integer,
            pos_4_10 integer,
            pos_11_20 integer,
            pos_21_30 integer,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_domain_monthly_metric
                UNIQUE (environment, target, location_code, language_code, year, month)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_domain_monthly_lookup ON domain_monthly_metrics "
        "(environment, target, location_code, language_code, year, month)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS domain_monthly_metrics")
