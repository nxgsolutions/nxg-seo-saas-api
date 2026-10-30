"""Domain ORM model (workspace-scoped SEO property)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = (
        UniqueConstraint("workspace_id", "domain", name="uq_workspace_domain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Normalized hostname, e.g. "example.com".
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    # Per-domain market + niche (migration 0043). NULL country/language fall back
    # to the workspace default, then the global in/en (see services/domain_settings.py).
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Comma-separated on-topic terms (auto-derived from the site, user-editable);
    # NULL/empty = no topic filtering. brand_terms = competitor/own brands to drop.
    topic_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Local business: keep "near me"/"nearby" keywords instead of dropping them.
    is_local: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Industry category (migration 0044). NULL => no category-scoped filtering
    # (competitor suggestions fall back to workspace-wide behavior).
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Cached LLM business profile from competitor discovery (migration 0046):
    # {business_description, industry, services, target_audience, market}.
    # NULL = not extracted yet; reused on later discoveries unless refresh=True.
    business_profile: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    business_profile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workspace = relationship("Workspace")
    category = relationship("Category")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Domain id={self.id} domain={self.domain!r}>"
