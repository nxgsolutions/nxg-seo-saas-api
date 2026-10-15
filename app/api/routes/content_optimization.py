"""Content optimization routes (workspace-scoped, private per workspace)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_workspace_context
from app.db.session import get_db
from app.models.content_optimization import ContentOptimization
from app.schemas.content_opt import (
    ContentOptimizeRequest,
    ContentOptResult,
    ContentOptSession,
    ContentOptSessionPage,
    ContentOptSessionSummary,
    KeywordOption,
    KeywordOptions,
    ScorePoint,
)
from app.services import content_opt_service, openai_ai
from app.services.content_opt_service import ContentFetchError
from app.services.pagination import PageQuery, page_query

router = APIRouter(
    prefix="/workspaces/{workspace_id}/content-optimization", tags=["content-optimization"]
)


def _summary(s: ContentOptimization) -> ContentOptSessionSummary:
    return ContentOptSessionSummary(
        id=str(s.id), url=s.url, title=s.title, keyword=s.keyword, model_used=s.model_used,
        content_score=s.content_score, created_at=s.created_at,
    )


def _session_out(db: Session, ctx: WorkspaceContext, s: ContentOptimization) -> ContentOptSession:
    return ContentOptSession(
        **_summary(s).model_dump(),
        result=ContentOptResult(**s.result),
        history=[ScorePoint(id=str(h.id), content_score=h.content_score, created_at=h.created_at)
                 for h in content_opt_service.history(db, ctx.workspace, s)],
    )


@router.post("/run", response_model=ContentOptSession,
             summary="Score a page for a keyword, compare with top pages, write AI suggestions")
def run(
    payload: ContentOptimizeRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> ContentOptSession:
    try:
        session = content_opt_service.optimize(
            db, ctx.workspace, payload.url, keyword=payload.keyword, country=payload.country,
            domain_id=payload.domain_id, competitor_urls=payload.competitor_urls, use_ai=payload.use_ai,
        )
    except ContentFetchError as exc:
        # The page we tried to fetch is upstream of us — a failure to reach it is
        # a gateway problem (502), not a validation error (422).
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return _session_out(db, ctx, session)


@router.get("/sessions", response_model=ContentOptSessionPage, summary="Optimization sessions (paged, newest first)")
def list_sessions(
    listing: PageQuery = Depends(page_query),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> ContentOptSessionPage:
    rows, total = content_opt_service.list_sessions(
        db, ctx.workspace, page=listing.page, page_size=listing.page_size, q=listing.search())
    total_pages = max(1, -(-total // listing.page_size))
    return ContentOptSessionPage(
        rows=[_summary(s) for s in rows],
        pagination={"page": min(listing.page, total_pages), "page_size": listing.page_size,
                    "total": total, "total_pages": total_pages},
    )


@router.get("/sessions/{session_id}", response_model=ContentOptSession, summary="Get one session")
def get_session(
    session_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> ContentOptSession:
    session = content_opt_service.get_session(db, ctx.workspace, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return _session_out(db, ctx, session)


@router.get("/keyword-options", response_model=KeywordOptions, summary="Keywords to optimize a page for")
def keyword_options(
    domain_id: str | None = Query(None),
    url: str | None = Query(None, max_length=2048),
    country: str = Query("in", min_length=2, max_length=8),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> KeywordOptions:
    rows = content_opt_service.keyword_options(db, ctx.workspace, domain_id=domain_id, url=url, country=country)
    return KeywordOptions(keywords=[KeywordOption(**r) for r in rows], ai_available=openai_ai.any_available())
