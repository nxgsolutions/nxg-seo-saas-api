"""Pydantic schemas for the whole-site (domain) SEO score."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DomainScoreJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str  # running | done | error
    step: int
    step_label: str | None = None
    pages_total: int | None = None
    pages_scored: int
    error: str | None = None
    result_id: uuid.UUID | None = None
    started_at: datetime
    finished_at: datetime | None = None


class SiteCheck(BaseModel):
    id: str
    label: str
    status: str
    detail: str


class ScoredPage(BaseModel):
    url: str
    page_id: str | None = None
    score: int
    page_type: str


class DomainScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    overall: int
    page_quality: int
    site_technical: int
    category_averages: dict[str, int] = {}
    site_checks: list[SiteCheck] = []
    pages: list[ScoredPage] = []
    pages_total: int
    pages_scored: int
    pages_indexable: int
    weak_share: float
    created_at: datetime


class DomainScoreTrendPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    overall: int
    page_quality: int
    site_technical: int
    created_at: datetime


class DomainScoreView(BaseModel):
    """Latest score + recent trend for the Site Score page."""

    latest: DomainScoreOut | None = None
    history: list[DomainScoreTrendPoint] = []
    running: DomainScoreJobOut | None = None
