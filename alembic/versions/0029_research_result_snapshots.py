"""Store domain-search results so a search can be reopened without re-fetching

- keyword_researches: target (normalized host for kind='domain'), mode
  ('site'|'url'), source ('google_ads'), result_count. Lets a domain search —
  your own site or a competitor's — be found again by domain.
- keyword_research_results: a per-run copy of what the provider returned
  (volume, competition, CPC range, intent, SEO difficulty, monthly volumes), so
  reopening a search replays it exactly instead of calling Google Ads again.

Existing domain runs get `target` backfilled from their seed keyword.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE keyword_researches
            ADD COLUMN target varchar(255),
            ADD COLUMN mode varchar(10),
            ADD COLUMN source varchar(30),
            ADD COLUMN result_count integer NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE keyword_researches r
        SET target = regexp_replace(lower(k.keyword), '^www\\.', ''), source = 'google_ads'
        FROM keywords k
        WHERE k.id = r.seed_keyword_id AND r.kind = 'domain'
        """
    )
    op.execute(
        """
        UPDATE keyword_researches r
        SET result_count = (SELECT count(*) FROM keyword_research_results x
                            WHERE x.research_id = r.id)
        """
    )
    op.execute(
        "CREATE INDEX ix_keyword_researches_domain_lookup ON keyword_researches "
        "(workspace_id, kind, target, country_code, language_code, created_at DESC)"
    )

    op.execute(
        """
        ALTER TABLE keyword_research_results
            ADD COLUMN search_volume bigint,
            ADD COLUMN competition varchar(20),
            ADD COLUMN competition_index smallint,
            ADD COLUMN cpc_low numeric(12, 4),
            ADD COLUMN cpc_high numeric(12, 4),
            ADD COLUMN intent varchar(20),
            ADD COLUMN seo_difficulty smallint,
            ADD COLUMN monthly jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE keyword_research_results
            DROP COLUMN search_volume, DROP COLUMN competition,
            DROP COLUMN competition_index, DROP COLUMN cpc_low, DROP COLUMN cpc_high,
            DROP COLUMN intent, DROP COLUMN seo_difficulty, DROP COLUMN monthly
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_keyword_researches_domain_lookup")
    op.execute(
        """
        ALTER TABLE keyword_researches
            DROP COLUMN target, DROP COLUMN mode, DROP COLUMN source,
            DROP COLUMN result_count
        """
    )
