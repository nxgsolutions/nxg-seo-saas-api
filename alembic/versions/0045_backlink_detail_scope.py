"""backlink_runs: scope column + site/page detail run kinds

A "Load all links" fetch (Hybrid data depth) stores the complete backlinks for
one referring host or one target page as a normal run, distinguished by
kind='site'|'page' and a non-null `scope` ("site:<host>" | "page:<url>").
Profile/gap runs keep scope NULL. Detail runs reuse the backlink_links table.

Additive: only backlink_runs is altered (new column, widened kind check, new
lookup index). No data is moved.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE backlink_runs ADD COLUMN scope text")
    op.execute("ALTER TABLE backlink_runs DROP CONSTRAINT IF EXISTS ck_backlink_runs_kind")
    op.execute(
        "ALTER TABLE backlink_runs ADD CONSTRAINT ck_backlink_runs_kind "
        "CHECK (kind IN ('profile', 'gap', 'site', 'page'))"
    )
    # Fast "latest detail run for this scope" lookup.
    op.execute(
        "CREATE INDEX ix_backlink_runs_detail ON backlink_runs "
        "(environment, kind, target, scope, fetched_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_backlink_runs_detail")
    op.execute("DELETE FROM backlink_runs WHERE kind IN ('site', 'page')")
    op.execute("ALTER TABLE backlink_runs DROP CONSTRAINT IF EXISTS ck_backlink_runs_kind")
    op.execute(
        "ALTER TABLE backlink_runs ADD CONSTRAINT ck_backlink_runs_kind "
        "CHECK (kind IN ('profile', 'gap'))"
    )
    op.execute("ALTER TABLE backlink_runs DROP COLUMN IF EXISTS scope")
