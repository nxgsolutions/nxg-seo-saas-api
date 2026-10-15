"""Manual SERP scraper — its OWN menu/feature, independent of keyword research.

Scrapes a single Google results page for a keyword and stores organic results
in serp_snapshots. Low-volume, manual, ToS-risky (see serp_scrape_service);
errors are mapped to clear HTTP statuses so the UI can tell the user exactly
what happened (blocked vs. network vs. bad input).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_workspace_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.serp import KeywordBreakdownResponse, SerpAnalyzeResponse, SerpScrapeResponse
from app.services import data_env, serp_breakdown_service, serp_scrape_service
from app.services.serp_scrape_service import ScrapeBlockedError, ScrapeError

router = APIRouter(prefix="/workspaces/{workspace_id}/serp", tags=["serp"])


@router.post(
    "/analyze",
    response_model=SerpAnalyzeResponse,
    summary="Analyze a keyword's Google SERP via DataForSEO -> serp_snapshots",
    responses={
        429: {"description": "SERP provider rate-limited the request"},
        502: {"description": "SERP provider error / unreachable"},
    },
)
def analyze_serp(
    keyword: str = Query(..., min_length=1, max_length=200),
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=5),
    depth: int = Query(100, ge=1, le=100),
    persist: bool = Query(True),
    provider: str = Query("dataforseo", pattern="^(dataforseo|scrape)$"),
    refresh: bool = Query(False),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> SerpAnalyzeResponse:
    kw = keyword.strip()
    if not kw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Keyword is empty"
        )
    env = data_env.env_for_workspace(ctx.workspace)

    # Cost control: by default read the STORED snapshot from serp_snapshots (no
    # provider call, no charge). Only call the paid SERP API when refresh=True or
    # nothing is stored yet AND the caller explicitly asked to fetch.
    if not refresh:
        cached = serp_scrape_service.latest_snapshot(
            db, kw, country=country, language=language, env=env
        )
        if cached is not None:
            return SerpAnalyzeResponse(**cached)
        # Nothing stored — return an empty "not fetched yet" state; never auto-fetch.
        return SerpAnalyzeResponse(
            keyword=kw, country=country, language=language, engine="google",
            source=provider, se_results_count=None, check_url=None,
            captured_at=None, count=0, rows=[],
        )

    try:
        result = serp_scrape_service.analyze_keyword(
            db, kw, country=country, language=language, depth=depth,
            persist=persist, provider=provider, env=env,
        )
    except ScrapeBlockedError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except ScrapeError as exc:
        raise upstream_error(exc) from exc
    return SerpAnalyzeResponse(**result)


@router.get(
    "/breakdown",
    response_model=KeywordBreakdownResponse,
    summary="Keyword Breakdown: stored SERP + per-result domain metrics (DB only, no charge)",
)
def keyword_breakdown(
    keyword: str = Query(..., min_length=1, max_length=200),
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=5),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> KeywordBreakdownResponse:
    kw = keyword.strip()
    if not kw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Keyword is empty"
        )
    return KeywordBreakdownResponse(
        **serp_breakdown_service.build(db, ctx.workspace, kw, country=country, language=language)
    )


@router.get(
    "/scrape",
    response_model=SerpScrapeResponse,
    summary="Scrape one Google SERP for a keyword (manual, low-volume) -> serp_snapshots",
    responses={
        429: {"description": "Google blocked the request (CAPTCHA / unusual traffic)"},
        502: {"description": "Could not reach Google"},
    },
)
def scrape_serp(
    keyword: str = Query(..., min_length=1, max_length=200),
    country: str = Query("in", min_length=2, max_length=2),
    language: str = Query("en", min_length=2, max_length=5),
    num: int = Query(20, ge=1, le=30),
    persist: bool = Query(True),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> SerpScrapeResponse:
    kw = keyword.strip()
    if not kw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Keyword is empty"
        )
    try:
        result = serp_scrape_service.scrape_keyword(
            db, kw, country=country, language=language, num=num, persist=persist,
            env=data_env.env_for_workspace(ctx.workspace),
        )
    except ScrapeBlockedError as exc:
        # 429 = Google rate-limited/CAPTCHA'd us. Expected; tell the user to back off.
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except ScrapeError as exc:
        raise upstream_error(exc) from exc
    return SerpScrapeResponse(**result)
