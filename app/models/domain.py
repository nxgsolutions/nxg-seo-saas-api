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
from sqlalchemy.dialects.postgresql import UUID
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workspace = relationship("Workspace")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Domain id={self.id} domain={self.domain!r}>"
