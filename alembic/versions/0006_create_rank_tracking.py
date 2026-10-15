"""create tracked_keywords and rank_entries tables

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tracked_keywords",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("website_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("term", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("website_id", "term", name="uq_website_term"),
    )
    op.create_index(
        op.f("ix_tracked_keywords_website_id"), "tracked_keywords", ["website_id"]
    )

    op.create_table(
        "rank_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("keyword_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["keyword_id"], ["tracked_keywords.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("keyword_id", "recorded_at", name="uq_keyword_date"),
    )
    op.create_index(
        op.f("ix_rank_entries_keyword_id"), "rank_entries", ["keyword_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rank_entries_keyword_id"), table_name="rank_entries")
    op.drop_table("rank_entries")
    op.drop_index(
        op.f("ix_tracked_keywords_website_id"), table_name="tracked_keywords"
    )
    op.drop_table("tracked_keywords")
