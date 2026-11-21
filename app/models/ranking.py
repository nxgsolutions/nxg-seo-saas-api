"""Rank tracking models: tracked keywords and their position entries."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin


class TrackedKeyword(Base, SoftDeleteMixin):
    __tablename__ = "tracked_keywords"
    __table_args__ = (
        UniqueConstraint("domain_id", "term", name="uq_domain_term"),
        # The scheduler's due-query: WHERE tracking_enabled AND next_fetch_at <= now().
        Index("ix_tracked_keywords_due", "tracking_enabled", "next_fetch_at"),
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
    # Link to the shared keyword library so the list can show volume / difficulty
    # / intent from keyword_metrics. Nullable: a term may have no canonical row yet.
    keyword_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("keywords.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ── Automated SERP tracking (scheduling + status) ─────────────────────────
    # Whether the scheduler should keep fetching this keyword's SERP.
    tracking_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Cadence in days (15 or 30). The next fetch is scheduled this far ahead.
    tracking_frequency_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("15")
    )
    # idle | pending | processing | error. 'processing' is a lock that prevents a
    # second overlapping fetch for the same keyword.
    tracking_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'idle'")
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the next scheduled SERP fetch is due. Set to now() on add so the first
    # fetch happens immediately (sandbox) or on the next click/run (live).
    next_fetch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last provider error message (kept separate from a genuine "not ranking").
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    entries: Mapped[list["RankEntry"]] = relationship(
        back_populates="keyword",
        cascade="all, delete-orphan",
        order_by="RankEntry.recorded_at",
    )


class RankEntry(Base):
    __tablename__ = "rank_entries"
    __table_args__ = (
        UniqueConstraint("keyword_id", "recorded_at", name="uq_keyword_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracked_keywords.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Search position; null means "not in the tracked range" (Not Ranking).
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[date] = mapped_column(Date, nullable=False)
    # Our ranking URL at this position (from the SERP snapshot); null when unknown.
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where this position came from: serp | gsc | manual.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'serp'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    keyword: Mapped["TrackedKeyword"] = relationship(back_populates="entries")
