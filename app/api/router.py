"""Aggregate API router that mounts all route modules."""

from fastapi import APIRouter

from app.api.routes import (
    ai_overview,
    auth,
    backlinks,
    billing,
    categories,
    competitors,
    content_optimization,
    domain_score,
    pages,
    my_keywords,
    gsc,
    integrations,
    keyword_analysis,
    keyword_reports,
    keyword_search,
    onboarding,
    opportunity,
    page_discovery,
    rankings,
    serp,
    tools,
    workspaces,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(workspaces.router)
api_router.include_router(rankings.router)
api_router.include_router(onboarding.router)
api_router.include_router(tools.router)
api_router.include_router(gsc.router)
api_router.include_router(gsc.data_router)
api_router.include_router(gsc.callback_router)
api_router.include_router(billing.plans_router)
api_router.include_router(billing.billing_router)
api_router.include_router(billing.webhook_router)
api_router.include_router(content_optimization.router)
api_router.include_router(pages.router)
api_router.include_router(my_keywords.router)
api_router.include_router(categories.router)
api_router.include_router(competitors.router)
api_router.include_router(opportunity.router)
api_router.include_router(integrations.router)
api_router.include_router(keyword_analysis.router)
api_router.include_router(keyword_search.router)
api_router.include_router(keyword_reports.router)
api_router.include_router(serp.router)
api_router.include_router(ai_overview.router)
api_router.include_router(backlinks.router)
api_router.include_router(page_discovery.router)
api_router.include_router(domain_score.router)
