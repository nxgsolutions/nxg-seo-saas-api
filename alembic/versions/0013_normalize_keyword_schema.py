"""normalize keyword schema: global keywords + website_keywords + hosts + rankings

Restructures the first-party keyword domain (created in 0012) into a properly
normalized shape, PRESERVING all existing data:

  * keywords            -> global canonical keyword (website_id / country /
                           language REMOVED; normalized_keyword now UNIQUE).
  * website_keywords    -> NEW junction carrying the per-website targeting
                           (website_id, keyword_id, country, language) that used
                           to live on keywords. Backfilled 1:1 from keywords.
  * website_hosts       -> NEW root/subdomain registry per website. Seeded with a
                           `root` host from websites.domain.
  * keyword_metrics     -> SUPERSET: keeps the first-party signal columns (GSC,
                           intent, opportunity/relevance/confidence, trend_index)
                           AND adds market columns (country/language/engine/
                           device, search_volume, cpc, competition, trend).
                           captured_on renamed -> metric_date.
  * keyword_rankings    -> NEW per (website_keyword, host, date) SERP/GSC rank row.
  * keyword_sources     -> source_type widened, source_reference -> TEXT.
  * keyword_relations   -> widened, + score(8,4) + created_at + related index.
  * keyword_clusters    -> name widened, + UNIQUE(website_id, name) + parent idx.
  * keyword_cluster_members -> rebuilt with UUID PK + website_keyword_id (was
                           keyword_id) + created_at (table was empty).
  * keyword_researches  -> seed_keyword text replaced by seed_keyword_id FK,
                           status widened, + completed_at.
  * keyword_research_results -> UUID PK + created_at + UNIQUE(research_id,
                           keyword_id) (was a composite PK).

Every foreign key gets an index.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-08
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# lower + collapse internal whitespace — mirrors keyword_scoring.normalize_keyword.
_NORM = r"regexp_replace(lower(btrim({col})), '\s+', ' ', 'g')"


def upgrade() -> None:
    # ── 1. website_hosts (NEW) ────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE website_hosts (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            website_id     UUID NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
            host           TEXT NOT NULL,
            host_type      VARCHAR(20) NOT NULL DEFAULT 'root',
            parent_host_id UUID REFERENCES website_hosts(id) ON DELETE CASCADE,
            is_primary     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_website_host UNIQUE (website_id, host),
            CONSTRAINT ck_host_type CHECK (host_type IN ('root', 'subdomain'))
        );
        CREATE INDEX ix_website_hosts_website_id ON website_hosts (website_id);
        CREATE INDEX ix_website_hosts_parent_host_id ON website_hosts (parent_host_id);
        """
    )
    # Seed one primary root host per website from its (normalized) domain.
    op.execute(
        """
        INSERT INTO website_hosts (website_id, host, host_type, is_primary)
        SELECT w.id,
               regexp_replace(
                   regexp_replace(lower(btrim(w.domain)), '^https?://(www\\.)?', ''),
                   '/.*$', ''),
               'root', TRUE
        FROM websites w
        ON CONFLICT (website_id, host) DO NOTHING;
        """
    )

    # ── 2. website_keywords (NEW) — carries per-website targeting ─────────────
    op.execute(
        """
        CREATE TABLE website_keywords (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            website_id    UUID NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
            keyword_id    UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
            country_code  VARCHAR(2),
            language_code VARCHAR(10),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_website_keyword
                UNIQUE (website_id, keyword_id, country_code, language_code)
        );
        CREATE INDEX ix_website_keywords_website_id ON website_keywords (website_id);
        CREATE INDEX ix_website_keywords_keyword_id ON website_keywords (keyword_id);
        """
    )
    # Backfill 1:1 from the current (website-scoped) keywords.
    op.execute(
        """
        INSERT INTO website_keywords (website_id, keyword_id, country_code, language_code, created_at)
        SELECT k.website_id, k.id, k.country_code, k.language_code, k.created_at
        FROM keywords k
        ON CONFLICT ON CONSTRAINT uq_website_keyword DO NOTHING;
        """
    )

    # ── 3. keywords -> global canonical keyword ───────────────────────────────
    op.execute(
        """
        ALTER TABLE keywords DROP CONSTRAINT IF EXISTS uq_keyword_scope;
        DROP INDEX IF EXISTS ix_keywords_website_id;
        DROP INDEX IF EXISTS ix_keywords_normalized_keyword;
        ALTER TABLE keywords DROP COLUMN website_id;
        ALTER TABLE keywords DROP COLUMN country_code;
        ALTER TABLE keywords DROP COLUMN language_code;
        ALTER TABLE keywords
            ADD CONSTRAINT uq_keywords_normalized UNIQUE (normalized_keyword);
        """
    )

    # ── 4. keyword_metrics -> SUPERSET (first-party + market columns) ─────────
    op.execute(
        """
        ALTER TABLE keyword_metrics DROP CONSTRAINT IF EXISTS uq_metric_snapshot;
        ALTER TABLE keyword_metrics RENAME COLUMN captured_on TO metric_date;

        ALTER TABLE keyword_metrics
            ALTER COLUMN difficulty TYPE NUMERIC(5, 2) USING difficulty::numeric,
            ALTER COLUMN trend_change_pct TYPE NUMERIC(12, 4) USING trend_change_pct::numeric;

        ALTER TABLE keyword_metrics
            ADD COLUMN country_code  VARCHAR(2)  NOT NULL DEFAULT 'us',
            ADD COLUMN language_code VARCHAR(10) NOT NULL DEFAULT 'en',
            ADD COLUMN search_engine VARCHAR(50) NOT NULL DEFAULT 'google',
            ADD COLUMN device        VARCHAR(20) NOT NULL DEFAULT 'desktop',
            ADD COLUMN search_volume BIGINT,
            ADD COLUMN cpc           NUMERIC(12, 4),
            ADD COLUMN competition   NUMERIC(5, 2),
            ADD COLUMN trend         NUMERIC(12, 4);

        ALTER TABLE keyword_metrics
            ADD CONSTRAINT uq_keyword_metric
            UNIQUE (keyword_id, country_code, language_code, search_engine, device, metric_date);
        """
    )

    # ── 5. keyword_rankings (NEW) ─────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE keyword_rankings (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            website_keyword_id UUID NOT NULL REFERENCES website_keywords(id) ON DELETE CASCADE,
            host_id            UUID NOT NULL REFERENCES website_hosts(id) ON DELETE CASCADE,
            position           NUMERIC(8, 2),
            impressions        BIGINT,
            clicks             BIGINT,
            ctr                NUMERIC(8, 4),
            ranking_date       DATE NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_keyword_ranking UNIQUE (website_keyword_id, host_id, ranking_date)
        );
        CREATE INDEX ix_keyword_rankings_website_keyword_id ON keyword_rankings (website_keyword_id);
        CREATE INDEX ix_keyword_rankings_host_id ON keyword_rankings (host_id);
        """
    )

    # ── 6. keyword_sources -> widen ───────────────────────────────────────────
    op.execute(
        """
        ALTER TABLE keyword_sources
            ALTER COLUMN source_type TYPE VARCHAR(50),
            ALTER COLUMN source_reference TYPE TEXT;
        """
    )

    # ── 7. keyword_relations -> widen + score(8,4) + created_at + FK index ────
    op.execute(
        """
        ALTER TABLE keyword_relations
            ALTER COLUMN relation_type TYPE VARCHAR(50),
            ALTER COLUMN score TYPE NUMERIC(8, 4) USING score::numeric,
            ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();
        CREATE INDEX ix_keyword_relations_related_keyword_id
            ON keyword_relations (related_keyword_id);
        """
    )

    # ── 8. keyword_clusters -> widen name + UNIQUE + parent FK index ──────────
    op.execute(
        """
        ALTER TABLE keyword_clusters
            ALTER COLUMN name TYPE VARCHAR(255),
            ADD CONSTRAINT uq_cluster_name UNIQUE (website_id, name);
        CREATE INDEX ix_keyword_clusters_parent_cluster_id
            ON keyword_clusters (parent_cluster_id);
        """
    )

    # ── 9. keyword_cluster_members -> rebuild on website_keyword_id (was empty) ─
    op.execute(
        """
        DROP TABLE keyword_cluster_members;
        CREATE TABLE keyword_cluster_members (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cluster_id         UUID NOT NULL REFERENCES keyword_clusters(id) ON DELETE CASCADE,
            website_keyword_id UUID NOT NULL REFERENCES website_keywords(id) ON DELETE CASCADE,
            score              NUMERIC(8, 4),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_cluster_member UNIQUE (cluster_id, website_keyword_id)
        );
        CREATE INDEX ix_keyword_cluster_members_cluster_id
            ON keyword_cluster_members (cluster_id);
        CREATE INDEX ix_keyword_cluster_members_website_keyword_id
            ON keyword_cluster_members (website_keyword_id);
        """
    )

    # ── 10. keyword_researches -> seed_keyword_id FK + completed_at ───────────
    op.execute("ALTER TABLE keyword_researches ADD COLUMN seed_keyword_id UUID;")
    # Any seed with no matching global keyword yet: create one.
    op.execute(
        f"""
        INSERT INTO keywords (keyword, normalized_keyword)
        SELECT DISTINCT btrim(r.seed_keyword), {_NORM.format(col='r.seed_keyword')}
        FROM keyword_researches r
        ON CONFLICT (normalized_keyword) DO NOTHING;
        """
    )
    op.execute(
        f"""
        UPDATE keyword_researches r
        SET seed_keyword_id = k.id
        FROM keywords k
        WHERE k.normalized_keyword = {_NORM.format(col='r.seed_keyword')};
        """
    )
    op.execute(
        """
        ALTER TABLE keyword_researches
            ALTER COLUMN seed_keyword_id SET NOT NULL,
            ADD CONSTRAINT fk_research_seed_keyword
                FOREIGN KEY (seed_keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
            DROP COLUMN seed_keyword,
            ALTER COLUMN status TYPE VARCHAR(30),
            ALTER COLUMN language_code TYPE VARCHAR(10),
            ADD COLUMN completed_at TIMESTAMPTZ;
        UPDATE keyword_researches SET completed_at = created_at WHERE status = 'completed';
        CREATE INDEX ix_keyword_researches_seed_keyword_id
            ON keyword_researches (seed_keyword_id);
        """
    )

    # ── 11. keyword_research_results -> UUID PK + created_at + UNIQUE ─────────
    op.execute(
        """
        ALTER TABLE keyword_research_results
            DROP CONSTRAINT keyword_research_results_pkey,
            ADD COLUMN id UUID NOT NULL DEFAULT gen_random_uuid(),
            ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();
        ALTER TABLE keyword_research_results ADD PRIMARY KEY (id);
        ALTER TABLE keyword_research_results
            ADD CONSTRAINT uq_research_result UNIQUE (research_id, keyword_id);
        CREATE INDEX ix_keyword_research_results_research_id
            ON keyword_research_results (research_id);
        CREATE INDEX ix_keyword_research_results_keyword_id
            ON keyword_research_results (keyword_id);
        """
    )


def downgrade() -> None:
    """Best-effort reverse. Restores structure and re-derives keywords.website_id/
    country/language from website_keywords; market-only columns and hosts/rankings
    are dropped (they had no pre-0013 equivalent)."""
    # 11. research_results back to composite PK.
    op.execute(
        """
        DROP INDEX IF EXISTS ix_keyword_research_results_keyword_id;
        DROP INDEX IF EXISTS ix_keyword_research_results_research_id;
        ALTER TABLE keyword_research_results DROP CONSTRAINT IF EXISTS uq_research_result;
        ALTER TABLE keyword_research_results DROP CONSTRAINT keyword_research_results_pkey;
        ALTER TABLE keyword_research_results DROP COLUMN id, DROP COLUMN created_at;
        ALTER TABLE keyword_research_results ADD PRIMARY KEY (research_id, keyword_id);
        """
    )
    # 10. researches back to seed_keyword text.
    op.execute(
        """
        DROP INDEX IF EXISTS ix_keyword_researches_seed_keyword_id;
        ALTER TABLE keyword_researches ADD COLUMN seed_keyword VARCHAR(200);
        UPDATE keyword_researches r SET seed_keyword = k.keyword
            FROM keywords k WHERE k.id = r.seed_keyword_id;
        ALTER TABLE keyword_researches
            ALTER COLUMN seed_keyword SET NOT NULL,
            DROP CONSTRAINT fk_research_seed_keyword,
            DROP COLUMN seed_keyword_id,
            DROP COLUMN completed_at,
            ALTER COLUMN status TYPE VARCHAR(20),
            ALTER COLUMN language_code TYPE VARCHAR(2);
        """
    )
    # 9. cluster_members back to composite PK on keyword_id.
    op.execute(
        """
        DROP TABLE keyword_cluster_members;
        CREATE TABLE keyword_cluster_members (
            cluster_id UUID NOT NULL REFERENCES keyword_clusters(id) ON DELETE CASCADE,
            keyword_id UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
            score NUMERIC(5, 2),
            PRIMARY KEY (cluster_id, keyword_id)
        );
        """
    )
    # 8. clusters.
    op.execute(
        """
        DROP INDEX IF EXISTS ix_keyword_clusters_parent_cluster_id;
        ALTER TABLE keyword_clusters DROP CONSTRAINT IF EXISTS uq_cluster_name;
        ALTER TABLE keyword_clusters ALTER COLUMN name TYPE VARCHAR(200);
        """
    )
    # 7. relations.
    op.execute(
        """
        DROP INDEX IF EXISTS ix_keyword_relations_related_keyword_id;
        ALTER TABLE keyword_relations
            DROP COLUMN created_at,
            ALTER COLUMN score TYPE NUMERIC(5, 2),
            ALTER COLUMN relation_type TYPE VARCHAR(20);
        """
    )
    # 6. sources.
    op.execute(
        """
        ALTER TABLE keyword_sources
            ALTER COLUMN source_reference TYPE VARCHAR(500),
            ALTER COLUMN source_type TYPE VARCHAR(32);
        """
    )
    # 5. rankings.
    op.execute("DROP TABLE keyword_rankings;")
    # 4. metrics back to first-party-only + captured_on.
    op.execute(
        """
        ALTER TABLE keyword_metrics DROP CONSTRAINT IF EXISTS uq_keyword_metric;
        ALTER TABLE keyword_metrics
            DROP COLUMN country_code, DROP COLUMN language_code, DROP COLUMN search_engine,
            DROP COLUMN device, DROP COLUMN search_volume, DROP COLUMN cpc,
            DROP COLUMN competition, DROP COLUMN trend,
            ALTER COLUMN difficulty TYPE SMALLINT USING round(difficulty)::smallint,
            ALTER COLUMN trend_change_pct TYPE SMALLINT USING round(trend_change_pct)::smallint;
        ALTER TABLE keyword_metrics RENAME COLUMN metric_date TO captured_on;
        ALTER TABLE keyword_metrics
            ADD CONSTRAINT uq_metric_snapshot UNIQUE (keyword_id, captured_on);
        """
    )
    # 3. keywords back to website-scoped (re-derive from website_keywords).
    op.execute(
        """
        ALTER TABLE keywords DROP CONSTRAINT IF EXISTS uq_keywords_normalized;
        ALTER TABLE keywords
            ADD COLUMN website_id UUID,
            ADD COLUMN country_code VARCHAR(2) NOT NULL DEFAULT 'us',
            ADD COLUMN language_code VARCHAR(2) NOT NULL DEFAULT 'en';
        UPDATE keywords k SET website_id = wk.website_id,
                              country_code = wk.country_code,
                              language_code = wk.language_code
            FROM (SELECT DISTINCT ON (keyword_id) keyword_id, website_id, country_code, language_code
                  FROM website_keywords ORDER BY keyword_id, created_at) wk
            WHERE wk.keyword_id = k.id;
        DELETE FROM keywords WHERE website_id IS NULL;
        ALTER TABLE keywords
            ALTER COLUMN website_id SET NOT NULL,
            ADD CONSTRAINT keywords_website_id_fkey
                FOREIGN KEY (website_id) REFERENCES websites(id) ON DELETE CASCADE,
            ADD CONSTRAINT uq_keyword_scope
                UNIQUE (website_id, normalized_keyword, country_code, language_code);
        CREATE INDEX ix_keywords_website_id ON keywords (website_id);
        CREATE INDEX ix_keywords_normalized_keyword ON keywords (normalized_keyword);
        """
    )
    # 2 & 1. drop junctions.
    op.execute("DROP TABLE website_keywords;")
    op.execute("DROP TABLE website_hosts;")
