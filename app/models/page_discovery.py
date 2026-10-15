"""Page Discovery runs — one row per "scan this site for pages" action.

Records how the URL list was obtained (sitemap vs crawl), the counts, and the
previous run's total (for the "+N%" delta). Powers the result banner, the KPI
tiles and progress polling on the Page Discovery page. One run = one scan.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PageDiscoveryRun(Base):
    __tablename__ = "page_discovery_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    # How the URL list was obtained: 'sitemap' | 'crawl' | 'none'.
    source: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    sitemap_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'running' | 'completed' | 'partial' | 'failed'.
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'running'"))
    pages_found: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pages_enriched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Total pages from the previous completed run, for the "+N%" delta.
    prev_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
