"""page discovery — discovery columns on site_pages + page_discovery_runs

Page Discovery fills the existing site_pages table from a domain's sitemap (or a
crawl fallback) and enriches each page with http status / indexability / type.
A page_discovery_runs row records each scan (for the banner, KPIs and the delta).

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE site_pages
            ADD COLUMN discovery_source varchar(10),
            ADD COLUMN http_status int,
            ADD COLUMN indexable boolean,
            ADD COLUMN page_type varchar(20),
            ADD COLUMN sitemap_lastmod timestamptz,
            ADD COLUMN discovered_at timestamptz;

        CREATE TABLE page_discovery_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            -- how the URL list was obtained: 'sitemap' | 'crawl' | 'none'
            source varchar(10) NOT NULL DEFAULT 'none',
            sitemap_url text,
            -- 'running' | 'completed' | 'partial' | 'failed'
            status varchar(20) NOT NULL DEFAULT 'running',
            pages_found int NOT NULL DEFAULT 0,
            pages_enriched int NOT NULL DEFAULT 0,
            truncated boolean NOT NULL DEFAULT false,
            -- total pages from the previous completed run, for the "+N%" delta
            prev_total int,
            error text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        );
        CREATE INDEX ix_page_discovery_runs_domain
            ON page_discovery_runs (domain_id, started_at DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS page_discovery_runs;
        ALTER TABLE site_pages
            DROP COLUMN IF EXISTS discovery_source,
            DROP COLUMN IF EXISTS http_status,
            DROP COLUMN IF EXISTS indexable,
            DROP COLUMN IF EXISTS page_type,
            DROP COLUMN IF EXISTS sitemap_lastmod,
            DROP COLUMN IF EXISTS discovered_at;
        """
    )
