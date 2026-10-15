"""normalize serp_snapshots into fetch-event + items (scalable SERP storage)

The original `serp_snapshots` (migration 0019) was a single flat table with one
row per result. That can't represent a SERP FETCH as a unit (engine, device,
se_results_count, check_url) and can't distinguish Google/Bing/DuckDuckGo.

This migration splits it into the DataForSEO-style shape, preserving existing
data:
  * serp_snapshots      -> one fetch event (keyword x engine x device x locale
                           x time) with SERP-level metadata + raw JSONB.
  * serp_snapshot_items -> the result rows (rank_absolute/rank_group, domain,
                           url, title, description, breadcrumb, type, raw).

Migration strategy (data-preserving):
  1. Rename the existing per-result table to serp_snapshot_items.
  2. Add the new item columns; copy position -> rank_absolute.
  3. Create the new parent serp_snapshots.
  4. Backfill one parent per (keyword, locale, source, second) group and link
     each item to it.
  5. Enforce FK/NOT NULL, drop the columns that moved to the parent, add indexes.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Existing rows are per-result items -> rename the table.
    op.execute("ALTER TABLE serp_snapshots RENAME TO serp_snapshot_items;")
    # Rename its indexes out of the way (the domain one we keep for items).
    op.execute("ALTER INDEX ix_serp_snapshots_domain RENAME TO ix_serp_snapshot_items_domain;")
    # These two sit on columns we will drop; they auto-drop with the columns, but
    # rename first so the new parent-table index names are free.
    op.execute("ALTER INDEX ix_serp_snapshots_keyword_id RENAME TO ix_ssi_tmp_keyword_id;")
    op.execute("ALTER INDEX ix_serp_snapshots_captured_at RENAME TO ix_ssi_tmp_captured_at;")

    # 2. New item columns.
    op.execute(
        """
        ALTER TABLE serp_snapshot_items
            ADD COLUMN snapshot_id  uuid,
            ADD COLUMN type         varchar(30) NOT NULL DEFAULT 'organic',
            ADD COLUMN rank_absolute smallint,
            ADD COLUMN rank_group   smallint,
            ADD COLUMN description  text,
            ADD COLUMN breadcrumb   text,
            ADD COLUMN raw          jsonb;
        UPDATE serp_snapshot_items SET rank_absolute = position;
        """
    )

    # 3. New parent (fetch-event) table.
    op.execute(
        """
        CREATE TABLE serp_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            keyword_id uuid NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
            search_engine varchar(20) NOT NULL DEFAULT 'google',
            source varchar(30) NOT NULL DEFAULT 'scrape',
            device varchar(10) NOT NULL DEFAULT 'desktop',
            country_code varchar(2) NOT NULL DEFAULT 'in',
            language_code varchar(10) NOT NULL DEFAULT 'en',
            location_code integer,
            depth smallint,
            items_count smallint NOT NULL DEFAULT 0,
            se_results_count bigint,
            check_url text,
            status varchar(20) NOT NULL DEFAULT 'ok',
            raw jsonb,
            captured_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    # 4. Backfill: one parent per (keyword, locale, source, second) group; link items.
    op.execute(
        """
        WITH groups AS (
            SELECT keyword_id, country_code, language_code, source,
                   date_trunc('second', captured_at) AS ts,
                   count(*) AS cnt
            FROM serp_snapshot_items
            GROUP BY keyword_id, country_code, language_code, source,
                     date_trunc('second', captured_at)
        ), ins AS (
            INSERT INTO serp_snapshots
                (keyword_id, search_engine, source, country_code, language_code,
                 items_count, captured_at)
            SELECT g.keyword_id,
                   CASE WHEN g.source = 'duckduckgo' THEN 'duckduckgo'
                        WHEN g.source = 'bing'       THEN 'bing'
                        ELSE 'google' END,
                   CASE WHEN g.source IN ('duckduckgo','bing','google') THEN 'scrape'
                        ELSE g.source END,
                   g.country_code, g.language_code, g.cnt, g.ts
            FROM groups g
            RETURNING id, keyword_id, country_code, language_code, source, captured_at
        )
        UPDATE serp_snapshot_items i
        SET snapshot_id = ins.id
        FROM ins
        WHERE i.keyword_id = ins.keyword_id
          AND i.country_code = ins.country_code
          AND i.language_code = ins.language_code
          AND date_trunc('second', i.captured_at) = ins.captured_at;
        """
    )

    # 5. Enforce constraints, drop moved columns, add indexes.
    op.execute("DELETE FROM serp_snapshot_items WHERE snapshot_id IS NULL;")  # safety
    op.execute(
        """
        ALTER TABLE serp_snapshot_items
            ADD CONSTRAINT fk_ssi_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES serp_snapshots(id) ON DELETE CASCADE;
        ALTER TABLE serp_snapshot_items ALTER COLUMN snapshot_id SET NOT NULL;
        ALTER TABLE serp_snapshot_items ALTER COLUMN rank_absolute SET NOT NULL;
        ALTER TABLE serp_snapshot_items
            DROP COLUMN keyword_id,
            DROP COLUMN country_code,
            DROP COLUMN language_code,
            DROP COLUMN source,
            DROP COLUMN captured_at,
            DROP COLUMN position;
        """
    )
    op.execute(
        """
        CREATE INDEX ix_serp_snapshots_keyword_captured
            ON serp_snapshots (keyword_id, captured_at DESC);
        CREATE INDEX ix_serp_snapshots_engine ON serp_snapshots (search_engine);
        CREATE INDEX ix_serp_snapshots_captured_at ON serp_snapshots (captured_at);
        CREATE INDEX ix_serp_snapshot_items_snapshot
            ON serp_snapshot_items (snapshot_id, rank_absolute);
        """
    )


def downgrade() -> None:
    # Rebuild the original flat serp_snapshots (data-preserving) and drop the
    # normalized tables.
    op.execute(
        """
        CREATE TABLE serp_snapshots_flat (
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
        INSERT INTO serp_snapshots_flat
            (keyword_id, country_code, language_code, position, url, domain,
             title, source, captured_at)
        SELECT s.keyword_id, s.country_code, s.language_code, i.rank_absolute,
               i.url, i.domain, i.title,
               COALESCE(NULLIF(s.search_engine,'google'), s.source), s.captured_at
        FROM serp_snapshot_items i
        JOIN serp_snapshots s ON s.id = i.snapshot_id;
        """
    )
    op.execute("DROP TABLE serp_snapshot_items;")
    op.execute("DROP TABLE serp_snapshots;")
    op.execute("ALTER TABLE serp_snapshots_flat RENAME TO serp_snapshots;")
    op.execute(
        """
        CREATE INDEX ix_serp_snapshots_keyword_id ON serp_snapshots (keyword_id);
        CREATE INDEX ix_serp_snapshots_domain ON serp_snapshots (domain);
        CREATE INDEX ix_serp_snapshots_captured_at ON serp_snapshots (captured_at);
        """
    )
