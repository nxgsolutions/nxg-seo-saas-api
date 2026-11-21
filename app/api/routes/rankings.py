"""Rank tracking routes (domain-scoped tracked keywords + position entries)."""

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.ranking import (
    RankEntryCreate,
    RankEntryResponse,
    KeywordReportingResponse,
    RankHistoryResponse,
    RankImportResponse,
    RankSyncResponse,
    RankTimelineResponse,
    ReportingFacetsResponse,
    RunDueResponse,
    TrackedKeywordCreate,
    TrackedKeywordResponse,
    TrackedKeywordUpdate,
    TrackNowResponse,
)
from app.services import data_env, gsc_service, rank_service, rank_tracking_service
from app.services.rank_service import KeywordExistsError
from app.services.serp_scrape_service import ScrapeBlockedError, ScrapeError


def _gsc_keywords_or_http(
    db: Session, ctx: DomainContext, days: int = 28
) -> list[dict]:
    """Fetch the domain's Search Console query data, mapping GSC errors to HTTP."""
    try:
        return gsc_service.keyword_performance(db, ctx.workspace, ctx.domain, days)["keywords"]
    except gsc_service.GscNotConfiguredError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google Search Console is not configured.")
    except gsc_service.GscNotConnectedError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Google Search Console is not connected for this workspace.")
    except gsc_service.GscPropertyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except gsc_service.GscApiError as exc:
        raise upstream_error(exc) from exc

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}",
    tags=["rankings"],
)


def _to_response(s: rank_service.KeywordSummary) -> TrackedKeywordResponse:
    return TrackedKeywordResponse(
        id=s.keyword.id,
        term=s.keyword.term,
        latest_position=s.latest_position,
        previous_position=s.previous_position,
        best_position=s.best_position,
        entry_count=s.entry_count,
        created_at=s.keyword.created_at,
        search_volume=s.search_volume,
        difficulty=s.difficulty,
        intent=s.intent,
        tracking_status=s.tracking_status,
        tracking_enabled=s.tracking_enabled,
        tracking_frequency_days=s.tracking_frequency_days,
        last_fetched_at=s.last_fetched_at,
        next_fetch_at=s.next_fetch_at,
    )


