"""opportunity research (pages, target keywords, trend history)

Domain-scoped Opportunity Research layer. References the canonical `keywords`
table; adds pages (URLs), per-keyword opportunity metrics (rank/intent/SERP
features/opportunity score/competitors) and a domain-level trend history.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE opportunity_pages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            url text NOT NULL,
            title varchar(300),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_opp_page_url UNIQUE (domain_id, url)
        );
        CREATE INDEX ix_opportunity_pages_domain_id ON opportunity_pages (domain_id);

        CREATE TABLE opportunity_keywords (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            page_id uuid REFERENCES opportunity_pages(id) ON DELETE SET NULL,
            keyword_id uuid REFERENCES keywords(id) ON DELETE SET NULL,
            keyword varchar(255) NOT NULL,
            search_volume integer NOT NULL DEFAULT 0,
            yoy_change double precision NOT NULL DEFAULT 0,
            keyword_difficulty integer NOT NULL DEFAULT 0,
            current_position integer,
            intent varchar(20) NOT NULL DEFAULT 'informational',
            opportunity_score integer NOT NULL DEFAULT 0,
            cpc double precision,
            ranking_url text,
            serp_features jsonb NOT NULL DEFAULT '[]'::jsonb,
            competitors jsonb NOT NULL DEFAULT '[]'::jsonb,
            country_code varchar(2) NOT NULL DEFAULT 'in',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_opp_domain_keyword UNIQUE (domain_id, keyword)
        );
        CREATE INDEX ix_opportunity_keywords_domain_id ON opportunity_keywords (domain_id);
        CREATE INDEX ix_opportunity_keywords_page_id ON opportunity_keywords (page_id);

        CREATE TABLE opportunity_trend (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            captured_on date NOT NULL,
            organic_traffic integer NOT NULL DEFAULT 0,
            ranking_keywords integer NOT NULL DEFAULT 0,
            serp_opportunities integer NOT NULL DEFAULT 0,
            CONSTRAINT uq_opp_trend_date UNIQUE (domain_id, captured_on)
        );
        CREATE INDEX ix_opportunity_trend_domain_id ON opportunity_trend (domain_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE opportunity_keywords;
        DROP TABLE opportunity_trend;
        DROP TABLE opportunity_pages;
        """
    )
