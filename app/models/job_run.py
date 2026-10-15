"""Scheduled-job audit log: one row per run of a background data job.

Jobs (see `app/jobs`) are launched by an external scheduler (PM2 cron on the
server) or by hand from the CLI. Each run records when it started/finished, its
outcome, and a small JSON `stats` blob (domains processed, rows written, errors)
so DataCube growth — and any silent gaps — are visible after the fact.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed')",
            name="ck_job_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    job_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 'running' while in flight; 'partial' = finished but some units failed.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'running'")
    )
    # 'scheduler' (cron) or 'manual' (someone ran the CLI by hand).
    triggered_by: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'manual'")
    )
    stats: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
