"""Schemas for the AI Search (GEO/AEO) analysis feature."""

from pydantic import BaseModel, Field


class AiSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=200)
    domain: str | None = Field(default=None, max_length=255)


class TargetQuery(BaseModel):
    query: str
    opportunity: str  # high | medium | low


class Recommendation(BaseModel):
    title: str
    detail: str
    priority: str  # high | medium | low


class AiSearchResponse(BaseModel):
    is_sample: bool = False  # always False now (kept for API compatibility)
    model: str | None = None  # the AI model that wrote the analysis
    query: str
    domain: str | None = None
    visibility_score: int
    summary: str
    target_queries: list[TargetQuery] = []
    recommendations: list[Recommendation] = []
    content_gaps: list[str] = []
    competitor_notes: list[str] = []


class MetaSuggestRequest(BaseModel):
    input: str = Field(..., min_length=2, max_length=2048)


class MetaSuggestResponse(BaseModel):
    is_sample: bool
    title: str
    description: str
