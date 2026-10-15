"""domain_analytics_snapshots: backlinks + referring domains

Adds the DataForSEO Backlinks bulk_backlinks / bulk_referring_domains counts to
the whole-site snapshot (nullable: older rows and failed lookups stay NULL).

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE domain_analytics_snapshots "
        "ADD COLUMN backlinks bigint, ADD COLUMN referring_domains integer"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE domain_analytics_snapshots "
        "DROP COLUMN backlinks, DROP COLUMN referring_domains"
    )
