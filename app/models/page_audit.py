"""PageAudit ORM model — a saved page audit result (workspace-scoped)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PageAudit(Base):
    __tablename__ = "page_audits"

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
    # Who ran it. Kept even if the user is later removed from the workspace.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    load_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    page_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Genuine overall SEO score + per-category breakdown + blocking problems,
    # computed by page_score_service.compose at save time (migration 0051). One
    # saved audit powers the score, the breakdown and the fixes. Nullable: audits
    # saved before 0051 have none.
    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_scores: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    blockers: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    workspace = relationship("Workspace")

    @property
    def overall(self) -> int | None:
        """The genuine overall score for history lists (alias of overall_score)."""
        return self.overall_score

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PageAudit id={self.id} score={self.score} url={self.url!r}>"
