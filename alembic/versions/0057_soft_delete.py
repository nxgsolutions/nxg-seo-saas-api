"""soft delete — deleted_at on user-facing data entities

Adds a reversible soft-delete marker (deleted_at timestamptz, null = live) to the
five user-deletable data entities: domains, tracked_competitors, saved_keywords,
tracked_keywords and categories. A global ORM session filter
(app/db/session.py) excludes rows where deleted_at IS NOT NULL from every read,
so deleting these now stamps deleted_at instead of physically removing the row —
preserving keywords, competitors, rankings, SERP/opportunity/score history and
allowing restore. Additive and nullable -> non-breaking; existing rows stay live.

A partial index on (deleted_at IS NULL) keeps the common "live rows" scans fast.

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "domains",
    "tracked_competitors",
    "saved_keywords",
    "tracked_keywords",
    "categories",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN IF NOT EXISTS deleted_at timestamptz;"
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_deleted_at "
            f"ON {table} (deleted_at);"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_deleted_at;")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS deleted_at;")
