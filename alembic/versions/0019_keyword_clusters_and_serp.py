"""keyword clustering + SERP snapshots (DataCube foundation)

Additive only — no existing table is touched, so the working Ads keyword-fetch
and GSC reports are unaffected.

* keyword_clusters / keyword_cluster_members — group global keywords into
  topic clusters (built lexically from data we already have, or later from
  SERP overlap). Gives `keyword_relations` a higher-level home.
* serp_snapshots — one scraped/imported SERP result (keyword x position x url)
  with a captured_at, so competitor rankings and own-SERP difficulty have a
  place to live and can be tracked over time. Nothing writes real Google-SERP
  rows automatically; this is the store the scraping/import layer fills.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE keyword_clusters (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            domain_id uuid REFERENCES domains(id) ON DELETE CASCADE,
            label varchar(200) NOT NULL,
            head_keyword_id uuid REFERENCES keywords(id) ON DELETE SET NULL,
            size integer NOT NULL DEFAULT 0,
            total_volume bigint,
            source varchar(30) NOT NULL DEFAULT 'lexical',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_keyword_clusters_workspace_id ON keyword_clusters (workspace_id);
        CREATE INDEX ix_keyword_clusters_domain_id ON keyword_clusters (domain_id);
        """
    )
    op.execute(
        """
        CREATE TABLE keyword_cluster_members (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            cluster_id uuid NOT NULL REFERENCES keyword_clusters(id) ON DELETE CASCADE,
            keyword_id uuid NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
            score numeric(8,4),
            is_head boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_cluster_member UNIQUE (cluster_id, keyword_id)
        );
        CREATE INDEX ix_keyword_cluster_members_cluster_id ON keyword_cluster_members (cluster_id);
        CREATE INDEX ix_keyword_cluster_members_keyword_id ON keyword_cluster_members (keyword_id);
        """
    )
    op.execute(
        """
        CREATE TABLE serp_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            keyword_id uuid NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
            country_code varchar(2) NOT NULL DEFAULT 'in',
            language_code varchar(10) NOT NULL DEFAULT 'en',
            position smallint NOT NULL,
            url text NOT NULL,
            domain varchar(255) NOT NULL,
            title text,
            source varchar(30) NOT NULL DEFAULT 'scrape',
            captured_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_serp_snapshots_keyword_id ON serp_snapshots (keyword_id);
        CREATE INDEX ix_serp_snapshots_domain ON serp_snapshots (domain);
        CREATE INDEX ix_serp_snapshots_captured_at ON serp_snapshots (captured_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS serp_snapshots;")
    op.execute("DROP TABLE IF EXISTS keyword_cluster_members;")
    op.execute("DROP TABLE IF EXISTS keyword_clusters;")
