"""create plans and subscriptions; seed plans; backfill free subscriptions

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-04

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="usd"),
        sa.Column("interval", sa.String(length=10), nullable=False, server_default="month"),
        sa.Column("features", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stripe_price_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_plans_code"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="free"),
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=255), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_subscription_workspace"),
    )
    op.create_index(op.f("ix_subscriptions_workspace_id"), "subscriptions", ["workspace_id"])

    # --- Seed the default plans ---
    plans_tbl = sa.table(
        "plans",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("price_cents", sa.Integer),
        sa.column("currency", sa.String),
        sa.column("interval", sa.String),
        sa.column("features", postgresql.JSONB),
        sa.column("is_active", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        plans_tbl,
        [
            {
                "id": uuid.uuid4(),
                "code": "free",
                "name": "Free",
                "description": "Everything you need to start optimizing one site.",
                "price_cents": 0,
                "currency": "usd",
                "interval": "month",
                "features": [
                    "1 website",
                    "Page Audit & Optimization",
                    "On-page keyword analysis",
                    "Google Search Console",
                    "Core Web Vitals (PageSpeed)",
                    "Community support",
                ],
                "is_active": True,
                "sort_order": 0,
            },
            {
                "id": uuid.uuid4(),
                "code": "pro",
                "name": "Pro",
                "description": "For growing teams that need keyword and competitor data.",
                "price_cents": 4900,
                "currency": "usd",
                "interval": "month",
                "features": [
                    "Up to 10 websites",
                    "Everything in Free",
                    "AI Search analysis",
                    "Keyword & competitor data (DataForSEO)",
                    "Rank tracking",
                    "Priority email support",
                ],
                "is_active": True,
                "sort_order": 1,
            },
            {
                "id": uuid.uuid4(),
                "code": "enterprise",
                "name": "Enterprise",
                "description": "Custom scale, security and support for large organizations.",
                "price_cents": 0,
                "currency": "usd",
                "interval": "month",
                "features": [
                    "Unlimited websites",
                    "Everything in Pro",
                    "Custom integrations & DataCube",
                    "SSO & audit logs",
                    "Dedicated success manager",
                    "SLA",
                ],
                "is_active": True,
                "sort_order": 2,
            },
        ],
    )

    # --- Backfill: every existing workspace gets a free subscription ---
    op.execute(
        """
        INSERT INTO subscriptions (id, workspace_id, plan_id, status)
        SELECT gen_random_uuid(), w.id, (SELECT id FROM plans WHERE code = 'free'), 'free'
        FROM workspaces w
        WHERE NOT EXISTS (
            SELECT 1 FROM subscriptions s WHERE s.workspace_id = w.id
        )
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_subscriptions_workspace_id"), table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
