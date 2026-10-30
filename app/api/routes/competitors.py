"""Competitor tracking routes (domain-scoped).

A dedicated surface for the competitors feature, separate from the read-only
keyword reports:
  * GET  /competitors/tracked  — current competitor list (from serp_snapshots)
  * GET  /competitors/history  — per-competitor rank time series (for the chart)
  * POST /competitors/refresh  — recompute + persist today's real snapshot
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.competitor import (
    BusinessProfileResponse,
    CompetitorCreate,
    CompetitorDiscoverRequest,
    CompetitorDiscoverResponse,
    CompetitorHistoryResponse,
    CompetitorListResponse,
    CompetitorRefreshResponse,
    CompetitorSuggestionsResponse,
    CuratedCompetitor,
    CuratedCompetitorsResponse,
    DiscoveryJobOut,
)
from app.services import competitor_service
from app.services.competitor_service import CompetitorError
from app.services.competitor_discovery_service import DiscoveryError

log = logging.getLogger("app.api")

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}",
    tags=["competitors"],
)


@router.get(
    "/competitors/list",
    response_model=CuratedCompetitorsResponse,
    summary="The user's editable competitor list (with latest rank)",
    responses={404: {"description": "Domain not found"}},
)
def list_curated(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CuratedCompetitorsResponse:
    return CuratedCompetitorsResponse(
        **competitor_service.list_competitors(db, ctx.domain)
    )


@router.post(
    "/competitors/list",
    response_model=CuratedCompetitor,
    status_code=status.HTTP_201_CREATED,
    summary="Add a competitor to the list",
    responses={400: {"description": "Invalid domain"}, 404: {"description": "Domain not found"}},
)
def add_curated(
    payload: CompetitorCreate,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CuratedCompetitor:
    try:
        row = competitor_service.add_competitor(
            db, ctx.domain, payload.domain, payload.label
        )
    except CompetitorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return CuratedCompetitor(**row)


@router.delete(
    "/competitors/list/{competitor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a competitor from the list",
    responses={404: {"description": "Competitor or domain not found"}},
)
def delete_curated(
    competitor_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> None:
    if not competitor_service.remove_competitor(db, ctx.domain, competitor_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Competitor not found"
        )


def _job_out(job) -> DiscoveryJobOut:
    return DiscoveryJobOut(
        job_id=str(job.id), status=job.status, step=job.step,
        step_label=job.step_label, total_steps=job.total_steps,
        count=job.count, error=job.error,
    )


@router.post(
    "/competitors/discover",
    response_model=DiscoveryJobOut,
    summary="Start async competitor discovery; poll the job for live progress",
    responses={404: {"description": "Domain not found"}},
)
def discover(
    payload: CompetitorDiscoverRequest,
    background_tasks: BackgroundTasks,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DiscoveryJobOut:
    """Starts discovery in the background and returns a job to poll. The 3-agent
    pipeline (~1-2 min) runs server-side, updating the job's step as it goes, so a
    long request never ties up (or is reset by) the connection."""
    # If a run is already in flight for this domain, return it instead of starting
    # a duplicate.
    existing = competitor_service.active_discovery_job(db, ctx.domain)
    if existing is not None:
        return _job_out(existing)

    job = competitor_service.create_discovery_job(db, ctx.domain)
    background_tasks.add_task(
        competitor_service.run_discovery_job,
        ctx.workspace.id, ctx.domain.id, job.id,
        target_domain=payload.target_domain, limit=payload.limit,
        extra_services=payload.extra_services, refresh=payload.refresh,
    )
    return _job_out(job)


@router.get(
    "/competitors/discover/{job_id}",
    response_model=DiscoveryJobOut,
    summary="Poll an async discovery job for live step progress",
    responses={404: {"description": "Job not found"}},
)
def discover_status(
    job_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DiscoveryJobOut:
    job = competitor_service.get_discovery_job(db, job_id)
    if job is None or job.domain_id != ctx.domain.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _job_out(job)


@router.get(
    "/competitors/business-profile",
    response_model=BusinessProfileResponse,
    summary="Business profile extracted from the site (first pass of discovery)",
    responses={404: {"description": "Domain not found"}},
)
def business_profile(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> BusinessProfileResponse:
    return BusinessProfileResponse(
        **competitor_service.get_business_profile(db, ctx.domain)
    )


@router.post(
    "/competitors/business-profile/refresh",
    response_model=BusinessProfileResponse,
    summary="Re-extract only the business profile (re-scrape the site)",
    responses={
        404: {"description": "Domain not found"},
        503: {"description": "LLM not configured / extraction failed"},
    },
)
def refresh_business_profile(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> BusinessProfileResponse:
    try:
        result = competitor_service.refresh_business_profile(db, ctx.domain)
        return BusinessProfileResponse(**result)
    except DiscoveryError as exc:
        raise upstream_error(exc, "We couldn't refresh your business profile right now. Please try again in a few minutes.") from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — never leak a raw 500 to the UI
        db.rollback()
        log.exception("business profile refresh failed for domain %s", ctx.domain.id)
        raise upstream_error(exc, "We couldn't refresh your business profile right now. Please try again in a few minutes.") from exc


@router.get(
    "/competitors/suggestions",
    response_model=CompetitorSuggestionsResponse,
    summary="Serp-derived competitors not yet in the list (one-click add)",
    responses={404: {"description": "Domain not found"}},
)
def suggestions(
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=10),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CompetitorSuggestionsResponse:
    return CompetitorSuggestionsResponse(
        **competitor_service.get_suggestions(
            db, ctx.domain, country=country, language=language
        )
    )


@router.get(
    "/competitors/tracked",
    response_model=CompetitorListResponse,
    summary="Current competitors for a domain (from SERP snapshots)",
    responses={404: {"description": "Domain not found"}},
)
def tracked(
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=10),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CompetitorListResponse:
    return CompetitorListResponse(
        **competitor_service.current_competitors(
            db, ctx.domain, country=country, language=language
        )
    )


@router.get(
    "/competitors/history",
    response_model=CompetitorHistoryResponse,
    summary="Competitor rank-change time series",
    responses={404: {"description": "Domain not found"}},
)
def history(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CompetitorHistoryResponse:
    return CompetitorHistoryResponse(**competitor_service.get_history(db, ctx.domain))


@router.post(
    "/competitors/refresh",
    response_model=CompetitorRefreshResponse,
    summary="Refresh competitor ranks (persist today's snapshot)",
    responses={404: {"description": "Domain not found"}},
)
def refresh(
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=10),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CompetitorRefreshResponse:
    return CompetitorRefreshResponse(
        **competitor_service.refresh(
            db, ctx.domain, country=country, language=language
        )
    )
