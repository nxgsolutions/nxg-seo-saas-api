"""Rank tracking routes (domain-scoped tracked keywords + position entries)."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.ranking import (
    RankEntryCreate,
    RankEntryResponse,
    RankImportResponse,
    RankSyncResponse,
    TrackedKeywordCreate,
    TrackedKeywordResponse,
)
from app.services import gsc_service, rank_service
from app.services.rank_service import KeywordExistsError


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
    return TrackedKeywordResponse(
        id=kw.id,
        term=kw.term,
        latest_position=None,
        previous_position=None,
        best_position=None,
        entry_count=0,
        created_at=kw.created_at,
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
        db, kw, payload.position, payload.recorded_at or date.today()
    )
    return RankEntryResponse(id=entry.id, position=entry.position, recorded_at=entry.recorded_at)
