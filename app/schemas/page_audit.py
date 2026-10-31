"""Pydantic schemas for saved page audits."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.audit import AuditCheck
from app.schemas.page_score import ScoreCategoryOut


class PageAuditRunRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)
    device: Literal["desktop", "mobile"] = "desktop"


class PageAuditSummary(BaseModel):
    """Lightweight row for history lists (no checks/metrics)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    score: int
    # Genuine overall SEO score (the headline number shown in lists).
    overall: int | None = None
    status_code: int
    load_time_ms: int
    created_at: datetime


class PageAuditDetail(BaseModel):
    """Full saved audit."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    score: int
    status_code: int
    load_time_ms: int
    page_size_bytes: int
    checks: list[AuditCheck]
    metrics: dict[str, Any]
    # Genuine overall SEO score + category breakdown + blocking problems, so one
    # audit powers the score, the breakdown and the fixes (null on pre-0051 audits).
    overall: int | None = None
    categories: list[ScoreCategoryOut] = []
    blockers: list[str] = []
    created_at: datetime
    # Page history: the tracked page and what changed since its previous check.
    page_id: uuid.UUID | None = None
    changes: list[dict[str, Any]] = []
