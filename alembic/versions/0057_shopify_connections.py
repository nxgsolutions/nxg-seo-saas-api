"""shopify_connections + shopify_webhook_events

One Shopify store per workspace domain, with an encrypted access token, plus a
table of processed webhook ids for idempotent webhook handling.

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE shopify_connections (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            domain_id uuid NOT NULL UNIQUE REFERENCES domains(id) ON DELETE CASCADE,
            shop varchar(255) NOT NULL,
            shop_name varchar(255),
            primary_host varchar(255),
            shop_hosts jsonb NOT NULL DEFAULT '[]'::jsonb,
            access_token_enc text,
            refresh_token_enc text,
            token_expires_at timestamptz,
            scopes text,
            api_version varchar(10) NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'active',
            installed_by uuid REFERENCES users(id) ON DELETE SET NULL,
            installed_at timestamptz NOT NULL DEFAULT now(),
            uninstalled_at timestamptz,
            last_sync_at timestamptz,
            last_sync_error text,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX ix_shopify_connections_workspace_id ON shopify_connections (workspace_id);")
    op.execute("CREATE INDEX ix_shopify_connections_shop ON shopify_connections (shop);")
    op.execute(
        """
        CREATE TABLE shopify_webhook_events (
            webhook_id varchar(100) PRIMARY KEY,
            topic varchar(100) NOT NULL,
            shop varchar(255) NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS shopify_webhook_events;")
    op.execute("DROP TABLE IF EXISTS shopify_connections;")
