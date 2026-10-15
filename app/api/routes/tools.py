"""SEO tools routes."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import upstream_error
from app.db.session import get_db
from app.models.user import User
from app.schemas.audit import AuditCheck, AuditRequest, AuditResponse
from app.schemas.content import (
    KeywordAnalysisRequest,
    KeywordAnalysisResponse,
)
from app.schemas.crawl import RobotsCheckRequest, RobotsCheckResponse
from app.schemas.keyword_ideas import KeywordIdeasRequest, KeywordIdeasResponse
from app.schemas.keyword_planner import KeywordPlannerRequest, KeywordPlannerResponse
from app.schemas.ai import (
    AiSearchRequest,
    AiSearchResponse,
    MetaSuggestRequest,
    MetaSuggestResponse,
)
from app.schemas.optimization import OptimizeRequest, OptimizeResponse
from app.schemas.psi import PsiRequest, PsiResponse
from app.services import (
    ai_service,
    audit_service,
    content_service,
    crawl_service,
    keyword_ideas_service,
    keyword_planner_service,
    meta_service,
    optimization_service,
    psi_service,
)
from app.services.keyword_planner_service import NotConfiguredError, PlannerError
from app.services.ai_service import AiUnavailableError
from app.services.psi_service import PsiUnavailableError
from app.services.audit_service import (
    BlockedUrlError,
    FetchError,
    InvalidUrlError,
)

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post(
    "/page-audit",
    response_model=AuditResponse,
    summary="Audit a web page for SEO and performance signals",
    responses={
        400: {"description": "The page could not be fetched"},
        422: {"description": "Invalid or disallowed URL"},
    },
)
def page_audit(
    payload: AuditRequest,
    _current_user: User = Depends(get_current_user),
) -> AuditResponse:
    """Fetch the given URL server-side and return a scored audit."""
    try:
        result = audit_service.run_audit(payload.url, payload.device)
    except (InvalidUrlError, BlockedUrlError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except FetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )

    return AuditResponse(
        url=result.url,
        final_url=result.final_url,
        status_code=result.status_code,
        load_time_ms=result.load_time_ms,
        page_size_bytes=result.page_size_bytes,
        score=result.score,
        checks=[
            AuditCheck(id=c.id, label=c.label, status=c.status, detail=c.detail)
            for c in result.checks
        ],
        metrics=result.metrics,
    )


@router.post(
    "/robots-check",
    response_model=RobotsCheckResponse,
    summary="Check a site's robots.txt and XML sitemap",
    responses={
        400: {"description": "The site could not be fetched"},
        422: {"description": "Invalid or disallowed URL"},
    },
)
def robots_check(
    payload: RobotsCheckRequest,
    _current_user: User = Depends(get_current_user),
) -> RobotsCheckResponse:
    """Fetch robots.txt + the site's sitemap and analyze crawlability."""
    try:
        result = crawl_service.run_check(payload.url)
    except (InvalidUrlError, BlockedUrlError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except FetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )

    return RobotsCheckResponse(
        url=result.url,
        origin=result.origin,
        robots=result.robots,
        sitemap=result.sitemap,
        checks=[
            AuditCheck(id=c.id, label=c.label, status=c.status, detail=c.detail)
            for c in result.checks
        ],
    )


@router.post(
    "/keyword-analysis",
    response_model=KeywordAnalysisResponse,
    summary="Analyze a page's keywords and optionally a target keyword",
    responses={
        400: {"description": "The page could not be fetched"},
        422: {"description": "Invalid or disallowed URL"},
    },
)
def keyword_analysis(
    payload: KeywordAnalysisRequest,
    _current_user: User = Depends(get_current_user),
) -> KeywordAnalysisResponse:
    """Fetch a page and return its top keyword phrases + optional target report."""
    try:
        result = content_service.run_analysis(payload.url, payload.term)
    except (InvalidUrlError, BlockedUrlError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except FetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )

    return KeywordAnalysisResponse(
        url=result.url,
        word_count=result.word_count,
        unigrams=result.unigrams,
        bigrams=result.bigrams,
        trigrams=result.trigrams,
        target=result.target,
    )


@router.post(
    "/pagespeed",
    response_model=PsiResponse,
    summary="Run Google PageSpeed Insights (Core Web Vitals) for a URL",
    responses={
        422: {"description": "Invalid URL"},
        502: {"description": "PageSpeed Insights unavailable / rate-limited"},
    },
)
def pagespeed(
    payload: PsiRequest,
    _current_user: User = Depends(get_current_user),
) -> PsiResponse:
    """Fetch real Lighthouse lab metrics + CrUX field data from Google PSI."""
    try:
        result = psi_service.run_psi(payload.url, payload.strategy)
    except InvalidUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except PsiUnavailableError as exc:
        raise upstream_error(exc, "Speed data isn't available right now. Please try again later.") from exc
    return PsiResponse(**result)


