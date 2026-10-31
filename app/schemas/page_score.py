"""Pydantic schemas for the overall page SEO score."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PageScoreRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)
    # Optional target keyword — content checks degrade gracefully without one.
    keyword: str | None = Field(default=None, max_length=255)
    device: Literal["desktop", "mobile"] = "desktop"


class ScoreCheckOut(BaseModel):
    id: str
    label: str
    status: str  # pass | warn | fail
    detail: str
    tip: str = ""


class ScoreCategoryOut(BaseModel):
    id: str
    label: str
    weight: int           # product weight (percent)
    score: int | None     # 0-100, or null when nothing could be measured
    checks: list[ScoreCheckOut]


class PerformanceContext(BaseModel):
    """Real Search Console numbers shown beside the score — NOT part of it."""

    clicks: int
    impressions: int
    ctr: float
    position: float
    days: int
    queries: list[dict[str, Any]] = []


class PageScoreResponse(BaseModel):
    url: str
    final_url: str
    status_code: int
    load_time_ms: int
    page_size_bytes: int
    keyword: str
    overall: int
    # Blocking problems that capped the score (noindex, non-200, no title/H1,
    # severely thin). Empty when none. The UI explains why a score is low.
    blockers: list[str] = []
    categories: list[ScoreCategoryOut]
    metrics: dict[str, Any] = {}
    # Real-world performance (GSC) — context only, present when connected.
    performance_context: PerformanceContext | None = None
    # Persisted runs only: the tracked page + what changed since its last check.
    page_id: uuid.UUID | None = None
    changes: list[dict[str, Any]] = []
    captured_at: datetime | None = None

    @classmethod
    def from_result(cls, result: Any, **extra: Any) -> "PageScoreResponse":
        """Build the API payload from a ``page_score_service.PageScoreResult``.

        ``extra`` carries the persisted-run fields (performance_context, page_id,
        changes, captured_at) that the stateless tool leaves unset.
        """
        return cls(
            url=result.url,
            final_url=result.final_url,
            status_code=result.status_code,
            load_time_ms=result.load_time_ms,
            page_size_bytes=result.page_size_bytes,
            keyword=result.keyword,
            overall=result.overall,
            blockers=list(getattr(result, "blockers", []) or []),
            categories=[
                ScoreCategoryOut(
                    id=cat.id, label=cat.label, weight=cat.weight, score=cat.score,
                    checks=[ScoreCheckOut(id=c.id, label=c.label, status=c.status,
                                          detail=c.detail, tip=c.tip) for c in cat.checks],
                )
                for cat in result.categories
            ],
            metrics=result.metrics,
            **extra,
        )
