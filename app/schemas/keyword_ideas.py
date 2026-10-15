"""Schemas for the free (keyless) Keyword Ideas explorer — Ubersuggest-style.

Ideas come from Google Autocomplete; intent + difficulty are computed by the
pure rule engine. Volume / CPC are intentionally optional: they require a real
volume source (Google Ads Keyword Planner or a paid provider) and are null until
one is wired.
"""

from pydantic import BaseModel, Field


class KeywordIdeasRequest(BaseModel):
    seed: str = Field(min_length=1, max_length=200)
    country: str = Field(default="us", min_length=2, max_length=2)
    language: str = Field(default="en", min_length=2, max_length=5)


class KeywordIdeaRow(BaseModel):
    keyword: str
    intent: str  # informational | commercial | transactional | navigational | unknown
    difficulty: int  # 0-100 estimated SEO difficulty
    word_count: int
    volume: int | None = None  # avg monthly searches (Ads); null until configured
    cpc: float | None = None  # top-of-page bid (Ads); null until configured
    pd: int | None = None  # paid difficulty = Ads competition index 0-100


class KeywordIdeaCategory(BaseModel):
    name: str  # autocomplete | related | questions | prepositions | comparisons
    count: int
    keywords: list[KeywordIdeaRow]


class KeywordIdeasResponse(BaseModel):
    seed: str
    country: str
    language: str
    # Where volume numbers come from. "none" = free ideas only (no absolute volume).
    volume_source: str = "none"
    total: int
    categories: list[KeywordIdeaCategory]
