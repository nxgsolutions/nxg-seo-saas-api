"""Monthly whole-site history (DataForSEO Labs historical endpoints).

One row per (target, location, language, year, month): monthly organic estimated
traffic + ranking-keyword count (and position distribution). Global, not
tenant-scoped — a public domain's history is identical for every workspace, so
one fetch is shared and reads filter on `environment` (live vs sandbox).

Distinct from DomainAnalyticsSnapshot (daily, one row per day): this table holds
the real 12-month monthly series that feeds the Competitive Strategy graph.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DomainMonthlyMetric(Base):
    __tablename__ = "domain_monthly_metrics"
    __table_args__ = (
        UniqueConstraint("environment", "target", "location_code", "language_code",
                         "year", "month", name="uq_domain_monthly_metric"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    # "live" | "sandbox" (DataForSEO Sandbox dummy data) — reads must filter on it.
    environment: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'live'"))
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    location_code: Mapped[int] = mapped_column(Integer, nullable=False)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'dfs_historical'"))

    organic_etv: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    organic_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Position distribution (from historical_rank_overview; NULL when only the
    # bulk-traffic endpoint had a given month).
    pos_1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pos_2_3: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pos_4_10: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pos_11_20: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pos_21_30: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