@router.post(
    "/ai-search",
    response_model=AiSearchResponse,
    summary="Analyze AI-search (GEO/AEO) visibility for a topic/brand",
    responses={503: {"description": "AI provider unavailable"}},
)
def ai_search(
    payload: AiSearchRequest,
    _current_user: User = Depends(get_current_user),
) -> AiSearchResponse:
    """Return an AI-written AI-search visibility analysis (gpt-4o-mini; 503 when no AI is available)."""
    try:
        result = ai_service.analyze_ai_search(payload.query, payload.domain)
    except AiUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    return AiSearchResponse(**result)


@router.post(
    "/meta-suggest",
    response_model=MetaSuggestResponse,
    summary="AI-suggest an SEO title + meta description for a topic or URL",
)
def meta_suggest(
    payload: MetaSuggestRequest,
    _current_user: User = Depends(get_current_user),
) -> MetaSuggestResponse:
    """Generate an optimized title + meta description (OpenAI gpt-4o-mini, local model fallback)."""
    try:
        return MetaSuggestResponse(**meta_service.suggest(payload.input))
    except meta_service.MetaUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.post(
    "/optimize",
    response_model=OptimizeResponse,
    summary="Get a prioritized optimization plan for a page",
    responses={
        400: {"description": "The page could not be fetched"},
        422: {"description": "Invalid or disallowed URL"},
    },
)
def optimize(
    payload: OptimizeRequest,
    _current_user: User = Depends(get_current_user),
) -> OptimizeResponse:
    """Audit the page and return prioritized, actionable recommendations."""
    try:
        result = optimization_service.optimize(payload.url, payload.device)
    except (InvalidUrlError, BlockedUrlError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except FetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )
    return OptimizeResponse(**result)


@router.post(
    "/keyword-ideas",
    response_model=KeywordIdeasResponse,
    summary="Free keyword ideas from Google Autocomplete (no paid provider)",
)
def keyword_ideas(
    payload: KeywordIdeasRequest,
    _current_user: User = Depends(get_current_user),
) -> KeywordIdeasResponse:
    """Categorized keyword ideas (autocomplete / related / questions /
    prepositions / comparisons) with rule-based intent + difficulty. Keyless and
    free; volume/CPC are null until a real volume source is configured."""
    result = keyword_ideas_service.keyword_ideas(
        payload.seed, payload.country, payload.language
    )
    return KeywordIdeasResponse(**result)


@router.post(
    "/keyword-planner",
    response_model=KeywordPlannerResponse,
    summary="Raw keyword metrics from Google Ads Keyword Planner (volume/CPC/competition)",
    responses={502: {"description": "Keyword Planner request failed"}},
)
def keyword_planner(
    payload: KeywordPlannerRequest,
    _current_user: User = Depends(get_current_user),
) -> KeywordPlannerResponse:
    """RAW Google Ads Keyword Planner data — real avg monthly searches, competition
    and CPC. `mode`: 'keyword' (seed), 'url' (one page) or 'site' (whole domain).
    Returns configured=False with setup guidance when credentials aren't set."""
    try:
        result = keyword_planner_service.generate_ideas(
            payload.input, mode=payload.mode, country=payload.country, language=payload.language
        )
    except NotConfiguredError as exc:
        return KeywordPlannerResponse(configured=False, message="Keyword data isn't available right now. Please try again later.", mode=payload.mode, input=payload.input)
    except PlannerError as exc:
        raise upstream_error(exc, "This data isn't available right now. Please try again later.") from exc
    return KeywordPlannerResponse(**result)


@router.post(
    "/keyword-planner/history",
    response_model=KeywordPlannerResponse,
    summary="Monthly search volumes for the selected keyword",
)
def keyword_history(
    payload: KeywordPlannerRequest,
    _current_user: User = Depends(get_current_user),
) -> KeywordPlannerResponse:
    keyword = payload.input.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="Enter a keyword.")
    try:
        rows = keyword_planner_service.historical_metrics(
            [keyword], country=payload.country, language=payload.language
        )
    except NotConfiguredError as exc:
        return KeywordPlannerResponse(
            configured=False, message="Connect Google Ads to view monthly search volumes.",
            input=keyword, country=payload.country, language=payload.language,
        )
    except PlannerError as exc:
        raise upstream_error(exc, "This data isn't available right now. Please try again later.") from exc
    return KeywordPlannerResponse(
        configured=True, input=keyword, country=payload.country,
        language=payload.language, count=len(rows), rows=rows,
    )
