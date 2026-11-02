"""competitor_discovery_jobs: proposed-competitor result payload

"Find competitors again" now runs in preview mode: it discovers candidates
WITHOUT overwriting the user's current list, then asks the user to confirm
merging the new ones in. The background worker needs somewhere to park the
proposed candidates so the UI can read them when the job finishes — this adds a
nullable jsonb `result` column for that payload.

Additive and nullable (null = legacy job / non-preview run) -> non-breaking.

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE competitor_discovery_jobs ADD COLUMN result jsonb;")


def downgrade() -> None:
    op.execute("ALTER TABLE competitor_discovery_jobs DROP COLUMN IF EXISTS result;")
