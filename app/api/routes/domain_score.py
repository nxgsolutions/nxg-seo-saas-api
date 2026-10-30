"""Whole-site (domain) SEO score routes — start a scoring run, poll it, read it."""

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.db.session import get_db
from app.models.domain_score import DomainScoreJob
from app.schemas.domain_score import (
    DomainScoreJobOut,
    DomainScoreView,
)
from app.services import domain_score_service

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}/site-score",
    tags=["site-score"],
)


@router.post("", response_model=DomainScoreJobOut, summary="Score this whole site")
def start_score(
    background_tasks: BackgroundTasks,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DomainScoreJobOut:
    """Start a background whole-site score: samples the domain's discovered pages
    (reusing recent page scores), adds site-wide technical signals, and stores one
    0-100. Poll GET /status; a run already in flight is returned as-is."""
    running = domain_score_service.running_job(db, ctx.domain)
    if running is not None:
        return DomainScoreJobOut.model_validate(running)

    domain_score_service.supersede_stale(db, ctx.domain)
    job = DomainScoreJob(workspace_id=ctx.workspace.id, domain_id=ctx.domain.id, status="running")
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(
        domain_score_service.run_domain_score, ctx.workspace.id, ctx.domain.id, job.id)
    return DomainScoreJobOut.model_validate(job)


@router.get("/status", response_model=DomainScoreJobOut | None, summary="Latest scoring run status")
def status(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DomainScoreJobOut | None:
    job = domain_score_service.latest_job(db, ctx.domain)
    return DomainScoreJobOut.model_validate(job) if job is not None else None


@router.get("", response_model=DomainScoreView, summary="Latest whole-site score + trend")
def get_score(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DomainScoreView:
    latest = domain_score_service.latest_score(db, ctx.domain)
    hist = domain_score_service.history(db, ctx.domain)
    running = domain_score_service.running_job(db, ctx.domain)
    return DomainScoreView(
        latest=latest,
        history=list(reversed(hist)),  # oldest → newest for the trend line
        running=running,
    )
