"""Keyword reports (read-only analyses combining GSC + Google Ads data)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from fastapi import HTTPException, status

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.keyword_reports import (
    CompetitorDomainsResponse,
    CompetitorGapResponse,
    StrikingDistanceResponse,
)
from app.services.pagination import PageQuery, page_query, paginate
from app.services import keyword_analysis_service
from app.services.keyword_planner_service import NotConfiguredError, PlannerError

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}",
    tags=["keyword-reports"],
)


@router.get(
    "/striking-distance",
    response_model=StrikingDistanceResponse,
    summary="Keywords this domain ranks #4-20 for (GSC) + real Ads volume — quick wins",
)
def striking_distance(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
    days: int = Query(90, ge=1, le=480),
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=5),
    min_impressions: int = Query(1, ge=0),
    min_volume: int = Query(0, ge=0),
    listing: PageQuery = Depends(page_query),
) -> StrikingDistanceResponse:
    """One page of rows (sort: opportunity | position | impressions | clicks |
    volume | keyword; default opportunity desc; `q` filters by keyword), plus
    slim `points` for every matching keyword (the chart)."""
    result = keyword_analysis_service.striking_distance(
        db, ctx.workspace, ctx.domain,
        days=days, country=country, language=language,
        min_impressions=min_impressions, min_volume=min_volume,
    )
    term = listing.search()
    rows = [r for r in result.get("rows", []) if not term or term in r["keyword"].lower()]
    sort = listing.sort if listing.sort in _STRIKING_SORTS else "opportunity"
    result["points"] = [{k: r.get(k) for k in ("keyword", "position", "impressions", "volume", "opportunity")}
                        for r in rows]
    result["rows"], result["pagination"] = paginate(
        rows, page=listing.page, page_size=listing.page_size,
        sort_key=(lambda r: r["keyword"].lower()) if sort == "keyword" else sort,
        descending=listing.descending)
    return StrikingDistanceResponse(**result)


_STRIKING_SORTS = {"opportunity", "position", "impressions", "clicks", "volume", "keyword"}


@router.get(
    "/competitor-gap",
    response_model=CompetitorGapResponse,
    summary="Keywords a competitor ranks for that you don't (content gap)",
    responses={502: {"description": "Keyword Planner request failed"}},
)
def competitor_gap(
    competitor: str = Query(..., min_length=2, max_length=255),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=5),
    min_volume: int = Query(10, ge=0),
    gaps_only: bool = Query(False),
) -> CompetitorGapResponse:
    try:
        result = keyword_analysis_service.competitor_gap(
            db, ctx.workspace, ctx.domain, competitor,
            country=country, language=language, min_volume=min_volume, gaps_only=gaps_only,
        )
    except NotConfiguredError as exc:
        return CompetitorGapResponse(configured=False, message=str(exc), competitor=competitor)
    except PlannerError as exc:
        raise upstream_error(exc) from exc
    return CompetitorGapResponse(**result)


@router.get(
    "/competitors",
    response_model=CompetitorDomainsResponse,
    summary="Competitor domains ranking for your scraped keywords (from serp_snapshots)",
)
def competitor_domains(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=5),
    limit: int = Query(50, ge=1, le=200),
    min_shared: int = Query(1, ge=1),
) -> CompetitorDomainsResponse:
    result = keyword_analysis_service.competitor_domains(
        db, ctx.domain, country=country, language=language,
        limit=limit, min_shared=min_shared,
    )
    return CompetitorDomainsResponse(**result)
