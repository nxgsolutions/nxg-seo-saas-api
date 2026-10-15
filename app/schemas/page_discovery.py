"""Schemas for Page Discovery (discover a domain's pages, list + summary)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class DiscoveryRunOut(BaseModel):
    id: uuid.UUID | None = None
    source: str  # sitemap | crawl | none
    sitemap_url: str | None = None
    status: str  # running | completed | partial | failed | cancelled
    pages_found: int = 0
    pages_enriched: int = 0
    truncated: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PageDiscoverySummary(BaseModel):
    total: int
    indexable: int
    noindex: int
    unknown_index: int = 0
    by_type: dict[str, int]
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_freshness: dict[str, int] = {}
    delta_pct: int | None = None


class DiscoveredPageOut(BaseModel):
    id: uuid.UUID
    url: str
    title: str
    page_type: str
    http_status: int | None = None
    indexable: bool | None = None
    discovery_source: str | None = None
    sitemap_lastmod: datetime | None = None
    latest_audit_score: int | None = None
    word_count: int | None = None
    has_content: bool = False


class PageContentOut(BaseModel):
    title: str = ""
    meta: str = ""
    body_html: str = ""
    word_count: int | None = None
    http_status: int | None = None
    url: str = ""
    cached: bool = False
    fetched_at: datetime | None = None
    error: str | None = None


class ContentCheckOut(BaseModel):
    id: str
    group: str
    label: str
    status: str  # pass | warn | fail
    weight: int
    value: str
    tip: str


class ScoreContentRequest(BaseModel):
    title: str = ""
    meta: str = ""
    body_html: str = ""
    keyword: str = ""


class ScoreContentResult(BaseModel):
    score: int
    checks: list[ContentCheckOut]
    word_count: int
    reading_ease: float | None = None
    internal_links: int = 0
    external_links: int = 0
    images: int = 0
    headings: int = 0


class PageInfo(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class DiscoveredPageList(BaseModel):
    rows: list[DiscoveredPageOut]
    pagination: PageInfo