@router.get(
    "/tracked-keywords",
    response_model=list[TrackedKeywordResponse],
    summary="List tracked keywords for a domain",
    responses={404: {"description": "Domain not found"}},
)
def list_keywords(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> list[TrackedKeywordResponse]:
    return [
        _to_response(s)
        for s in rank_service.list_keyword_summaries(db, ctx.domain)
    ]


@router.post(
    "/tracked-keywords",
    response_model=TrackedKeywordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Track a keyword for a domain",
    responses={
        404: {"description": "Domain not found"},
        409: {"description": "Keyword already tracked"},
    },
)
def add_keyword(
    payload: TrackedKeywordCreate,
    background: BackgroundTasks,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> TrackedKeywordResponse:
    try:
        kw = rank_service.add_keyword(db, ctx.domain, payload.term)
    except KeywordExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That keyword is already tracked.",
        )
    # New keywords are due immediately; set the schedule anchor now.
    if kw.next_fetch_at is None:
        from sqlalchemy import func as _func
        kw.next_fetch_at = _func.now()
        db.commit()
        db.refresh(kw)
    # Sandbox workspaces capture the initial SERP right away (free). Live
    # workspaces wait for an explicit "Track now" click (no silent paid spend).
    if data_env.env_for_workspace(ctx.workspace) == data_env.SANDBOX:
        background.add_task(
            rank_tracking_service.fetch_keyword_task, ctx.domain.id, kw.id, only_free=True
        )
    return TrackedKeywordResponse(
        id=kw.id,
        term=kw.term,
        latest_position=None,
        previous_position=None,
        best_position=None,
        entry_count=0,
        created_at=kw.created_at,
        tracking_status=kw.tracking_status,
        tracking_enabled=kw.tracking_enabled,
        tracking_frequency_days=kw.tracking_frequency_days,
        last_fetched_at=kw.last_fetched_at,
        next_fetch_at=kw.next_fetch_at,
    )


@router.delete(
    "/tracked-keywords/{keyword_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop tracking a keyword",
    responses={404: {"description": "Keyword not found"}},
)
def delete_keyword(
    keyword_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> Response:
    if not rank_service.delete_keyword(db, ctx.domain, keyword_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tracked-keywords/{keyword_id}/restore",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Restore a removed tracked keyword",
    responses={404: {"description": "Keyword not found or not deleted"}},
)
def restore_keyword(
    keyword_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> Response:
    if not rank_service.restore_keyword(db, ctx.domain, keyword_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Keyword not found or not deleted",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tracked-keywords/{keyword_id}/entries",
    response_model=list[RankEntryResponse],
    summary="Get a keyword's position history",
    responses={404: {"description": "Keyword not found"}},
)
def list_entries(
    keyword_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> list[RankEntryResponse]:
    kw = rank_service.get_keyword(db, ctx.domain, keyword_id)
    if kw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    return [
        RankEntryResponse(id=e.id, position=e.position, recorded_at=e.recorded_at)
        for e in rank_service.list_entries(db, kw)
    ]


@router.post(
    "/tracked-keywords/sync-gsc",
    response_model=RankSyncResponse,
    summary="Record real positions for tracked keywords from Search Console",
    responses={
        409: {"description": "Search Console not connected"},
        404: {"description": "No matching Search Console property"},
        502: {"description": "Search Console request failed"},
        503: {"description": "Search Console not configured"},
    },
)
def sync_from_gsc(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankSyncResponse:
    """Pull the last 28 days of query positions from GSC and record today's
    position for each tracked keyword that matches a query."""
    keywords = _gsc_keywords_or_http(db, ctx)
    result = rank_service.sync_from_gsc(db, ctx.domain, keywords, date.today())
    return RankSyncResponse(synced=result["synced"], total=result["total"], recorded_at=date.today())


@router.post(
    "/tracked-keywords/import-gsc",
    response_model=RankImportResponse,
    summary="Import top Search Console queries as tracked keywords",
    responses={
        409: {"description": "Search Console not connected"},
        404: {"description": "No matching Search Console property"},
        502: {"description": "Search Console request failed"},
        503: {"description": "Search Console not configured"},
    },
)
def import_from_gsc(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankImportResponse:
    """Start tracking the domain's top Search Console queries (and record today's
    position for each). For the empty Rank Tracking page: one click to pull real
    keywords from Google instead of adding them by hand."""
    keywords = _gsc_keywords_or_http(db, ctx)
    result = rank_service.import_from_gsc(db, ctx.domain, keywords, date.today())
    return RankImportResponse(
        imported=result["imported"], synced=result["synced"],
        total=result["total"], recorded_at=date.today(),
    )


@router.post(
    "/tracked-keywords/sync-serp",
    response_model=RankSyncResponse,
    summary="Record positions for tracked keywords from the latest SERP snapshot",
)
def sync_from_serp(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankSyncResponse:
    """Record each tracked keyword's position from the domain's latest SERP
    snapshot (already-pulled DataForSEO ranked keywords). No new paid call — it
    reads stored data. recorded_at is the snapshot's date (today if none)."""
    result = rank_service.sync_from_serp_snapshot(db, ctx.domain)
    return RankSyncResponse(
        synced=result["synced"], total=result["total"],
        recorded_at=result["recorded_at"] or date.today(),
    )


@router.post(
    "/tracked-keywords/{keyword_id}/entries",
    response_model=RankEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a position for a keyword (upserts by date)",
    responses={404: {"description": "Keyword not found"}, 422: {"description": "Validation error"}},
)
def record_position(
    keyword_id: str,
    payload: RankEntryCreate,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankEntryResponse:
    kw = rank_service.get_keyword(db, ctx.domain, keyword_id)
    if kw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    entry = rank_service.record_position(
        db, kw, payload.position, payload.recorded_at or date.today(), source="manual"
    )
    return RankEntryResponse(id=entry.id, position=entry.position, recorded_at=entry.recorded_at)


@router.patch(
    "/tracked-keywords/{keyword_id}",
    response_model=TrackedKeywordResponse,
    summary="Update a keyword's tracking cadence / on-off",
    responses={404: {"description": "Keyword not found"}},
)
def update_tracking(
    keyword_id: str,
    payload: TrackedKeywordUpdate,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> TrackedKeywordResponse:
    kw = rank_service.get_keyword(db, ctx.domain, keyword_id)
    if kw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    # Applies the change AND re-anchors next_fetch_at so the scheduler follows the
    # new cadence / resume immediately (pause/history are preserved).
    rank_tracking_service.update_tracking(
        db, kw,
        frequency_days=payload.tracking_frequency_days,
        enabled=payload.tracking_enabled,
    )
    summaries = {s.keyword.id: s for s in rank_service.list_keyword_summaries(db, ctx.domain)}
    return _to_response(summaries[kw.id])


@router.get(
    "/tracked-keywords/{keyword_id}/rank-history",
    response_model=RankHistoryResponse,
    summary="Our + competitor rank timeline for a tracked keyword",
    responses={404: {"description": "Keyword not found"}},
)
def rank_history(
    keyword_id: str,
    start: date | None = None,
    end: date | None = None,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankHistoryResponse:
    kw = rank_service.get_keyword(db, ctx.domain, keyword_id)
    if kw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    env = data_env.env_for_workspace(ctx.workspace)
    data = rank_tracking_service.rank_history(
        db, ctx.domain, kw, start=start, end=end, env=env
    )
    return RankHistoryResponse(**data)


@router.get(
    "/tracked-keywords/keyword-reporting",
    response_model=KeywordReportingResponse,
    summary="Total Ranked Keywords — Page 1/2/3 counts over time (from stored SERPs)",
)
def keyword_reporting(
    freq: str = Query("monthly", pattern="^(daily|weekly|monthly)$"),
    start: date | None = None,
    end: date | None = None,
    device: str | None = Query(None),
    location_code: int | None = Query(None),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> KeywordReportingResponse:
    """Counts how many tracked keywords rank on Page 1 (1-10), Page 2 (11-20) and
    Page 3 (21-30) per reporting period, derived from the stored depth-100 SERP
    snapshots. No provider call."""
    env = data_env.env_for_workspace(ctx.workspace)
    data = rank_tracking_service.keyword_reporting(
        db, ctx.domain, freq=freq, start=start, end=end, env=env,
        device=device, location_code=location_code,
    )
    return KeywordReportingResponse(**data)


@router.get(
    "/tracked-keywords/reporting-facets",
    response_model=ReportingFacetsResponse,
    summary="Distinct device + location values present in this domain's stored SERPs",
)
def reporting_facets(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> ReportingFacetsResponse:
    """Drives the Keyword Reporting device/location filters: each selector is only
    shown when more than one value exists in the stored snapshots."""
    env = data_env.env_for_workspace(ctx.workspace)
    data = rank_tracking_service.reporting_facets(db, ctx.domain, env=env)
    return ReportingFacetsResponse(**data)


@router.get(
    "/tracked-keywords/rank-timeline",
    response_model=RankTimelineResponse,
    summary="Aggregated competitor rank over time across tracked keywords",
)
def rank_timeline(
    freq: str = Query("weekly", pattern="^(daily|weekly|monthly)$"),
    start: date | None = None,
    end: date | None = None,
    keywords: str | None = Query(None, description="Comma-separated tracked keyword ids"),
    competitors: str | None = Query(None, description="Comma-separated competitor hosts"),
    top: int = Query(8, ge=1, le=20, description="Max competitor lines to plot"),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RankTimelineResponse:
    """Powers the per-keyword SERP-competitor graph on the Rankings page (with
    filters, top 10) and the simple overview on the Competitors page."""
    kw_ids = [k for k in (keywords or "").split(",") if k.strip()] or None
    comps = [c for c in (competitors or "").split(",") if c.strip()] or None
    env = data_env.env_for_workspace(ctx.workspace)
    data = rank_tracking_service.competitor_timeline(
        db, ctx.domain, env=env, freq=freq, start=start, end=end,
        keyword_ids=kw_ids, competitors=comps, top=top,
    )
    return RankTimelineResponse(**data)


@router.post(
    "/tracked-keywords/{keyword_id}/track-now",
    response_model=TrackNowResponse,
    summary="Fetch this keyword's SERP now (sandbox free; live paid)",
    responses={404: {"description": "Keyword not found"}, 502: {"description": "SERP provider failed"}},
)
def track_now(
    keyword_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> TrackNowResponse:
    """Fetch a fresh Google SERP for one keyword and record our position +
    competitors. Paid in live workspaces (the frontend confirms the price first);
    free in sandbox."""
    kw = rank_service.get_keyword(db, ctx.domain, keyword_id)
    if kw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    env = data_env.env_for_workspace(ctx.workspace)
    try:
        result = rank_tracking_service.fetch_keyword_serp(
            db, ctx.domain, kw, env=env, force=True
        )
    except (ScrapeError, ScrapeBlockedError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return TrackNowResponse(
        term=result.get("term", kw.term),
        our_position=result.get("our_position"),
        recorded_at=result.get("recorded_at"),
        results=result.get("results", 0),
        competitors=result.get("competitors", 0),
        reused=result.get("reused", False),
        skipped=result.get("skipped"),
    )


@router.post(
    "/tracked-keywords/run-due",
    response_model=RunDueResponse,
    summary="Process this domain's keywords that are due for a SERP fetch",
)
def run_due(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> RunDueResponse:
    """Manually trigger the scheduler for this domain (fetches every tracked
    keyword whose next_fetch_at is due). Sandbox is always processed; live only
    when RANK_TRACKING_LIVE_ENABLED is on."""
    stats = rank_tracking_service.run_due(db, domain=ctx.domain)
    return RunDueResponse(**stats)
