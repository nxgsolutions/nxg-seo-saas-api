"""backlink_runs + referring domains / links / anchors / gap domains (DataForSEO Backlinks)

One run = one stored fetch. kind='profile' is a domain's backlink profile
(summary + top referring domains, backlinks and anchors — 4 requests);
kind='gap' is a link gap: domains linking to the competitors but not to
`target` (backlinks/domain_intersection — 1 request).

Global (not tenant-scoped) like domain_ranked_keyword_runs: a public domain's
backlinks are the same for every workspace, so one fetch is shared. Tagged with
`environment` ('live' | 'sandbox') — every read filters on it, so sandbox dummy
data never shows in a live workspace. Rows are a per-run snapshot (history
accumulates); the provider-reported cost is stored on the run.

New tables only — no existing table is altered.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE backlink_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            environment varchar(10) NOT NULL DEFAULT 'live',
            kind varchar(10) NOT NULL,
            target varchar(255) NOT NULL,
            competitors jsonb NOT NULL DEFAULT '[]'::jsonb,
            rank integer,
            backlinks bigint,
            referring_domains integer,
            referring_domains_nofollow integer,
            referring_main_domains integer,
            referring_ips integer,
            broken_backlinks integer,
            spam_score smallint,
            first_seen timestamptz,
            total_count integer,
            items_count integer NOT NULL DEFAULT 0,
            requests smallint NOT NULL DEFAULT 0,
            cost numeric(10, 5),
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_backlink_runs_kind CHECK (kind IN ('profile', 'gap'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_backlink_runs_lookup ON backlink_runs "
        "(environment, kind, target, fetched_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE backlink_referring_domains (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES backlink_runs(id) ON DELETE CASCADE,
            domain varchar(255) NOT NULL,
            rank integer,
            backlinks bigint,
            nofollow_backlinks bigint,
            spam_score smallint,
            first_seen timestamptz,
            lost_date timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_backlink_ref_domains_run ON backlink_referring_domains (run_id)")
    op.execute(
        """
        CREATE TABLE backlink_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES backlink_runs(id) ON DELETE CASCADE,
            url_from text NOT NULL,
            domain_from varchar(255),
            url_to text,
            anchor text,
            item_type varchar(20),
            dofollow boolean,
            is_new boolean NOT NULL DEFAULT false,
            is_lost boolean NOT NULL DEFAULT false,
            is_broken boolean NOT NULL DEFAULT false,
            domain_from_rank integer,
            page_from_rank integer,
            page_from_title text,
            spam_score smallint,
            first_seen timestamptz,
            last_seen timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_backlink_links_run ON backlink_links (run_id)")
    op.execute(
        """
        CREATE TABLE backlink_anchors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES backlink_runs(id) ON DELETE CASCADE,
            anchor text NOT NULL,
            backlinks bigint,
            referring_domains integer,
            referring_domains_nofollow integer,
            rank integer,
            spam_score smallint,
            first_seen timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_backlink_anchors_run ON backlink_anchors (run_id)")
    op.execute(
        """
        CREATE TABLE backlink_gap_domains (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES backlink_runs(id) ON DELETE CASCADE,
            domain varchar(255) NOT NULL,
            rank integer,
            competitors_linking smallint NOT NULL DEFAULT 0,
            links jsonb NOT NULL DEFAULT '{}'::jsonb,
            spam_score smallint
        )
        """
    )
    op.execute("CREATE INDEX ix_backlink_gap_run ON backlink_gap_domains (run_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS backlink_gap_domains")
    op.execute("DROP TABLE IF EXISTS backlink_anchors")
    op.execute("DROP TABLE IF EXISTS backlink_links")
    op.execute("DROP TABLE IF EXISTS backlink_referring_domains")
    op.execute("DROP TABLE IF EXISTS backlink_runs")
