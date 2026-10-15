"""Run a registered job and record it in `job_runs`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.job_run import JobRun

logger = logging.getLogger(__name__)


def run_job(
    db: Session,
    name: str,
    fn: Callable[..., dict],
    *,
    triggered_by: str = "manual",
    **kwargs,
) -> JobRun:
    """Execute `fn(db, **kwargs)` inside a job_runs record.

    The run row is committed as 'running' first, so a crash mid-job still leaves
    a trace. Status ends as 'success', 'partial' (stats['failed'] > 0) or
    'failed' (the job raised)."""
    run = JobRun(job_name=name, triggered_by=triggered_by, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        stats = fn(db, **kwargs)
        run.stats = stats
        run.status = "partial" if stats.get("failed") else "success"
    except Exception as exc:  # noqa: BLE001 - record, then re-raise for the exit code
        db.rollback()
        run = db.get(JobRun, run.id)
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        logger.exception("Job %s failed", name)
        raise

    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run
