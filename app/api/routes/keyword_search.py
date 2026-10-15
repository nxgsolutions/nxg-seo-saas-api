"""Workspace-scoped keyword search (keyword input only, no domain).

The "giant" model: a search grows the GLOBAL keywords + keyword_metrics tables and
is recorded as a workspace search (domain_id NULL). Reuses the same service and
response shape as the domain-scoped keyword-analysis routes.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_workspace_context
from app.api.errors import upstream_error
from app.api.routes.keyword_analysis import _detail, _summary
from app.db.session import get_db
from app.schemas.keyword_analysis import (
    OpportunityPoints, ResearchDetail, ResearchRequest, ResearchResultsPage, ResearchSummary,
)
from app.services.pagination import PageQuery, page_query, paginate
from app.schemas.keyword_planner import KeywordPlannerRequest, KeywordPlannerResponse
from app.services import keyword_analysis_service
from app.services.keyword_planner_service import NotConfiguredError, PlannerError

router = APIRouter(
    prefix="/workspaces/{workspace_id}/keyword-search",
    tags=["keyword-search"],
)


@router.post("/research", response_model=ResearchDetail, summary="Run a keyword-only search")
def run_search(
    payload: ResearchRequest,
    include_results: bool = Query(True, description="false = metadata only; page results via /results"),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> ResearchDetail:
    research = keyword_analysis_service.run_research(
        db, ctx.workspace, None, seed=payload.seed, country=payload.country, language=payload.language
    )
    return _detail(db, research, include_results=include_results)


@router.post("/website", response_model=KeywordPlannerResponse, summary="Search by website (persisted)")
def website_search(
    payload: KeywordPlannerRequest,
    refresh: bool = Query(False, description="Ignore the stored search and query Google Ads again"),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> KeywordPlannerResponse:
    """Google Ads keywords for a domain (free). Returns the newest STORED search
    for the same host + locale unless refresh=true; a fresh search is stored as a
    new run. Returns configured=False (with guidance) when Ads isn't set up."""
    try:
        result = keyword_analysis_service.run_website_search(
            db, ctx.workspace, payload.input, country=payload.country,
            language=payload.language, refresh=refresh,
        )
    except NotConfiguredError as exc:
        return KeywordPlannerResponse(configured=False, message=str(exc), input=payload.input)
    except PlannerError as exc:
        raise upstream_error(exc) from exc
    return KeywordPlannerResponse(**result)


@router.get("/domain-searches", response_model=list[ResearchSummary], summary="List domain searches")
def list_domain_searches(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[ResearchSummary]:
    return [_summary(r) for r in keyword_analysis_service.list_researches(db, ctx.workspace, kind="domain")]


@router.get("/domain-searches/{research_id}", response_model=KeywordPlannerResponse,
            summary="Reopen a stored domain search (DB only, no Google call)")
def get_domain_search(
    research_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> KeywordPlannerResponse:
    result = keyword_analysis_service.get_domain_search(db, ctx.workspace, research_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")
    return KeywordPlannerResponse(**result)


@router.get("/researches", response_model=list[ResearchSummary], summary="List recent searches")
def list_searches(
    kind: str = Query("keyword", pattern="^(keyword|domain|all)$"),
    limit: int = Query(30, ge=1, le=200),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[ResearchSummary]:
    """Recent searches, newest first. kind=all mixes keyword and domain searches
    (domain searches de-duplicated to the newest run per host + locale)."""
    return [_summary(r) for r in keyword_analysis_service.list_researches(
        db, ctx.workspace, None, kind=kind, limit=limit)]


@router.get("/researches/{research_id}", response_model=ResearchDetail, summary="Get one keyword search")
def get_search(
    research_id: str,
    include_results: bool = Query(True, description="false = metadata only; page results via /results"),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> ResearchDetail:
    return _detail(db, _research_or_404(db, ctx, research_id), include_results=include_results)


def _research_or_404(db: Session, ctx: WorkspaceContext, research_id: str):
    research = keyword_analysis_service.get_research(db, ctx.workspace, research_id)
    if research is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")
    return research


_RESULT_SORTS = {"search_volume", "cpc", "competition", "difficulty"}


@router.get("/researches/{research_id}/results", response_model=ResearchResultsPage,
            summary="One page of a keyword search's results (filter + sort server-side)")
def search_results(
    research_id: str,
    category: str = Query("all", max_length=40, description="Idea category, e.g. questions"),
    intent: str = Query("all", max_length=20),
    listing: PageQuery = Depends(page_query),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> ResearchResultsPage:
    """sort = search_volume | cpc | competition | difficulty | keyword (default:
    search_volume desc); `q` filters by keyword text."""
    rows = _detail(db, _research_or_404(db, ctx, research_id)).results
    term = listing.search()
    rows = [r for r in rows
            if (category == "all" or category in r.sources)
            and (intent == "all" or ((r.metric.intent if r.metric else None) or "unknown") == intent)
            and (not term or term in r.keyword.lower())]
    if listing.sort == "keyword":
        key = lambda r: r.keyword.lower()  # noqa: E731
    else:
        field = listing.sort if listing.sort in _RESULT_SORTS else "search_volume"
        key = lambda r: getattr(r.metric, field) if r.metric else None  # noqa: E731
    page_rows, info = paginate(rows, page=listing.page, page_size=listing.page_size,
                               sort_key=key, descending=listing.descending)
    return ResearchResultsPage(rows=page_rows, pagination=info)


@router.get("/researches/{research_id}/points", response_model=OpportunityPoints,
            summary="Slim volume/difficulty points of a keyword search (for the opportunity map)")
def search_points(
    research_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> OpportunityPoints:
    rows = _detail(db, _research_or_404(db, ctx, research_id)).results
    points = [{"id": r.keyword_id, "keyword": r.keyword, "volume": r.metric.search_volume,
               "difficulty": r.metric.difficulty, "cpc": r.metric.cpc}
              for r in rows if r.metric and (r.metric.search_volume or 0) > 0 and r.metric.difficulty is not None]
    return OpportunityPoints(points=points, skipped=len(rows) - len(points))
