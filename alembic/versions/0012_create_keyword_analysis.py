"""create keyword_analysis domain (first-party, no third-party provider)

A parallel keyword domain that draws only on free / first-party signals:
GSC (impressions = our "volume"), Google Autocomplete / PAA / related searches,
Google Trends (relative index) and our own SERP crawl. Kept fully separate from
the existing DataForSEO-backed `tools` flow so the two can coexist.

Lean 8-table set. Absolute search volume is intentionally NOT stored — GSC
impressions stand in for demand, and everything else is a computed signal.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)
_GEN_UUID = sa.text("gen_random_uuid()")
_NOW = sa.text("now()")

# Closed sets are enforced with CHECK constraints (DB-level safety without the
# CREATE TYPE churn of native enums; app/Pydantic layer mirrors these).
_INTENTS = "'informational','navigational','commercial','transactional','unknown'"
_STATUSES = "'pending','running','completed','failed'"


def upgrade() -> None:
    # ---- keywords: canonical keyword, website-scoped -------------------------
    op.create_table(
        "keywords",
        sa.Column("id", UUID, server_default=_GEN_UUID, nullable=False),
        sa.Column("website_id", UUID, nullable=False),
        sa.Column("keyword", sa.String(length=200), nullable=False),
        sa.Column("normalized_keyword", sa.String(length=200), nullable=False),
        sa.Column("country_code", sa.String(length=2), server_default="us", nullable=False),
        sa.Column("language_code", sa.String(length=2), server_default="en", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "website_id", "normalized_keyword", "country_code", "language_code",
            name="uq_keyword_scope",
        ),
    )
    op.create_index(op.f("ix_keywords_website_id"), "keywords", ["website_id"])
    op.create_index(op.f("ix_keywords_normalized_keyword"), "keywords", ["normalized_keyword"])

    # ---- keyword_metrics: time-stamped signals (one snapshot per day) --------
    op.create_table(
        "keyword_metrics",
        sa.Column("id", UUID, server_default=_GEN_UUID, nullable=False),
        sa.Column("keyword_id", UUID, nullable=False),
        sa.Column("intent", sa.String(length=20), nullable=True),
        # GSC impressions stand in for search volume; clicks/position give context.
        sa.Column("gsc_impressions", sa.Integer(), nullable=True),
        sa.Column("gsc_clicks", sa.Integer(), nullable=True),
        sa.Column("gsc_position", sa.Numeric(precision=5, scale=2), nullable=True),
        # Google Trends relative demand, 0-100, plus recent-vs-prior % change.
        sa.Column("trend_index", sa.SmallInteger(), nullable=True),
        sa.Column("trend_change_pct", sa.SmallInteger(), nullable=True),
        # Computed from the live SERP, 0-100.
        sa.Column("difficulty", sa.SmallInteger(), nullable=True),
        sa.Column("opportunity_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("relevance_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("confidence_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("captured_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["keyword_id"], ["keywords.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("keyword_id", "captured_on", name="uq_metric_snapshot"),
        sa.CheckConstraint(f"intent IS NULL OR intent IN ({_INTENTS})", name="ck_metric_intent"),
    )
    op.create_index(op.f("ix_keyword_metrics_keyword_id"), "keyword_metrics", ["keyword_id"])

    # ---- keyword_sources: provenance / audit trail --------------------------
    op.create_table(
        "keyword_sources",
        sa.Column("id", UUID, server_default=_GEN_UUID, nullable=False),
        sa.Column("keyword_id", UUID, nullable=False),
        # gsc | autocomplete | paa | related_search | crawl | trends | seed
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_reference", sa.String(length=500), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["keyword_id"], ["keywords.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_keyword_sources_keyword_id"), "keyword_sources", ["keyword_id"])

    # ---- keyword_relations: related / variant / parent (self-referential) ---
    op.create_table(
        "keyword_relations",
        sa.Column("id", UUID, server_default=_GEN_UUID, nullable=False),
        sa.Column("keyword_id", UUID, nullable=False),
        sa.Column("related_keyword_id", UUID, nullable=False),
        # related | variant | parent | child | synonym
        sa.Column("relation_type", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["keyword_id"], ["keywords.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_keyword_id"], ["keywords.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "keyword_id", "related_keyword_id", "relation_type", name="uq_keyword_relation"
        ),
        sa.CheckConstraint("keyword_id <> related_keyword_id", name="ck_relation_not_self"),
    )
    op.create_index(op.f("ix_keyword_relations_keyword_id"), "keyword_relations", ["keyword_id"])

    # ---- keyword_clusters: topic grouping (hierarchical) --------------------
    op.create_table(
        "keyword_clusters",
        sa.Column("id", UUID, server_default=_GEN_UUID, nullable=False),
        sa.Column("website_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_cluster_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_cluster_id"], ["keyword_clusters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_keyword_clusters_website_id"), "keyword_clusters", ["website_id"])

    # ---- keyword_cluster_members: keyword <-> cluster -----------------------
    op.create_table(
        "keyword_cluster_members",
        sa.Column("cluster_id", UUID, nullable=False),
        sa.Column("keyword_id", UUID, nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["cluster_id"], ["keyword_clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["keyword_id"], ["keywords.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cluster_id", "keyword_id"),
    )

    # ---- keyword_researches: a research run ---------------------------------
    op.create_table(
        "keyword_researches",
        sa.Column("id", UUID, server_default=_GEN_UUID, nullable=False),
        sa.Column("website_id", UUID, nullable=False),
        sa.Column("seed_keyword", sa.String(length=200), nullable=False),
        sa.Column("country_code", sa.String(length=2), server_default="us", nullable=False),
        sa.Column("language_code", sa.String(length=2), server_default="en", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_research_status"),
    )
    op.create_index(op.f("ix_keyword_researches_website_id"), "keyword_researches", ["website_id"])

    # ---- keyword_research_results: run output -------------------------------
    op.create_table(
        "keyword_research_results",
        sa.Column("research_id", UUID, nullable=False),
        sa.Column("keyword_id", UUID, nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["research_id"], ["keyword_researches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["keyword_id"], ["keywords.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("research_id", "keyword_id"),
    )


def downgrade() -> None:
    op.drop_table("keyword_research_results")
    op.drop_index(op.f("ix_keyword_researches_website_id"), table_name="keyword_researches")
    op.drop_table("keyword_researches")
    op.drop_table("keyword_cluster_members")
    op.drop_index(op.f("ix_keyword_clusters_website_id"), table_name="keyword_clusters")
    op.drop_table("keyword_clusters")
    op.drop_index(op.f("ix_keyword_relations_keyword_id"), table_name="keyword_relations")
    op.drop_table("keyword_relations")
    op.drop_index(op.f("ix_keyword_sources_keyword_id"), table_name="keyword_sources")
    op.drop_table("keyword_sources")
    op.drop_index(op.f("ix_keyword_metrics_keyword_id"), table_name="keyword_metrics")
    op.drop_table("keyword_metrics")
    op.drop_index(op.f("ix_keywords_normalized_keyword"), table_name="keywords")
    op.drop_index(op.f("ix_keywords_website_id"), table_name="keywords")
    op.drop_table("keywords")
