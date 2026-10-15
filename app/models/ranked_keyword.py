"""Keywords a domain actually ranks for (DataForSEO Labs ranked_keywords).

Global, not tenant-scoped (a public domain's rankings are the same for every
workspace); tagged with `environment` so sandbox dummy data never mixes with
live data. One run = one paid fetch; rows are that run's snapshot.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, Numeric, SmallInteger,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DomainRankedKeywordRun(Base):
    __tablename__ = "domain_ranked_keyword_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    environment: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'live'"))
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    location_code: Mapped[int] = mapped_column(Integer, nullable=False)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'dataforseo_labs'"))
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # Labs' count of ALL keywords the domain ranks for (we store only top-N).
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost: Mapped[float | None] = mapped_column(Numeric(10, 5), nullable=True)  # USD, provider-reported
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    keywords: Mapped[list["DomainRankedKeyword"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")


class DomainRankedKeyword(Base):
    __tablename__ = "domain_ranked_keywords"
    __table_args__ = (UniqueConstraint("run_id", "keyword", name="uq_ranked_keyword_run"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domain_ranked_keyword_runs.id", ondelete="CASCADE"),
        nullable=False, index=True)
    # Canonical keyword (live rows only — sandbox dummy keywords stay out of it).
    keyword_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="SET NULL"),
        nullable=True, index=True)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    rank_absolute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_rank_absolute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_new: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_up: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_down: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    serp_item_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    relative_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    etv: Mapped[float | None] = mapped_column(Float, nullable=True)  # est. monthly traffic
    search_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpc: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    competition: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..1 (Ads)
    keyword_difficulty: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    intent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    serp_features: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    monthly: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    run: Mapped["DomainRankedKeywordRun"] = relationship(back_populates="keywords")
