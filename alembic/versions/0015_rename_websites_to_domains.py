"""rename websites -> domains (tables, columns, constraints, indexes)

App-wide entity rename. Data-preserving pure renames:
  * tables:  websites -> domains, website_keywords -> domain_keywords,
             website_hosts -> domain_hosts
  * columns: website_id -> domain_id (domain_keywords, domain_hosts,
             keyword_clusters, keyword_researches, tracked_keywords);
             website_keyword_id -> domain_keyword_id (keyword_rankings,
             keyword_cluster_members)
  * constraints + indexes renamed to their domain_* equivalents.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-09
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (old, new) pairs, applied in order. Reversed on downgrade.
_TABLES = [
    ("websites", "domains"),
    ("website_keywords", "domain_keywords"),
    ("website_hosts", "domain_hosts"),
]
_COLUMNS = [
    ("domain_keywords", "website_id", "domain_id"),
    ("domain_hosts", "website_id", "domain_id"),
    ("keyword_clusters", "website_id", "domain_id"),
    ("keyword_researches", "website_id", "domain_id"),
    ("tracked_keywords", "website_id", "domain_id"),
    ("keyword_rankings", "website_keyword_id", "domain_keyword_id"),
    ("keyword_cluster_members", "website_keyword_id", "domain_keyword_id"),
]
_CONSTRAINTS = [
    ("domains", "websites_pkey", "domains_pkey"),
    ("domains", "websites_workspace_id_fkey", "domains_workspace_id_fkey"),
    ("domain_keywords", "website_keywords_pkey", "domain_keywords_pkey"),
    ("domain_keywords", "uq_website_keyword", "uq_domain_keyword"),
    ("domain_keywords", "website_keywords_website_id_fkey", "domain_keywords_domain_id_fkey"),
    ("domain_keywords", "website_keywords_keyword_id_fkey", "domain_keywords_keyword_id_fkey"),
    ("domain_hosts", "website_hosts_pkey", "domain_hosts_pkey"),
    ("domain_hosts", "uq_website_host", "uq_domain_host"),
    ("domain_hosts", "website_hosts_website_id_fkey", "domain_hosts_domain_id_fkey"),
    ("domain_hosts", "website_hosts_parent_host_id_fkey", "domain_hosts_parent_host_id_fkey"),
    ("keyword_clusters", "keyword_clusters_website_id_fkey", "keyword_clusters_domain_id_fkey"),
    ("keyword_researches", "keyword_researches_website_id_fkey", "keyword_researches_domain_id_fkey"),
    ("tracked_keywords", "tracked_keywords_website_id_fkey", "tracked_keywords_domain_id_fkey"),
    ("tracked_keywords", "uq_website_term", "uq_domain_term"),
    ("keyword_rankings", "keyword_rankings_website_keyword_id_fkey", "keyword_rankings_domain_keyword_id_fkey"),
    ("keyword_cluster_members", "keyword_cluster_members_website_keyword_id_fkey", "keyword_cluster_members_domain_keyword_id_fkey"),
]
_INDEXES = [
    ("ix_websites_workspace_id", "ix_domains_workspace_id"),
    ("ix_website_keywords_website_id", "ix_domain_keywords_domain_id"),
    ("ix_website_keywords_keyword_id", "ix_domain_keywords_keyword_id"),
    ("ix_website_hosts_website_id", "ix_domain_hosts_domain_id"),
    ("ix_website_hosts_parent_host_id", "ix_domain_hosts_parent_host_id"),
    ("ix_keyword_clusters_website_id", "ix_keyword_clusters_domain_id"),
    ("ix_keyword_researches_website_id", "ix_keyword_researches_domain_id"),
    ("ix_tracked_keywords_website_id", "ix_tracked_keywords_domain_id"),
    ("ix_keyword_rankings_website_keyword_id", "ix_keyword_rankings_domain_keyword_id"),
    ("ix_keyword_cluster_members_website_keyword_id", "ix_keyword_cluster_members_domain_keyword_id"),
]


def upgrade() -> None:
    # tables first, then columns/constraints/indexes reference the NEW names.
    for old, new in _TABLES:
        op.execute(f"ALTER TABLE {old} RENAME TO {new};")
    for table, old, new in _COLUMNS:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new};")
    for table, old, new in _CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};")
    for old, new in _INDEXES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new};")


def downgrade() -> None:
    # Reverse order: rename constraints/columns back while tables still hold the
    # NEW names, then rename the tables back last.
    for old, new in reversed(_INDEXES):
        op.execute(f"ALTER INDEX {new} RENAME TO {old};")
    for table, old, new in reversed(_CONSTRAINTS):
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old};")
    for table, old, new in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {new} TO {old};")
    for old, new in reversed(_TABLES):
        op.execute(f"ALTER TABLE {new} RENAME TO {old};")
