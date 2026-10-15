"""drop unused tables: keyword_rankings, keyword_clusters, keyword_cluster_members

- keyword_rankings: redundant with the working tracked_keywords/rank_entries tracker.
- keyword_clusters / keyword_cluster_members: clustering feature never wired up.
All three were empty. Downgrade recreates them (structure only) from 0013's shape.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-11
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS keyword_cluster_members;")
    op.execute("DROP TABLE IF EXISTS keyword_clusters;")
    op.execute("DROP TABLE IF EXISTS keyword_rankings;")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE keyword_clusters (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id         UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            name              VARCHAR(255) NOT NULL,
            description       TEXT,
            parent_cluster_id UUID REFERENCES keyword_clusters(id) ON DELETE SET NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_cluster_name UNIQUE (domain_id, name)
        );
        CREATE INDEX ix_keyword_clusters_domain_id ON keyword_clusters (domain_id);
        CREATE INDEX ix_keyword_clusters_parent_cluster_id ON keyword_clusters (parent_cluster_id);

        CREATE TABLE keyword_cluster_members (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cluster_id         UUID NOT NULL REFERENCES keyword_clusters(id) ON DELETE CASCADE,
            domain_keyword_id  UUID NOT NULL REFERENCES domain_keywords(id) ON DELETE CASCADE,
            score              NUMERIC(8, 4),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_cluster_member UNIQUE (cluster_id, domain_keyword_id)
        );
        CREATE INDEX ix_keyword_cluster_members_cluster_id ON keyword_cluster_members (cluster_id);
        CREATE INDEX ix_keyword_cluster_members_domain_keyword_id ON keyword_cluster_members (domain_keyword_id);

        CREATE TABLE keyword_rankings (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_keyword_id  UUID NOT NULL REFERENCES domain_keywords(id) ON DELETE CASCADE,
            host_id            UUID NOT NULL REFERENCES domain_hosts(id) ON DELETE CASCADE,
            position           NUMERIC(8, 2),
            impressions        BIGINT,
            clicks             BIGINT,
            ctr                NUMERIC(8, 4),
            ranking_date       DATE NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_keyword_ranking UNIQUE (domain_keyword_id, host_id, ranking_date)
        );
        CREATE INDEX ix_keyword_rankings_domain_keyword_id ON keyword_rankings (domain_keyword_id);
        CREATE INDEX ix_keyword_rankings_host_id ON keyword_rankings (host_id);
        """
    )
