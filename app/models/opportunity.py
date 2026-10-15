"""Opportunity Research models (BrightEdge Data Cube-style).

A focused, domain-scoped layer for the Opportunity Research page. It REUSES the
canonical `keywords` table (via keyword_id) and the workspace/domain/tenant
scoping, and adds only what's missing: pages (URLs), per-keyword opportunity
metrics (rank / intent / SERP features / opportunity score / competitors) and a
domain-level trend history for the chart.

Fields map cleanly onto GSC + Google Ads + DataForSEO so the seeded demo data
can later be replaced by real ingestion without changing the API/UI:
  - search_volume / keyword_difficulty / cpc  -> Google Ads / DataForSEO Labs
  - current_position / ranking_url            -> GSC / DataForSEO SERP
  - intent / serp_features                    -> DataForSEO SERP
  - trend rows                                -> aggregated over time
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class OpportunityPage(Base):
    """A ranking URL on the domain that target keywords are grouped under."""

    __tablename__ = "opportunity_pages"
    __table_args__ = (UniqueConstraint("domain_id", "url", name="uq_opp_page_url"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    keywords: Mapped[list["OpportunityKeyword"]] = relationship(
        back_populates="page", cascade="all, delete-orphan")


class OpportunityKeyword(Base):
    """A target keyword with its opportunity metrics for a domain (and page)."""

    __tablename__ = "opportunity_keywords"
    __table_args__ = (
        UniqueConstraint("domain_id", "keyword", name="uq_opp_domain_keyword"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False, index=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunity_pages.id", ondelete="SET NULL"),
        nullable=True, index=True)
    # Reuse the canonical keyword row when available (kept nullable for ingestion).
    keyword_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="SET NULL"), nullable=True)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)

    search_volume: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    yoy_change: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    keyword_difficulty: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    current_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intent: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'informational'"))
    opportunity_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cpc: Mapped[float | None] = mapped_column(Float, nullable=True)
    ranking_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ["sitelinks","reviews","image","snippet","video"]
    serp_features: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # [{"domain": "x.com", "position": 3}, ...]
    competitors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, server_default=text("'in'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    page: Mapped["OpportunityPage | None"] = relationship(back_populates="keywords")


class OpportunityTrend(Base):
    """Domain-level daily/monthly KPIs for the Opportunity Research trend chart."""

    __tablename__ = "opportunity_trend"
    __table_args__ = (UniqueConstraint("domain_id", "captured_on", name="uq_opp_trend_date"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False, index=True)
    captured_on: Mapped[date] = mapped_column(Date, nullable=False)
    organic_traffic: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ranking_keywords: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    serp_opportunities: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
