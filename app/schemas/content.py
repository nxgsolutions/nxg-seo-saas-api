"""Schemas for the keyword / content analysis tool."""

from pydantic import BaseModel, Field


class KeywordAnalysisRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)
    term: str | None = Field(default=None, max_length=120)


class Phrase(BaseModel):
    phrase: str
    count: int
    density: float


class KeywordPresence(BaseModel):
    term: str
    in_title: bool
    in_h1: bool
    in_meta_description: bool
    occurrences: int
    density: float
    score: int


class KeywordAnalysisResponse(BaseModel):
    url: str
    word_count: int
    unigrams: list[Phrase]
    bigrams: list[Phrase]
    trigrams: list[Phrase]
    target: KeywordPresence | None = None
