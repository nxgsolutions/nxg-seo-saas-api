"""domain settings — per-domain market + niche config

Per-domain overrides so the app serves ANY niche/market, not just the original
UPSC/IAS + India + English build:
  - country / language: the domain's market; NULL falls back to the workspace
    default (migration 0039), then to the global in/en.
  - topic_terms: comma-separated on-topic terms (auto-derived from the site on
    create, user-editable). NULL/empty => no topic filtering (nothing dropped).
  - brand_terms: comma-separated competitor/own brands to exclude from targets.
  - is_local: when true, keep "near me"/"nearby" keywords (local businesses).

Additive + nullable => non-breaking. No back-fill: existing domains start
generic (reset), so nothing is filtered for topic reasons until a niche is set.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE domains
            ADD COLUMN country varchar(2) NULL,
            ADD COLUMN language varchar(10) NULL,
            ADD COLUMN topic_terms text NULL,
            ADD COLUMN brand_terms text NULL,
            ADD COLUMN is_local boolean NOT NULL DEFAULT false;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE domains
            DROP COLUMN IF EXISTS country,
            DROP COLUMN IF EXISTS language,
            DROP COLUMN IF EXISTS topic_terms,
            DROP COLUMN IF EXISTS brand_terms,
            DROP COLUMN IF EXISTS is_local;
        """
    )
