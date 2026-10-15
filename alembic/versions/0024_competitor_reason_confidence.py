"""tracked_competitors: add reason + confidence (for AI-discovered competitors)

The LLM competitor finder returns a short reason and a confidence level
("Very High"/"High"/"Medium"/"Low") per competitor. Store them on the curated
list so the UI can show why each was suggested.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tracked_competitors
            ADD COLUMN reason varchar(600),
            ADD COLUMN confidence varchar(20);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE tracked_competitors
            DROP COLUMN reason,
            DROP COLUMN confidence;
        """
    )
