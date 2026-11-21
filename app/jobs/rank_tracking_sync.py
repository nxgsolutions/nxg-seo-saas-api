"""Automated rank-tracking job — fetch SERPs for tracked keywords that are due.

Driven by the external scheduler (daily is ideal; each keyword is only fetched
when its own next_fetch_at is due, so the 15/30-day cadence is honoured):

    python -m app.jobs rank_tracking_sync --triggered-by scheduler

Spend policy: sandbox keywords are always processed (free DataForSEO sandbox);
live keywords are processed only when RANK_TRACKING_LIVE_ENABLED is on. Every run
is recorded in job_runs via the shared runner.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import rank_tracking_service


def run(db: Session, *, days: int = 28, country: str = "in", language: str = "en") -> dict:
    """Process all due tracked keywords (capped by RANK_TRACKING_MAX_PER_RUN).
    ``days``/``country``/``language`` are accepted for the job CLI contract but
    unused — locale comes from each domain's settings."""
    return rank_tracking_service.run_due(db)
