"""Rank tracking models: tracked keywords and their position entries."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TrackedKeyword(Base):
    __tablename__ = "tracked_keywords"
    __table_args__ = (
        UniqueConstraint("domain_id", "term", name="uq_domain_term"),
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
    # Search position; null means "not in the tracked range".
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    keyword: Mapped["TrackedKeyword"] = relationship(back_populates="entries")
