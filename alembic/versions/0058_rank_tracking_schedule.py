"""rank tracking schedule — scheduling/status on tracked_keywords, url/source on rank_entries

Adds the columns behind automated SERP rank tracking:
  * tracked_keywords: tracking_enabled, tracking_frequency_days (15|30),
    tracking_status (idle|pending|processing|error), last_fetched_at,
    next_fetch_at, last_error, plus an index on (tracking_enabled, next_fetch_at)
    for the scheduler's due-query.
  * rank_entries: url (our ranking URL from the SERP snapshot) and source
    (serp|gsc|manual).

All additive + nullable/defaulted -> non-breaking.

NOTE: the dev DB's alembic_version is stamped ahead of the files on disk
(0058 phantom; see the alembic-version-divergence note), so this migration is
applied to that DB via app/scripts/apply_rank_tracking_schedule.py (idempotent
SQL) rather than `alembic upgrade`. Keep the two in sync.

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tracked_keywords",
        sa.Column("tracking_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "tracked_keywords",
        sa.Column("tracking_frequency_days", sa.SmallInteger(), nullable=False, server_default=sa.text("15")),
    )
    op.add_column(
        "tracked_keywords",
        sa.Column("tracking_status", sa.String(length=16), nullable=False, server_default=sa.text("'idle'")),
    )
    op.add_column("tracked_keywords", sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tracked_keywords", sa.Column("next_fetch_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tracked_keywords", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_index(
        "ix_tracked_keywords_due", "tracked_keywords", ["tracking_enabled", "next_fetch_at"]
    )
    # Make existing tracked keywords due immediately.
    op.execute("UPDATE tracked_keywords SET next_fetch_at = now() WHERE next_fetch_at IS NULL")

    op.add_column("rank_entries", sa.Column("url", sa.Text(), nullable=True))
    op.add_column(
        "rank_entries",
        sa.Column("source", sa.String(length=16), nullable=False, server_default=sa.text("'serp'")),
    )


def downgrade() -> None:
    op.drop_column("rank_entries", "source")
    op.drop_column("rank_entries", "url")
    op.drop_index("ix_tracked_keywords_due", table_name="tracked_keywords")
    op.drop_column("tracked_keywords", "last_error")
    op.drop_column("tracked_keywords", "next_fetch_at")
    op.drop_column("tracked_keywords", "last_fetched_at")
    op.drop_column("tracked_keywords", "tracking_status")
    op.drop_column("tracked_keywords", "tracking_frequency_days")
    op.drop_column("tracked_keywords", "tracking_enabled")
