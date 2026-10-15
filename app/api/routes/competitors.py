"""Competitor tracking routes (domain-scoped).

A dedicated surface for the competitors feature, separate from the read-only
keyword reports:
  * GET  /competitors/tracked  — current competitor list (from serp_snapshots)
  * GET  /competitors/history  — per-competitor rank time series (for the chart)
  * POST /competitors/refresh  — recompute + persist today's real snapshot
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.competitor import (
    CompetitorCreate,
    CompetitorDiscoverRequest,
    CompetitorDiscoverResponse,
    CompetitorHistoryResponse,
    CompetitorListResponse,
    CompetitorRefreshResponse,
    CompetitorSuggestionsResponse,
    CuratedCompetitor,
    CuratedCompetitorsResponse,
)
from app.services import competitor_service
from app.services.competitor_service import CompetitorError
from app.services.competitor_discovery_service import DiscoveryError

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


@router.post(
    "/competitors/discover",
    response_model=CompetitorDiscoverResponse,
    summary="LLM: find top competitors for a domain and store them",
    responses={
        404: {"description": "Domain not found"},
        503: {"description": "LLM not configured / discovery failed"},
    },
)
def discover(
    payload: CompetitorDiscoverRequest,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CompetitorDiscoverResponse:
    try:
        result = competitor_service.discover_and_store(
            db, ctx.domain,
            target_domain=payload.target_domain, limit=payload.limit,
            extra_services=payload.extra_services,
        )
    except DiscoveryError as exc:
        raise upstream_error(exc, "We couldn't find competitors right now. Please try again in a few minutes.") from exc
    return CompetitorDiscoverResponse(**result)


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
