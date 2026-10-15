"""domain_ranked_keyword_runs + domain_ranked_keywords (DataForSEO Labs)

Keywords a domain ACTUALLY ranks for (position + ranking URL), from DataForSEO
Labs `ranked_keywords`. One run = one paid fetch (top-N keywords by traffic for
a target/location/language), with the provider-reported cost recorded. Rows are
a per-run snapshot, so history accumulates per domain.

Global (not tenant-scoped) like domain_analytics_snapshots: a public domain's
rankings are the same for every workspace, so one fetch is shared. Tagged with
`environment` ('live' | 'sandbox') — every read filters on it. keyword_id links
to the canonical keywords table for live rows only (sandbox rows are dummy data
and must not grow the global keyword table), so `keyword` text is stored too.

New tables only — no existing table is altered.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE domain_ranked_keyword_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            environment varchar(10) NOT NULL DEFAULT 'live',
            target varchar(255) NOT NULL,
            location_code integer NOT NULL,
            language_code varchar(8) NOT NULL,
            source varchar(32) NOT NULL DEFAULT 'dataforseo_labs',
            requested_limit integer NOT NULL,
            total_count integer,
            items_count integer NOT NULL DEFAULT 0,
            cost numeric(10, 5),
            meta jsonb NOT NULL DEFAULT '{}'::jsonb,
            fetched_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_ranked_runs_lookup ON domain_ranked_keyword_runs "
        "(environment, target, location_code, language_code, fetched_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE domain_ranked_keywords (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES domain_ranked_keyword_runs(id) ON DELETE CASCADE,
            keyword_id uuid REFERENCES keywords(id) ON DELETE SET NULL,
            keyword varchar(255) NOT NULL,
            rank_absolute integer,
            rank_group integer,
            prev_rank_absolute integer,
            is_new boolean NOT NULL DEFAULT false,
            is_up boolean NOT NULL DEFAULT false,
            is_down boolean NOT NULL DEFAULT false,
            serp_item_type varchar(40),
            url text,
            relative_url text,
            title text,
            etv double precision,
            search_volume bigint,
            cpc numeric(12, 4),
            competition double precision,
            keyword_difficulty smallint,
            intent varchar(20),
            serp_features jsonb NOT NULL DEFAULT '[]'::jsonb,
            monthly jsonb NOT NULL DEFAULT '[]'::jsonb,
            raw jsonb,
            CONSTRAINT uq_ranked_keyword_run UNIQUE (run_id, keyword)
        )
        """
    )
    op.execute("CREATE INDEX ix_ranked_keywords_run ON domain_ranked_keywords (run_id)")
    op.execute("CREATE INDEX ix_ranked_keywords_keyword_id ON domain_ranked_keywords (keyword_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS domain_ranked_keywords")
    op.execute("DROP TABLE IF EXISTS domain_ranked_keyword_runs")
