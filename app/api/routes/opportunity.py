"""Opportunity Research routes (domain-scoped; read-only except the explicit
DataForSEO analytics refresh)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.opportunity import (
    ActionItemsResponse,
    ActionPlanResponse,
    PageInsightResponse,
    TrackKeywordResponse,
    CompetitorAnalyticsResponse,
    CompetitorKeywordsResponse,
    OpportunityChangeResponse,
    OpportunityStrategyResponse,
    OpportunityKeywordsResponse,
    OpportunityOverviewResponse,
    OpportunityPagesResponse,
    OpportunityTrendResponse,
    RankedKeywordsEstimate,
    RankedKeywordsOptions,
    RankedKeywordsResponse,
)
from app.services.pagination import PageQuery, page_query, paginate
from app.services import (
    action_plan_service, data_env, domain_analytics_service, domain_settings,
    opportunity_service, page_insight_service, ranked_keywords_service,
)
from app.services.dataforseo_client import DataForSeoError

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}/opportunity",
    tags=["opportunity"],
)


@router.get("/overview", response_model=OpportunityOverviewResponse,
            summary="KPI cards for Opportunity Research")
def overview(ctx: DomainContext = Depends(get_domain_context),
             db: Session = Depends(get_db)) -> OpportunityOverviewResponse:
    return OpportunityOverviewResponse(**opportunity_service.overview(db, ctx.domain))


@router.get("/trend", response_model=OpportunityTrendResponse,
            summary="Historical trend (organic traffic / ranking keywords / SERP opportunities)")
def trend(ctx: DomainContext = Depends(get_domain_context),
          db: Session = Depends(get_db)) -> OpportunityTrendResponse:
    return OpportunityTrendResponse(**opportunity_service.trend(db, ctx.domain))


@router.get("/keywords", response_model=OpportunityKeywordsResponse,
            summary="Top keyword opportunities (filter/sort/paginate)")
def keywords(
    q: str = Query("", max_length=200),
    intent: str = Query("all"),
    min_volume: int = Query(0, ge=0),
    sort: str = Query("score"),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(8, ge=1, le=100),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> OpportunityKeywordsResponse:
    return OpportunityKeywordsResponse(**opportunity_service.keywords(
        db, ctx.domain, q=q, intent=intent, min_volume=min_volume,
        sort=sort, direction=direction, page=page, page_size=page_size,
    ))


@router.get("/pages", response_model=OpportunityPagesResponse,
            summary="Page-level view: URL -> target keywords")
def pages(ctx: DomainContext = Depends(get_domain_context),
          db: Session = Depends(get_db)) -> OpportunityPagesResponse:
    return OpportunityPagesResponse(**opportunity_service.pages(db, ctx.domain))


@router.get("/change", response_model=OpportunityChangeResponse,
            summary="Competitive Diagnosis: real month-over-month search-volume change")
def change(ctx: DomainContext = Depends(get_domain_context),
           db: Session = Depends(get_db)) -> OpportunityChangeResponse:
    return OpportunityChangeResponse(**opportunity_service.change(db, ctx.domain))


@router.get("/strategy", response_model=OpportunityStrategyResponse,
            summary="Competitive Strategy: real competitor footprint on our keywords")
def strategy(scope: str = Query("auto", pattern="^(auto|curated|serp)$",
                                description="auto = your competitor list if any, else SERP discovery"),
             ctx: DomainContext = Depends(get_domain_context),
             db: Session = Depends(get_db)) -> OpportunityStrategyResponse:
    return OpportunityStrategyResponse(**opportunity_service.strategy(db, ctx.domain, scope=scope))


@router.get("/strategy/keywords", response_model=CompetitorKeywordsResponse,
            summary="Point 3: the keywords a competitor ranks for vs ours")
def competitor_keywords(
    competitor: str = Query(..., min_length=3, description="Competitor host, e.g. drishtiias.com"),
    kind: str = Query("all", pattern="^(all|common|gap)$",
                      description="all | common (we both rank) | gap (they rank, we don't)"),
    listing: PageQuery = Depends(page_query),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CompetitorKeywordsResponse:
    """One page of rows (sort: search_volume | their_position | our_position |
    est_visits | cpc | seo_difficulty | keyword); totals cover all rows."""
    data = opportunity_service.competitor_keywords(db, ctx.domain, competitor, kind=kind)
    term = listing.search()
    rows = [r for r in data["rows"] if not term or term in r["keyword"].lower()]
    sort = listing.sort if listing.sort in _COMPETITOR_KEYWORD_SORTS else "search_volume"
    data["rows"], data["pagination"] = paginate(
        rows, page=listing.page, page_size=listing.page_size,
        sort_key=(lambda r: r["keyword"].lower()) if sort == "keyword" else sort,
        descending=listing.descending)
    return CompetitorKeywordsResponse(**data)


_COMPETITOR_KEYWORD_SORTS = {"search_volume", "their_position", "our_position", "est_visits", "cpc",
                             "seo_difficulty", "keyword"}


# Market for Labs metrics. None = use the domain's own setting (see
# domain_settings; falls back to the workspace default, then in/en).
_COUNTRY = Query(None, min_length=2, max_length=2, description="Market (defaults to the domain's setting)")


def _market(ctx: DomainContext, country: str | None) -> str:
    """Explicit ?country wins; otherwise the domain's resolved market."""
    return country or domain_settings.resolve(ctx.domain, getattr(ctx.domain, "workspace", None)).country


