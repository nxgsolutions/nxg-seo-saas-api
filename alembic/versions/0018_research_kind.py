"""keyword_researches: add `kind` to separate keyword vs domain searches

So the "Search by keywords" and "Search by domain" tabs keep independent
recent-search histories. Existing rows are keyword searches.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-11
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE keyword_researches "
        "ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'keyword';"
    )
    op.execute("CREATE INDEX ix_keyword_researches_kind ON keyword_researches (kind);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_keyword_researches_kind;")
    op.execute("ALTER TABLE keyword_researches DROP COLUMN kind;")
