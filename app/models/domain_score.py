"""Whole-site SEO score: an async scoring job + each stored domain score.

A domain score aggregates a capped sample of the domain's pages (per-page
overall scores, reused from page_snapshots when fresh) plus site-wide technical
signals (robots/sitemap/HTTPS/indexable coverage) into one 0-100. The POST
route starts a background worker that fills a DomainScoreJob's progress and, on
success, inserts a DomainScore row (kept for the trend). Mirrors the
page_discovery / competitor_discovery job pattern.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DomainScoreJob(Base):
    """Async whole-site scoring run with live step progress (UI polls this)."""

    __tablename__ = "domain_score_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'running'"))  # running|done|error
    step: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    step_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pages_total: Mapped[int | None] = mapped_column(Integer, nullable=True)   # sample size chosen
    pages_scored: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # The score id produced on success (so the UI can fetch the result directly).
    result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DomainScore(Base):
    """One computed whole-site score for a domain (kept for history/trend)."""

    __tablename__ = "domain_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    overall: Mapped[int] = mapped_column(Integer, nullable=False)
    page_quality: Mapped[int] = mapped_column(Integer, nullable=False)
    site_technical: Mapped[int] = mapped_column(Integer, nullable=False)
    # Mean of each of the six per-page categories across the sample {cat_id: 0-100}.
    category_averages: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Site-wide technical checks [{id,label,status,detail}] for display.
    site_checks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Per-page results in the sample [{url,page_id,score,page_type}] (worst-first use).
    pages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    pages_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))       # discovered pages
    pages_scored: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))       # sample size
    pages_indexable: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    weak_share: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))         # fraction scoring < 50
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