def _language(ctx: DomainContext, language: str | None = None) -> str:
    """Explicit language wins; otherwise the domain's resolved language.

    Must match what the read path (ranked_keywords_service.payload) resolves, or
    freshly fetched runs get stored under a language the UI never reads back."""
    return language or domain_settings.resolve(ctx.domain, getattr(ctx.domain, "workspace", None)).language


def _topic_regex(ctx: DomainContext) -> str | None:
    """The domain's compiled topic rule for server-side 'relevant' filtering."""
    return domain_settings.resolve(ctx.domain, getattr(ctx.domain, "workspace", None)).topic_regex


@router.get("/strategy/analytics", response_model=CompetitorAnalyticsResponse,
            summary="Whole-site competitor analytics (stored DataForSEO Labs data)")
def competitor_analytics(country: str = _COUNTRY,
                         ctx: DomainContext = Depends(get_domain_context),
                         db: Session = Depends(get_db)) -> CompetitorAnalyticsResponse:
    return CompetitorAnalyticsResponse(
        **opportunity_service.competitor_analytics(db, ctx.domain, country=_market(ctx, country))
    )


@router.post("/strategy/analytics/refresh", response_model=CompetitorAnalyticsResponse,
             summary="Fetch whole-site competitor metrics from DataForSEO and store them",
             responses={502: {"description": "DataForSEO error / not configured"}})
