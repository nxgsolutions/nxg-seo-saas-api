"""Competitor rank tracking: per-competitor SERP standing captured over time.

Each row is one competitor's organic standing for a domain on a given date,
derived from `serp_snapshots` at refresh time. A row per
(domain_id, competitor, captured_on) forms the time series behind the
rank-change chart. `is_simulated` marks backfilled demo history, which real
refreshes replace as they accrue.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TrackedCompetitor(Base):
    """A user-curated competitor for a domain — the editable source of truth for
    WHO is tracked. Seeded from serp_snapshots (source='auto') on first refresh,
    then the user can add/remove entries (source='manual'). The rank time series
    in `competitor_rank_snapshots` is captured for the competitors listed here."""

    __tablename__ = "tracked_competitors"
    __table_args__ = (
        UniqueConstraint("domain_id", "competitor", name="uq_tracked_competitor"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    competitor: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional friendly name shown in the UI (defaults to the host).
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 'auto' = seeded from SERP data; 'manual' = added by the user;
    # 'ai' = discovered by the LLM competitor finder.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'manual'")
    )
    # LLM-provided rationale + confidence ("Very High"/"High"/"Medium"/"Low"),
    # populated for source='ai'.
    reason: Mapped[str | None] = mapped_column(String(600), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompetitorRankSnapshot(Base):
    __tablename__ = "competitor_rank_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "domain_id", "competitor", "captured_on",
            name="uq_competitor_domain_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Competitor registrable host, e.g. "vajiramandravi.com".
    competitor: Mapped[str] = mapped_column(String(255), nullable=False)
    captured_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Average organic SERP position across the domain's scraped keywords (rank;
    # lower is better). best_position is the single best rank seen.
    avg_position: Mapped[float] = mapped_column(Float, nullable=False)
    best_position: Mapped[int] = mapped_column(Integer, nullable=False)
    # How many of the domain's keywords this competitor ranks for (share of voice).
    keywords_shared: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default=text("'in'")
    )
    language_code: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'en'")
    )
    is_simulated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
