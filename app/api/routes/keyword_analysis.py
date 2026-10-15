"""Keyword Analysis routes (domain-scoped, first-party — no paid provider).

Separate from the existing DataForSEO-backed `/tools` keyword endpoints so the
two coexist. Mounted under a workspace + domain so every keyword is scoped.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.db.session import get_db
from app.models.keyword_analysis import Keyword, KeywordMetric, KeywordResearch
from app.schemas.keyword_analysis import (
    CategoryCount,
    KeywordMetricOut,
    ResearchDetail,
    ResearchRequest,
    ResearchResultOut,
    ResearchSummary,
)
from app.services import domain_settings, keyword_analysis_service

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}/keyword-analysis",
    tags=["keyword-analysis"],
)


def _metric_out(metric: KeywordMetric | None) -> KeywordMetricOut | None:
    if metric is None:
        return None
    return KeywordMetricOut(
        intent=metric.intent,
        search_volume=metric.search_volume,
        cpc=float(metric.cpc) if metric.cpc is not None else None,
        competition=float(metric.competition) if metric.competition is not None else None,
        gsc_impressions=metric.gsc_impressions,
        gsc_clicks=metric.gsc_clicks,
        gsc_position=float(metric.gsc_position) if metric.gsc_position is not None else None,
        trend_index=metric.trend_index,
        trend_change_pct=float(metric.trend_change_pct) if metric.trend_change_pct is not None else None,
        difficulty=float(metric.difficulty) if metric.difficulty is not None else None,
        opportunity_score=(
            float(metric.opportunity_score) if metric.opportunity_score is not None else None
        ),
        relevance_score=(
            float(metric.relevance_score) if metric.relevance_score is not None else None
        ),
        confidence_score=(
            float(metric.confidence_score) if metric.confidence_score is not None else None
        ),
        captured_on=metric.metric_date,
    )


def _summary(research: KeywordResearch) -> ResearchSummary:
    return ResearchSummary(
        id=str(research.id),
        seed_keyword=research.seed.keyword,
        country_code=research.country_code,
        language_code=research.language_code,
        status=research.status,
        created_at=research.created_at,
        kind=research.kind,
        target=research.target,
        result_count=research.result_count,
    )


_CATEGORY_ORDER = ("autocomplete", "related", "questions", "prepositions", "comparisons")


def _detail(db: Session, research: KeywordResearch, *, include_results: bool = True) -> ResearchDetail:
    """Search metadata + category counts; `include_results=False` leaves the
    (potentially long) results list empty — the UI pages it via /results."""
    results: list[ResearchResultOut] = []
    seed_metric = None
    counts: dict[str, int] = {name: 0 for name in _CATEGORY_ORDER}
    for result, keyword, metric, sources in keyword_analysis_service.research_results(db, research):
        out = _metric_out(metric)
        results.append(
            ResearchResultOut(
                rank=result.rank,
                keyword_id=str(keyword.id),
                keyword=keyword.keyword,
                country_code=research.country_code,
                language_code=research.language_code,
                sources=sources,
                metric=out,
            )
        )
        for s in sources:
            if s in counts:
                counts[s] += 1
        if keyword.id == research.seed_keyword_id:
            seed_metric = out
    categories = [CategoryCount(name=n, count=counts[n]) for n in _CATEGORY_ORDER if counts[n] > 0]
    # Count what's actually stored (the research row's result_count isn't
    # always filled), so headers stay right when results are paged separately.
    summary = _summary(research).model_dump()
    summary["result_count"] = len(results)
    return ResearchDetail(
        **summary,
        seed_metric=seed_metric,
        questions=keyword_analysis_service.research_questions(db, research),
        categories=categories,
        results=results if include_results else [],
    )


@router.post(
    "/research",
    response_model=ResearchDetail,
    summary="Run a first-party keyword research pass from a seed",
)
def run_research(
    payload: ResearchRequest,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> ResearchDetail:
    # NOTE: keyword search does NOT auto-fetch a DataForSEO SERP — that is a paid
    # call and must be user-triggered (the SERP panel's Fetch/Refresh button via
    # /serp/analyze). Searching only runs the free first-party pipeline.
    # Market/language default to the domain's own settings (dynamic, per-domain)
    # so a search runs in the right market for any niche — not a static us/en.
    s = domain_settings.resolve(ctx.domain, getattr(ctx.domain, "workspace", None))
    research = keyword_analysis_service.run_research(
        db,
        ctx.workspace,
        ctx.domain,
        seed=payload.seed,
        country=payload.country or s.country,
        language=payload.language or s.language,
    )
    return _detail(db, research)


@router.get(
    "/researches",
    response_model=list[ResearchSummary],
    summary="List keyword research runs for a domain",
)
def list_researches(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> list[ResearchSummary]:
    return [_summary(r) for r in keyword_analysis_service.list_researches(db, ctx.workspace, ctx.domain)]


@router.get(
    "/researches/{research_id}",
    response_model=ResearchDetail,
    summary="Get one keyword research run with its ranked results",
)
def get_research(
    research_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> ResearchDetail:
    research = keyword_analysis_service.get_research(db, ctx.workspace, research_id)
    if research is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Research run not found"
        )
    return _detail(db, research)