def refresh_competitor_analytics(
    country: str = _COUNTRY,
    force: bool = Query(False, description="Re-fetch even if stored data is still fresh"),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> CompetitorAnalyticsResponse:
    try:
        payload = opportunity_service.refresh_competitor_analytics(
            db, ctx.domain, country=_market(ctx, country), force=force)
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc
    return CompetitorAnalyticsResponse(**payload)


@router.post("/strategy/backlinks/warm",
             summary="Fetch backlink counts + authority for our domain and its competitors",
             responses={502: {"description": "DataForSEO error / not configured"}})
def warm_competitor_backlinks(
    country: str = _COUNTRY,
    force: bool = Query(False, description="Re-fetch even if stored backlink data is still fresh"),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> dict:
    """Cheap, click-triggered warm-up so the Keyword Breakdown's Authority /
    Backlinks / Referring-domains columns are populated for our domain and
    competitors. Reads stay DB-only; reused until stale."""
    try:
        return opportunity_service.warm_competitor_backlinks(
            db, ctx.domain, country=_market(ctx, country), force=force)
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc


@router.get("/strategy/ranked-keywords/options", response_model=RankedKeywordsOptions,
            summary="Domains with stored ranked keywords + the live allowlist")
def ranked_keywords_options(country: str = _COUNTRY,
                            ctx: DomainContext = Depends(get_domain_context),
                            db: Session = Depends(get_db)) -> RankedKeywordsOptions:
    suggestions = [c["domain"] for c in opportunity_service.strategy(db, ctx.domain)["competitors"]]
    return RankedKeywordsOptions(**ranked_keywords_service.options(
        db, ctx.domain, country=country, suggestions=suggestions))


@router.get("/strategy/ranked-keywords", response_model=RankedKeywordsResponse,
            summary="Keywords a domain actually ranks for (stored DataForSEO Labs data, no fetch)")
def ranked_keywords(
    competitor: str = Query(..., min_length=3, description="Domain, e.g. drishtiias.com"),
    country: str = _COUNTRY,
    scope: str | None = Query(None, pattern="^(top|relevant|all)$",
                              description="Stored run of this scope (default: newest)"),
    bucket: str = Query("all", pattern="^(all|top3|top10|top20|rest)$", description="Position range"),
    show: str = Query("all", pattern="^(all|gap|common)$", description="gap = you don't rank; common = both rank"),
    url: str | None = Query(None, max_length=2000, description="Only this ranking page"),
    listing: PageQuery = Depends(page_query),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankedKeywordsResponse:
    """One page of rows (sort: etv | position | search_volume | keyword_difficulty | keyword)."""
    return RankedKeywordsResponse(**ranked_keywords_service.payload(
        db, ctx.domain, competitor, country=country, scope=scope,
        listing=listing, bucket=bucket, show=show, url=url))


@router.post("/strategy/ranked-keywords/estimate", response_model=RankedKeywordsEstimate,
             summary="Count + cost of a scope before fetching (one 1-keyword request)",
             responses={403: {"description": "Live disabled / domain not allowed"},
                        502: {"description": "DataForSEO error / not configured"}})
def estimate_ranked_keywords(
    competitor: str = Query(..., min_length=3),
    scope: str = Query("relevant", pattern="^(top|relevant|all)$"),
    country: str = _COUNTRY,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankedKeywordsEstimate:
    try:
        est = ranked_keywords_service.estimate(
            competitor, env=data_env.env_for_domain(db, ctx.domain),
            location_code=domain_analytics_service.location_code_for(_market(ctx, country)),
            language=_language(ctx), scope=scope, topic_regex=_topic_regex(ctx),
            extra_allowed=ranked_keywords_service.allowed_for_domain(db, ctx.domain))
    except ranked_keywords_service.RankedFetchNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc
    return RankedKeywordsEstimate(**est)


@router.post("/strategy/ranked-keywords/refresh", response_model=RankedKeywordsResponse,
             summary="Fetch a domain's ranked keywords from DataForSEO Labs (paid) and store them",
             responses={403: {"description": "Domain not in the live budget allowlist"},
                        502: {"description": "DataForSEO error / not configured"}})
def refresh_ranked_keywords(
    competitor: str = Query(..., min_length=3),
    country: str = _COUNTRY,
    limit: int | None = Query(None, ge=1, le=1000, description="Keywords to fetch (capped)"),
    force: bool = Query(False, description="Re-fetch even if a fresh stored run exists"),
    scope: str = Query("top", pattern="^(top|relevant|all)$",
                       description="top = top-N by traffic; relevant/all = paged, capped"),
    max_keywords: int | None = Query(None, ge=1, le=100000,
                                     description="Cap for relevant/all (<= server max)"),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankedKeywordsResponse:
    try:
        run, fetched = ranked_keywords_service.fetch(
            db, competitor, env=data_env.env_for_domain(db, ctx.domain),
            location_code=domain_analytics_service.location_code_for(_market(ctx, country)),
            language=_language(ctx), limit=limit, force=force, scope=scope, max_keywords=max_keywords,
            topic_regex=_topic_regex(ctx),
            extra_allowed=ranked_keywords_service.allowed_for_domain(db, ctx.domain))
    except ranked_keywords_service.RankedFetchNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc
    return RankedKeywordsResponse(**ranked_keywords_service.payload(
        db, ctx.domain, competitor, country=country, scope=scope,
        refresh={"fetched": fetched, "cost": float(run.cost) if fetched and run.cost is not None else None}))


@router.get("/action-plan", response_model=ActionPlanResponse,
            summary="Opportunity Research action plan (improve / competitor gaps / new targets) — DB only")
def action_plan(country: str = _COUNTRY,
                ctx: DomainContext = Depends(get_domain_context),
                db: Session = Depends(get_db)) -> ActionPlanResponse:
    """Top 10, counts and sources. The full improve / gaps / targets lists are
    paged separately via GET /action-plan/items (sent empty here)."""
    plan = action_plan_service.build(db, ctx.domain, country=country)
    plan["improve"], plan["gaps"], plan["targets"] = [], [], []
    return ActionPlanResponse(**plan)


_ACTION_LISTS = {"improve": "improve", "gap": "gaps", "target": "targets"}
_ACTION_SORTS = {"gain", "volume", "difficulty", "position", "competitor_position", "keyword"}


@router.get("/action-plan/items", response_model=ActionItemsResponse,
            summary="One page of an action-plan list (improve | gap | target) — DB only")
def action_plan_items(type: str = Query(..., pattern="^(improve|gap|target)$"),
                      country: str = _COUNTRY,
                      listing: PageQuery = Depends(page_query),
                      ctx: DomainContext = Depends(get_domain_context),
                      db: Session = Depends(get_db)) -> ActionItemsResponse:
    """Default order = the plan's priority; `sort` = gain | volume | difficulty |
    position | competitor_position | keyword. `q` matches keyword, competitor or URL."""
    items = action_plan_service.build(db, ctx.domain, country=country)[_ACTION_LISTS[type]]
    term = listing.search()
    if term:
        items = [i for i in items if term in i["keyword"].lower() or term in (i.get("competitor") or "")
                 or term in (i.get("url") or "").lower()]
    sort = listing.sort if listing.sort in _ACTION_SORTS else None
    rows, info = paginate(items, page=listing.page, page_size=listing.page_size,
                          sort_key=(lambda i: i["keyword"].lower()) if sort == "keyword" else sort,
                          descending=listing.descending)
    return ActionItemsResponse(type=type, rows=rows, pagination=info)


@router.post("/keywords/track", response_model=TrackKeywordResponse,
             summary="Add a keyword to the domain's tracked keywords (idempotent)")
def track_keyword(keyword: str = Query(..., min_length=1, max_length=255),
                  ctx: DomainContext = Depends(get_domain_context),
                  db: Session = Depends(get_db)) -> TrackKeywordResponse:
    try:
        return TrackKeywordResponse(**action_plan_service.track_keyword(db, ctx.domain, keyword))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/action-plan/insight", response_model=PageInsightResponse,
            summary="Your page details + content suggestions for one keyword (fetches pages, no paid API)")
def action_insight(
    keyword: str = Query(..., min_length=1, max_length=255),
    url: str | None = Query(None, max_length=2000, description="Your ranking page, if known"),
    competitor_url: str | None = Query(None, max_length=2000),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> PageInsightResponse:
    return PageInsightResponse(**page_insight_service.analyze(
        db, ctx.domain, keyword, url=url, competitor_url=competitor_url))
