"""workspace defaults — default country / language / timezone

Per-workspace defaults used across the app (keyword & SERP tools default to the
workspace's country/language; timezone for dates/reports). Additive, with
sensible server defaults → non-breaking.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workspaces
            ADD COLUMN default_country varchar(2) NOT NULL DEFAULT 'in',
            ADD COLUMN default_language varchar(10) NOT NULL DEFAULT 'en',
            ADD COLUMN timezone varchar(64) NOT NULL DEFAULT 'UTC';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE workspaces
            DROP COLUMN IF EXISTS default_country,
            DROP COLUMN IF EXISTS default_language,
            DROP COLUMN IF EXISTS timezone;
        """
    )
