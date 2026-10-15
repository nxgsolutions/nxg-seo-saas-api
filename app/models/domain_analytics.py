"""Whole-site domain analytics snapshots (DataForSEO Labs + Backlinks rank).

Global, not tenant-scoped: a public domain's metrics are identical for every
workspace, so one fetch is shared and history accumulates one row per day.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Float, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DomainAnalyticsSnapshot(Base):
    __tablename__ = "domain_analytics_snapshots"
    __table_args__ = (
        UniqueConstraint("environment", "target", "location_code", "language_code",
                         "fetched_on", name="uq_domain_analytics_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    # "live" | "sandbox" (DataForSEO Sandbox dummy data) — reads must filter on it.
    environment: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'live'"))
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    location_code: Mapped[int] = mapped_column(Integer, nullable=False)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    fetched_on: Mapped[date] = mapped_column(
        Date, nullable=False, server_default=text("CURRENT_DATE"))
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'dataforseo_labs'"))

    organic_etv: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    organic_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pos_1: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pos_2_3: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pos_4_10: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pos_11_20: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_new: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_up: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_down: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_lost: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    traffic_cost: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    paid_etv: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    paid_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    domain_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # DataForSEO Backlinks bulk counts (NULL = not fetched / unavailable).
    backlinks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referring_domains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
