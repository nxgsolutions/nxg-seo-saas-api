"""First-party Keyword Analysis domain (normalized in migration 0013).

Keywords are GLOBAL canonical rows (unique by `normalized_keyword`); per-domain
targeting lives in `domain_keywords`, and root/subdomain hosts in
`domain_hosts`. Demand is read from Google Search Console (impressions = our
"volume"); expansion comes from Google Autocomplete / PAA / related searches;
relative demand from Google Trends; difficulty from our own SERP crawl. Scores
are computed by a pure rule engine (`keyword_scoring.py`), never fetched.

`keyword_metrics` is a SUPERSET: it keeps the first-party signal columns (GSC,
intent, opportunity/relevance/confidence, trend_index) AND carries market
columns (search_volume, cpc, competition, trend) for when a paid provider fills
them. Kept separate from the DataForSEO-backed `tools` flow so both coexist.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Closed sets, mirrored by the CHECK constraints in the migrations.
INTENTS = ("informational", "navigational", "commercial", "transactional", "unknown")
RESEARCH_STATUSES = ("pending", "running", "completed", "failed")
HOST_TYPES = ("root", "subdomain")
# Open-ish sets, validated at the app/Pydantic layer (plain String columns).
SOURCE_TYPES = ("gsc", "autocomplete", "paa", "related_search", "crawl", "trends", "seed")
RELATION_TYPES = ("related", "variant", "parent", "child", "synonym")


def _uuid_pk() -> Mapped[uuid.UUID]:
    """A fresh UUID primary-key column (one instance per class)."""
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Keyword(Base):
    """Global canonical keyword. Unique by `normalized_keyword` — never
    duplicated across domains (per-domain targeting is `DomainKeyword`)."""

    __tablename__ = "keywords"
    __table_args__ = (
        UniqueConstraint("normalized_keyword", name="uq_keywords_normalized"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    keyword: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_keyword: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    metrics: Mapped[list["KeywordMetric"]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan"
    )
    sources: Mapped[list["KeywordSource"]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan"
    )
    domain_keywords: Mapped[list["DomainKeyword"]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan"
    )


class DomainKeyword(Base):
    """Per-domain targeting of a global keyword, scoped by locale."""

    __tablename__ = "domain_keywords"
    __table_args__ = (
        UniqueConstraint(
            "domain_id", "keyword_id", "country_code", "language_code",
            name="uq_domain_keyword",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    keyword: Mapped["Keyword"] = relationship(back_populates="domain_keywords")


class DomainHost(Base):
    """Root domain or subdomain of a domain (hierarchical via parent_host_id)."""

    __tablename__ = "domain_hosts"
    __table_args__ = (
        UniqueConstraint("domain_id", "host", name="uq_domain_host"),
        CheckConstraint("host_type IN ('root', 'subdomain')", name="ck_host_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    host: Mapped[str] = mapped_column(Text, nullable=False)
    host_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="root")
    parent_host_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domain_hosts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()


class KeywordMetric(Base):
    """Signals for a keyword+locale+engine+device on a given day.

    First-party columns (gsc_*, intent, *_score, trend_index) are computed by the
    pipeline; market columns (search_volume, cpc, competition, trend) are filled
    only when a paid provider is wired. `gsc_impressions` stands in for volume."""

    __tablename__ = "keyword_metrics"
    __table_args__ = (
        UniqueConstraint(
            "keyword_id", "country_code", "language_code", "search_engine",
            "device", "metric_date", name="uq_keyword_metric",
        ),
        CheckConstraint(
            "intent IS NULL OR intent IN "
            "('informational','navigational','commercial','transactional','unknown')",
            name="ck_metric_intent",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, server_default="us")
    language_code: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    search_engine: Mapped[str] = mapped_column(String(50), nullable=False, server_default="google")
    device: Mapped[str] = mapped_column(String(20), nullable=False, server_default="desktop")

    # Market columns (paid-provider fillable).
    search_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpc: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    difficulty: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    competition: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    trend: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    trend_change_pct: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)

    # First-party signal columns (computed, no provider).
    intent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gsc_impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gsc_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gsc_position: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    trend_index: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    opportunity_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    relevance_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)

    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    keyword: Mapped["Keyword"] = relationship(back_populates="metrics")


class KeywordSource(Base):
    """Provenance: where a keyword came from. The audit trail for the pipeline."""

    __tablename__ = "keyword_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    keyword: Mapped["Keyword"] = relationship(back_populates="sources")


class KeywordRelation(Base):
    """Self-referential link between two keywords (related / variant / parent…)."""

    __tablename__ = "keyword_relations"
    __table_args__ = (
        UniqueConstraint(
            "keyword_id", "related_keyword_id", "relation_type", name="uq_keyword_relation"
        ),
        CheckConstraint("keyword_id <> related_keyword_id", name="ck_relation_not_self"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    related_keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    relation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class KeywordResearch(Base):
    """A research run started from a seed keyword (FK to the global keyword)."""

    __tablename__ = "keyword_researches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed')", name="ck_research_status"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Optional: a keyword-only search has no domain (workspace-scoped only).
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    seed_keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, server_default="us")
    language_code: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    # 'keyword' (Search by keywords) | 'domain' (Search by domain)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="keyword")
    # Domain searches: the normalized host searched (own site or a competitor),
    # Ads seed mode ('site' | 'url') and provider ('google_ads').
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _created_at()
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    seed: Mapped["Keyword"] = relationship("Keyword")
    results: Mapped[list["KeywordResearchResult"]] = relationship(
        back_populates="research", cascade="all, delete-orphan"
    )


class KeywordResearchResult(Base):
    """Ranked output of a research run."""

    __tablename__ = "keyword_research_results"
    __table_args__ = (
        UniqueConstraint("research_id", "keyword_id", name="uq_research_result"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    research_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keyword_researches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # Per-run copy of what the provider returned (domain searches), so the run
    # can be reopened exactly without calling Google Ads again.
    search_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    competition: Mapped[str | None] = mapped_column(String(20), nullable=True)
    competition_index: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cpc_low: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    cpc_high: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    seo_difficulty: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    monthly: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    research: Mapped["KeywordResearch"] = relationship(back_populates="results")


class KeywordCluster(Base):
    """A topic cluster: a named group of global keywords. Built lexically from
    data we already have (or later from SERP overlap). Gives keyword_relations a
    higher-level home and feeds the content planner."""

    __tablename__ = "keyword_clusters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    head_keyword_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="SET NULL"),
        nullable=True,
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # How it was built: 'lexical' (token overlap) | 'serp' (SERP overlap).
    source: Mapped[str] = mapped_column(String(30), nullable=False, server_default="lexical")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    members: Mapped[list["KeywordClusterMember"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )


class KeywordClusterMember(Base):
    """A global keyword's membership in a cluster."""

    __tablename__ = "keyword_cluster_members"
    __table_args__ = (
        UniqueConstraint("cluster_id", "keyword_id", name="uq_cluster_member"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keyword_clusters.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    score: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    is_head: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()

    cluster: Mapped["KeywordCluster"] = relationship(back_populates="members")


class SerpSnapshot(Base):
    """One SERP FETCH EVENT: a keyword searched on one engine/device/locale at a
    point in time. The individual results live in `serp_snapshot_items`.

    This is the DataForSEO-style parent (their tasks[].result[]): it holds the
    SERP-level metadata (engine, device, location, se_results_count, check_url)
    and groups the result rows so a whole snapshot can be read, diffed, or
    superseded as a unit. Scalable: query "latest snapshot per keyword" via
    (keyword_id, captured_at), then join items."""

    __tablename__ = "serp_snapshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False,
    )
    # WHICH engine produced this SERP: 'google' | 'bing' | 'duckduckgo'.
    search_engine: Mapped[str] = mapped_column(String(20), nullable=False, server_default="google")
    # HOW it was obtained: 'scrape' | 'dataforseo' | 'gsc' | 'import'.
    source: Mapped[str] = mapped_column(String(30), nullable=False, server_default="scrape")
    # "live" | "sandbox" (DataForSEO Sandbox dummy data) — reads must filter on it.
    environment: Mapped[str] = mapped_column(String(10), nullable=False, server_default="live")
    device: Mapped[str] = mapped_column(String(10), nullable=False, server_default="desktop")
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, server_default="in")
    language_code: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    # DataForSEO-style numeric location (city/region granularity); optional.
    location_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depth: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    items_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    se_results_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    check_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="ok")
    # Full provider payload (DataForSEO task/result, or raw scrape) for audit /
    # re-parse without a schema change.
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    items: Mapped[list["SerpSnapshotItem"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan",
    )


class SerpSnapshotItem(Base):
    """One result within a `serp_snapshots` fetch (DataForSEO's items[] element).

    rank_absolute = position among ALL SERP elements; rank_group = position
    within this item type. Organic is the common case, but `type` + `raw` allow
    storing featured snippets, PAA, etc. without new columns."""

    __tablename__ = "serp_snapshot_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("serp_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(30), nullable=False, server_default="organic")
    rank_absolute: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    rank_group: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    breadcrumb: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    snapshot: Mapped["SerpSnapshot"] = relationship(back_populates="items")
