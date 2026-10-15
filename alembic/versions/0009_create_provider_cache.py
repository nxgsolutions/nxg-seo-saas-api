"""create provider_cache table

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("cache_key", sa.String(length=400), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="dataforseo"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cache_key", name="uq_provider_cache_key"),
    )
    op.create_index(op.f("ix_provider_cache_cache_key"), "provider_cache", ["cache_key"])


def downgrade() -> None:
    op.drop_index(op.f("ix_provider_cache_cache_key"), table_name="provider_cache")
    op.drop_table("provider_cache")
