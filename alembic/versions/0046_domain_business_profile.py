"""domains: cached LLM business profile for competitor discovery

Competitor discovery extracts a business profile (description, industry,
services, audience, market) from the domain's own site before asking the LLM for
competitors. That extraction is the expensive, slow first step. Cache the result
on the domain so a second discovery reuses it instead of re-scraping and
re-extracting; a client can force a fresh extraction with ?refresh=true.

Additive and nullable (null = not extracted yet) -> non-breaking.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE domains ADD COLUMN business_profile jsonb;")
    op.execute("ALTER TABLE domains ADD COLUMN business_profile_at timestamptz;")


def downgrade() -> None:
    op.execute("ALTER TABLE domains DROP COLUMN IF EXISTS business_profile;")
    op.execute("ALTER TABLE domains DROP COLUMN IF EXISTS business_profile_at;")
