"""Page history: a website's pages, timestamped snapshots, daily search data.

Every time a page is fetched (Page Audit, Content Optimization, Title & Meta)
a snapshot is stored with what the page contained at that moment and what
changed since the previous snapshot. Daily Search Console numbers per page are
stored next to it, so "what changed" can be compared with "what happened to
clicks / position afterwards". All private per workspace.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SitePage(Base):
    """One page (URL) of a website, tracked over time."""

    __tablename__ = "site_pages"
    __table_args__ = (UniqueConstraint("workspace_id", "url_key", name="uq_site_page_url"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    # The workspace website this page belongs to (NULL = a page on another site).
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="SET NULL"), nullable=True, index=True)
    # Identity: host without www + path without trailing slash (+ query). Scheme ignored.
    url_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The URL as last fetched (used for Search Console, which needs the exact URL).
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    latest_audit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Page Discovery fields (migration 0038): how the page was found + enrichment.
    discovery_source: Mapped[str | None] = mapped_column(String(10), nullable=True)  # sitemap | crawl
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indexable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    page_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # home|blog|category|tag|content|other
    # Clean main-content word count from the last discovery scan (trafilatura).
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cached editable article HTML, fetched on demand when a page is opened in the
    # content editor (not during discovery — keeps scans light for large sites).
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sitemap_lastmod: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PageSnapshot(Base):
    """What a page contained at one moment, and what changed since the last snapshot."""

    __tablename__ = "page_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("site_pages.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # audit | content | inspect — which tool fetched the page; source_id = its run id.
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    device: Mapped[str | None] = mapped_column(String(10), nullable=True)
    audit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keyword: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # On-page facts (title, meta, h1, headings, word count, links, images …).
    facts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Technical facts only an audit measures (status, canonical, indexable …).
    tech: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Audit checks [{id, label, status}] — for "fixed" / "new issue" detection.
    checks: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Readable main text (capped) — lets the next snapshot measure how much text changed.
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Changes vs the previous snapshot: [{kind, field, label, before, after}].
    changes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class PageSearchDaily(Base):
    """Search Console numbers for one page on one day (stored as fetched)."""

    __tablename__ = "page_search_daily"
    __table_args__ = (UniqueConstraint("page_id", "day", name="uq_page_search_day"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("site_pages.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ctr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    position: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
