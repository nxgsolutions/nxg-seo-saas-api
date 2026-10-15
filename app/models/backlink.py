"""Backlink data (DataForSEO Backlinks API), stored per fetch.

Global, not tenant-scoped (a public domain's backlinks are the same for every
workspace); tagged with `environment` so sandbox dummy data never mixes with
live data. One run = one stored fetch; child rows are that run's snapshot.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text,
    func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

KIND_PROFILE = "profile"
KIND_GAP = "gap"


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _run_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("backlink_runs.id", ondelete="CASCADE"),
                         nullable=False)  # indexed in migration 0033


class BacklinkRun(Base):
    __tablename__ = "backlink_runs"

    id: Mapped[uuid.UUID] = _id()
    environment: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'live'"))
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # profile | gap
    # profile: the domain profiled. gap: YOUR domain (excluded from the intersection).
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    competitors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    rank: Mapped[int | None] = mapped_column(Integer)
    backlinks: Mapped[int | None] = mapped_column(BigInteger)
    referring_domains: Mapped[int | None] = mapped_column(Integer)
    referring_domains_nofollow: Mapped[int | None] = mapped_column(Integer)
    referring_main_domains: Mapped[int | None] = mapped_column(Integer)
    referring_ips: Mapped[int | None] = mapped_column(Integer)
    broken_backlinks: Mapped[int | None] = mapped_column(Integer)
    spam_score: Mapped[int | None] = mapped_column(SmallInteger)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Provider total for the list (gap: all matching domains; we store the top rows).
    total_count: Mapped[int | None] = mapped_column(Integer)
    items_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    requests: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    cost: Mapped[float | None] = mapped_column(Numeric(10, 5))  # USD, provider-reported
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BacklinkReferringDomain(Base):
    __tablename__ = "backlink_referring_domains"

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[uuid.UUID] = _run_fk()
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    backlinks: Mapped[int | None] = mapped_column(BigInteger)
    nofollow_backlinks: Mapped[int | None] = mapped_column(BigInteger)
    spam_score: Mapped[int | None] = mapped_column(SmallInteger)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacklinkLink(Base):
    __tablename__ = "backlink_links"

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[uuid.UUID] = _run_fk()
    url_from: Mapped[str] = mapped_column(Text, nullable=False)
    domain_from: Mapped[str | None] = mapped_column(String(255))
    url_to: Mapped[str | None] = mapped_column(Text)
    anchor: Mapped[str | None] = mapped_column(Text)
    item_type: Mapped[str | None] = mapped_column(String(20))
    dofollow: Mapped[bool | None] = mapped_column(Boolean)
    is_new: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_lost: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_broken: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    domain_from_rank: Mapped[int | None] = mapped_column(Integer)
    page_from_rank: Mapped[int | None] = mapped_column(Integer)
    page_from_title: Mapped[str | None] = mapped_column(Text)
    spam_score: Mapped[int | None] = mapped_column(SmallInteger)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacklinkAnchor(Base):
    __tablename__ = "backlink_anchors"

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[uuid.UUID] = _run_fk()
    anchor: Mapped[str] = mapped_column(Text, nullable=False)
    backlinks: Mapped[int | None] = mapped_column(BigInteger)
    referring_domains: Mapped[int | None] = mapped_column(Integer)
    referring_domains_nofollow: Mapped[int | None] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer)
    spam_score: Mapped[int | None] = mapped_column(SmallInteger)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacklinkGapDomain(Base):
    __tablename__ = "backlink_gap_domains"

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[uuid.UUID] = _run_fk()
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    competitors_linking: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    # {competitor: backlinks from this domain}
    links: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    spam_score: Mapped[int | None] = mapped_column(SmallInteger)
