"""tracked_competitors: LLM relevance match score (0-100)

Competitor discovery now returns a numeric match score per competitor (how
closely their PRIMARY business overlaps the target's), used to rank the list and
shown in the UI alongside the confidence band. Additive and nullable (null =
not scored, e.g. manual adds) -> non-breaking.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tracked_competitors ADD COLUMN match_score integer;")


def downgrade() -> None:
    op.execute("ALTER TABLE tracked_competitors DROP COLUMN IF EXISTS match_score;")
