"""create keyword_rank_history table

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "keyword_rank_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("keyword", sa.String(length=200), nullable=False),
        sa.Column("location", sa.String(length=100), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("checked_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("keyword", "location", "domain", "checked_on", name="uq_rank_point"),
    )
    op.create_index(op.f("ix_keyword_rank_history_keyword"), "keyword_rank_history", ["keyword"])
    op.create_index(op.f("ix_keyword_rank_history_domain"), "keyword_rank_history", ["domain"])


def downgrade() -> None:
    op.drop_index(op.f("ix_keyword_rank_history_domain"), table_name="keyword_rank_history")
    op.drop_index(op.f("ix_keyword_rank_history_keyword"), table_name="keyword_rank_history")
    op.drop_table("keyword_rank_history")
