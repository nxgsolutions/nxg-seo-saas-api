"""Google Search Console OAuth connection (one per workspace).

Stores the OAuth grant that lets us query the Search Console API on the user's
behalf. A workspace connects one Google account, which may expose several
verified properties (site URLs); individual domains are matched to a property
at query time.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class GscConnection(Base):
    __tablename__ = "gsc_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # One connection per workspace.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # The Google account that granted access (for display).
    google_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # OAuth tokens. The refresh token is long-lived; the access token is
    # short-lived and refreshed on demand. Stored as text (no length cap).
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    workspace = relationship("Workspace")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<GscConnection workspace={self.workspace_id} email={self.google_email!r}>"
