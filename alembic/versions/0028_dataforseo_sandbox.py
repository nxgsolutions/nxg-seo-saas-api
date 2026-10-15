"""DataForSEO sandbox isolation: workspaces.is_sandbox + environment tags

- workspaces.is_sandbox: a sandbox workspace calls the free DataForSEO Sandbox
  (dummy data) instead of the paid API.
- serp_snapshots.environment / domain_analytics_snapshots.environment
  ('live' | 'sandbox'): these tables are shared across workspaces (per keyword /
  per domain), so every row is tagged and every read filters on it. Existing
  rows are real API data -> 'live'.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE workspaces ADD COLUMN is_sandbox boolean NOT NULL DEFAULT false")

    op.execute(
        "ALTER TABLE serp_snapshots ADD COLUMN environment varchar(10) NOT NULL DEFAULT 'live'"
    )
    op.execute(
        "CREATE INDEX ix_serp_snapshots_env_kw_captured ON serp_snapshots "
        "(environment, keyword_id, captured_at DESC)"
    )

    op.execute(
        "ALTER TABLE domain_analytics_snapshots "
        "ADD COLUMN environment varchar(10) NOT NULL DEFAULT 'live'"
    )
    op.execute("ALTER TABLE domain_analytics_snapshots DROP CONSTRAINT uq_domain_analytics_day")
    op.execute(
        "ALTER TABLE domain_analytics_snapshots ADD CONSTRAINT uq_domain_analytics_day "
        "UNIQUE (environment, target, location_code, language_code, fetched_on)"
    )
    op.execute("DROP INDEX IF EXISTS ix_domain_analytics_lookup")
    op.execute(
        "CREATE INDEX ix_domain_analytics_lookup ON domain_analytics_snapshots "
        "(environment, target, location_code, language_code, fetched_at DESC)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM domain_analytics_snapshots WHERE environment <> 'live'")
    op.execute("DROP INDEX IF EXISTS ix_domain_analytics_lookup")
    op.execute("ALTER TABLE domain_analytics_snapshots DROP CONSTRAINT uq_domain_analytics_day")
    op.execute(
        "ALTER TABLE domain_analytics_snapshots ADD CONSTRAINT uq_domain_analytics_day "
        "UNIQUE (target, location_code, language_code, fetched_on)"
    )
    op.execute(
        "CREATE INDEX ix_domain_analytics_lookup ON domain_analytics_snapshots "
        "(target, location_code, language_code, fetched_at DESC)"
    )
    op.execute("ALTER TABLE domain_analytics_snapshots DROP COLUMN environment")

    op.execute("DELETE FROM serp_snapshots WHERE environment <> 'live'")
    op.execute("DROP INDEX IF EXISTS ix_serp_snapshots_env_kw_captured")
    op.execute("ALTER TABLE serp_snapshots DROP COLUMN environment")

    op.execute("ALTER TABLE workspaces DROP COLUMN is_sandbox")
