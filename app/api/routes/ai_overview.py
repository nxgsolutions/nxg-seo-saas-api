"""AI overviews — short summaries of a page / section, built from stored data.

Never calls a paid API: facts come from the DB and the optional rewrite uses
the local Ollama model (see ai_overview_service). Cached per workspace.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, WorkspaceContext, get_domain_context, get_workspace_context
from app.api.routes.keyword_analysis import _detail
from app.db.session import get_db
from app.schemas.ai_overview import AiOverviewResponse
from app.services import action_plan_service, ai_overview_service, keyword_analysis_service

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["ai-overview"])


@router.post("/ai-overview/keyword-search/{research_id}", response_model=AiOverviewResponse,
            summary="Overview of one keyword search (Keyword Analysis)")
def keyword_search_overview(
    research_id: str,
    refresh: bool = Query(False),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> AiOverviewResponse:
    research = keyword_analysis_service.get_research(db, ctx.workspace, research_id)
    if research is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")
    facts, draft = ai_overview_service.keyword_search(_detail(db, research).model_dump())
    return AiOverviewResponse(**ai_overview_service.get_or_create(
        db, ctx.workspace, "keyword_search", research_id, facts, draft, refresh=refresh))


@router.post("/domains/{domain_id}/ai-overview/action-plan", response_model=AiOverviewResponse,
            summary="Overview of the Opportunity Research action plan")
def action_plan_overview(
    refresh: bool = Query(False),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> AiOverviewResponse:
    facts, draft = ai_overview_service.action_plan(action_plan_service.build(db, ctx.domain))
    return AiOverviewResponse(**ai_overview_service.get_or_create(
        db, ctx.workspace, "action_plan", str(ctx.domain.id), facts, draft, refresh=refresh))
