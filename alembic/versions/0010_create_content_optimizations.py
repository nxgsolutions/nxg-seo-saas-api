"""create content_optimizations table

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_optimizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("model_used", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("content_score", sa.Integer(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_content_optimizations_workspace_id"), "content_optimizations", ["workspace_id"])
    op.create_index(op.f("ix_content_optimizations_created_at"), "content_optimizations", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_content_optimizations_created_at"), table_name="content_optimizations")
    op.drop_index(op.f("ix_content_optimizations_workspace_id"), table_name="content_optimizations")
    op.drop_table("content_optimizations")
