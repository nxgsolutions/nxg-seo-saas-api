"""site_pages: Shopify link columns; shopify_push_log

Links a discovered page to the Shopify resource it came from (product / page)
so it can be edited and published back, and records every publish (before /
after) for audit and undo. All new site_pages columns are nullable -> additive.

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE site_pages
            ADD COLUMN external_source varchar(20),
            ADD COLUMN external_type varchar(20),
            ADD COLUMN external_id varchar(100),
            ADD COLUMN external_handle varchar(255),
            ADD COLUMN external_updated_at timestamptz,
            ADD COLUMN external_meta jsonb,
            ADD COLUMN seo_title text,
            ADD COLUMN seo_description text,
            ADD COLUMN published boolean;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_site_pages_domain_external ON site_pages (domain_id, external_id) "
        "WHERE external_id IS NOT NULL;"
    )
    op.execute(
        """
        CREATE TABLE shopify_push_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            page_id uuid NOT NULL REFERENCES site_pages(id) ON DELETE CASCADE,
            user_id uuid REFERENCES users(id) ON DELETE SET NULL,
            external_id varchar(100) NOT NULL,
            action varchar(20) NOT NULL DEFAULT 'publish',
            before jsonb NOT NULL DEFAULT '{}'::jsonb,
            after jsonb NOT NULL DEFAULT '{}'::jsonb,
            status varchar(20) NOT NULL,
            error text,
            redirect_created boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX ix_shopify_push_log_page ON shopify_push_log (page_id, created_at DESC);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS shopify_push_log;")
    op.execute("DROP INDEX IF EXISTS uq_site_pages_domain_external;")
    op.execute(
        """
        ALTER TABLE site_pages
            DROP COLUMN IF EXISTS published,
            DROP COLUMN IF EXISTS seo_description,
            DROP COLUMN IF EXISTS seo_title,
            DROP COLUMN IF EXISTS external_meta,
            DROP COLUMN IF EXISTS external_updated_at,
            DROP COLUMN IF EXISTS external_handle,
            DROP COLUMN IF EXISTS external_id,
            DROP COLUMN IF EXISTS external_type,
            DROP COLUMN IF EXISTS external_source;
        """
    )
