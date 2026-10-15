"""Schemas for page history (tracked pages, snapshots, search impact)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.pagination import PageInfo


class SitePageOut(BaseModel):
    id: str
    url: str
    url_key: str
    title: str
    domain_id: str | None = None
    latest_audit_score: int | None = None
    score_delta: int | None = None
    snapshot_count: int
    first_seen_at: datetime
    last_checked_at: datetime
    last_changed_at: datetime | None = None


class SitePageList(BaseModel):
    rows: list[SitePageOut]
    pagination: PageInfo


class PageChange(BaseModel):
    kind: str   # meta | content | media | links | technical | checks
    field: str
    label: str
    before: Any = None
    after: Any = None


class SnapshotOut(BaseModel):
    id: str
    captured_at: datetime
    source: str  # audit | content | inspect
    source_id: str | None = None
    device: str | None = None
    audit_score: int | None = None
    content_score: int | None = None
    keyword: str | None = None
    facts: dict[str, Any] = {}
    tech: dict[str, Any] | None = None
    changes: list[PageChange] = []


class PageTimeline(BaseModel):
    page: SitePageOut
    snapshots: list[SnapshotOut]


class SearchDay(BaseModel):
    day: str
    clicks: int
    impressions: int
    ctr: float
    position: float | None = None


class WindowStats(BaseModel):
    days: int
    clicks: int
    impressions: int
    ctr: float
    position: float | None = None


class ChangeImpact(BaseModel):
    snapshot_id: str
    date: str
    before: WindowStats | None = None
    after: WindowStats | None = None
    after_days: int
    too_early: bool


class PagePerformance(BaseModel):
    # ok | not_connected | no_property | no_website | error
    status: str
    series: list[SearchDay]
    impacts: list[ChangeImpact]
    window_days: int


class InspectRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)


class InspectResult(BaseModel):
    """A page's live tags (Title & Meta) — also saved as a page-history snapshot."""
    page_id: str | None = None
    url: str
    title: str
    meta: str
    h1: list[str] = []
    canonical: str = ""
    og_title: str = ""
    og_description: str = ""
    og_image: str = ""
    og_site_name: str = ""
    twitter_site: str = ""
    word_count: int = 0
    changes: list[PageChange] = []
