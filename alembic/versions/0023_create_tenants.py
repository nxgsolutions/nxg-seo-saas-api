"""create tenants (parent org above workspaces) + tenant_members

Introduces the top-level account/organization layer. One tenant owns many
workspaces; users get org-level membership via tenant_members; billing gains a
tenant_id (backfilled) while keeping workspace_id so the billing service is
untouched for now.

Backfill is 1:1 and non-destructive: every existing workspace gets its own
tenant (same name/slug/owner), workspaces.tenant_id is linked by slug, and each
workspace's members become tenant members.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Parent tables.
    op.execute(
        """
        CREATE TABLE tenants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name varchar(120) NOT NULL,
            slug varchar(160) NOT NULL UNIQUE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_tenants_slug ON tenants (slug);
        CREATE INDEX ix_tenants_owner_id ON tenants (owner_id);

        CREATE TABLE tenant_members (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role varchar(20) NOT NULL DEFAULT 'member',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_tenant_user UNIQUE (tenant_id, user_id)
        );
        CREATE INDEX ix_tenant_members_tenant_id ON tenant_members (tenant_id);
        CREATE INDEX ix_tenant_members_user_id ON tenant_members (user_id);
        """
    )

    # 2. workspaces.tenant_id (nullable during backfill).
    op.execute("ALTER TABLE workspaces ADD COLUMN tenant_id uuid;")

    # 3. Backfill: one tenant per existing workspace (same name/slug/owner).
    op.execute(
        """
        INSERT INTO tenants (id, name, slug, owner_id, created_at, updated_at)
        SELECT gen_random_uuid(), w.name, w.slug, w.owner_id, w.created_at, w.updated_at
        FROM workspaces w;

        -- Link each workspace to its tenant (slug is unique on both sides).
        UPDATE workspaces w SET tenant_id = t.id
        FROM tenants t WHERE t.slug = w.slug;

        -- Tenant membership: every workspace member becomes a tenant member;
        -- the workspace owner is forced to 'owner'.
        INSERT INTO tenant_members (id, tenant_id, user_id, role, created_at)
        SELECT gen_random_uuid(), w.tenant_id, wm.user_id,
               CASE WHEN wm.user_id = t.owner_id THEN 'owner' ELSE wm.role END,
               now()
        FROM workspace_members wm
        JOIN workspaces w ON w.id = wm.workspace_id
        JOIN tenants t ON t.id = w.tenant_id
        ON CONFLICT (tenant_id, user_id) DO NOTHING;

        -- Ensure the owner is a member even with no workspace_members row.
        INSERT INTO tenant_members (id, tenant_id, user_id, role, created_at)
        SELECT gen_random_uuid(), t.id, t.owner_id, 'owner', now()
        FROM tenants t
        ON CONFLICT (tenant_id, user_id) DO NOTHING;
        """
    )

    # 4. Enforce the FK + NOT NULL now that every workspace is linked.
    op.execute(
        """
        ALTER TABLE workspaces
            ALTER COLUMN tenant_id SET NOT NULL,
            ADD CONSTRAINT fk_workspaces_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants(id) ON DELETE CASCADE;
        CREATE INDEX ix_workspaces_tenant_id ON workspaces (tenant_id);
        """
    )

    # 5. subscriptions.tenant_id (nullable), backfilled from the workspace's tenant.
    op.execute(
        """
        ALTER TABLE subscriptions ADD COLUMN tenant_id uuid
            REFERENCES tenants(id) ON DELETE CASCADE;
        UPDATE subscriptions s SET tenant_id = w.tenant_id
        FROM workspaces w WHERE w.id = s.workspace_id;
        CREATE INDEX ix_subscriptions_tenant_id ON subscriptions (tenant_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_subscriptions_tenant_id;
        ALTER TABLE subscriptions DROP COLUMN IF EXISTS tenant_id;

        DROP INDEX IF EXISTS ix_workspaces_tenant_id;
        ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS fk_workspaces_tenant;
        ALTER TABLE workspaces DROP COLUMN IF EXISTS tenant_id;

        DROP TABLE IF EXISTS tenant_members;
        DROP TABLE IF EXISTS tenants;
        """
    )
