"""My Keywords — keywords a user saved for one of their websites.

Saved from any keyword table ("+" menu → Save to My Keywords). Private per
website (domain → workspace). Metrics are NOT copied here: the list joins the
shared keyword_metrics at read time, so numbers stay current.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SavedKeyword(Base):
    __tablename__ = "saved_keywords"
    __table_args__ = (
        UniqueConstraint("domain_id", "normalized_keyword", "country_code", name="uq_saved_keyword"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword: Mapped[str] = mapped_column(String(200), nullable=False)
    # Link to the shared master keyword library (nullable: set when a canonical
    # row exists/created, so metrics and opportunities can join by id).
    keyword_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("keywords.id", ondelete="SET NULL"), nullable=True, index=True)
    # Lower-cased, single-spaced — the identity used for de-duplication.
    normalized_keyword: Mapped[str] = mapped_column(String(200), nullable=False)
    country_code: Mapped[str] = mapped_column(String(8), nullable=False, server_default="in")
    language_code: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    # Where it was saved from (keyword_analysis | competitive_strategy | breakdown | manual …).
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
