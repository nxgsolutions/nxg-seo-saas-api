"""Schemas for Content Optimization sessions.

A run (version 2) = rule-based checklist + score, a comparison with the pages
ranking for the target keyword, and optional AI writing suggestions. Older
sessions (version 1: AI-only, URL-only) still parse — every new field has a
default and the legacy fields are kept.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.pagination import PageInfo


class ContentOptimizeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    keyword: str | None = Field(default=None, max_length=200)
    country: str = Field(default="in", min_length=2, max_length=8)
    domain_id: str | None = None
    # Optional competitor pages to compare with (used instead of / on top of the
    # stored Google results for the keyword). Fetching them is free.
    competitor_urls: list[str] = Field(default_factory=list, max_length=5)
    # Skip the AI writing step (rule-based result only).
    use_ai: bool = True


class OptSuggestion(BaseModel):
    area: str
    recommendation: str
    # Ready-to-use text to apply (e.g. a rewritten meta description); null when
    # the action isn't a text change.
    suggestion: str | None = None
    priority: str


class ContentCheck(BaseModel):
    id: str
    group: str
    label: str
    status: str  # pass | warn | fail
    weight: int
    value: str
    tip: str = ""


class PageHeading(BaseModel):
    level: int
    text: str


class PageFacts(BaseModel):
    title: str = ""
    meta: str = ""
    h1: list[str] = []
    headings: list[PageHeading] = []
    word_count: int = 0
    paragraphs: int = 0
    avg_paragraph_words: float | None = None
    lists: int = 0
    images: int = 0
    images_missing_alt: int = 0
    internal_links: int = 0
    external_links: int = 0
    reading_ease: float | None = None
    has_schema: bool = False


class CompetitorPage(BaseModel):
    position: int | None = None
    url: str
    host: str
    title: str = ""
    word_count: int | None = None
    headings: int | None = None
    top_headings: list[str] = []
    reading_ease: float | None = None
    images: int | None = None
    keyword_in_title: bool | None = None
    keyword_in_h1: bool | None = None
    error: str | None = None


class Benchmark(BaseModel):
    pages: int
    word_count: float | None = None
    headings: float | None = None
    reading_ease: float | None = None
    images: float | None = None
    internal_links: float | None = None
    h2: float | None = None
    keyword_in_title: int = 0


class TermRow(BaseModel):
    term: str
    competitors: int
    avg_count: float
    your_count: int
    status: str  # missing | low | ok


class OutlineItem(BaseModel):
    heading: str
    level: int = 2
    note: str = ""
    is_new: bool = True


class AiWriting(BaseModel):
    title_options: list[str] = []
    meta_options: list[str] = []
    outline: list[OutlineItem] = []
    intro: str = ""
    recommendations: list[OptSuggestion] = []


class ContentOptResult(BaseModel):
    version: int = 1
    content_score: int
    summary: str = ""
    summary_source: str = "rules"  # rules | ai
    keyword: str | None = None
    country: str | None = None
    page: PageFacts | None = None
    checks: list[ContentCheck] = []
    competitor_source: str = "none"  # serp | manual | serp+manual | none
    serp_captured_at: str | None = None
    competitors: list[CompetitorPage] = []
    # Typed competitor URLs left out because they're on your own website.
    skipped_competitors: list[str] = []
    benchmark: Benchmark | None = None
    terms: list[TermRow] = []
    ai: AiWriting | None = None
    ai_error: str | None = None
    # Version 1 (legacy) fields.
    suggestions: list[OptSuggestion] = []
    target_keywords: list[str] = []
    outline: list[str] = []
    rewritten_intro: str = ""
    is_sample: bool = False


class ScorePoint(BaseModel):
    id: str
    content_score: int | None
    created_at: datetime


class ContentOptSession(BaseModel):
    id: str
    url: str
    title: str
    keyword: str | None = None
    model_used: str
    content_score: int | None
    created_at: datetime
    result: ContentOptResult
    # Earlier runs for the same URL (oldest → newest, this one included).
    history: list[ScorePoint] = []


class ContentOptSessionSummary(BaseModel):
    id: str
    url: str
    title: str
    keyword: str | None = None
    model_used: str
    content_score: int | None
    created_at: datetime


class ContentOptSessionPage(BaseModel):
    rows: list[ContentOptSessionSummary]
    pagination: PageInfo


class KeywordOption(BaseModel):
    keyword: str
    source: str  # page (this URL ranks for it) | tracked | site (website keyword)
    search_volume: int | None = None
    position: int | None = None
    has_serp: bool = False


class KeywordOptions(BaseModel):
    keywords: list[KeywordOption]
    ai_available: bool
