"""Schemas for rank tracking."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class TrackedKeywordCreate(BaseModel):
    term: str = Field(min_length=1, max_length=200)

    @field_validator("term")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Keyword is required")
        return value


class RankEntryCreate(BaseModel):
    # 1..1000; null means "not ranking in the tracked range".
    position: int | None = Field(default=None, ge=1, le=1000)
    recorded_at: date | None = None


class RankEntryResponse(BaseModel):
    id: uuid.UUID
    position: int | None
    recorded_at: date


class TrackedKeywordResponse(BaseModel):
    id: uuid.UUID
    term: str
    latest_position: int | None
    previous_position: int | None
    best_position: int | None
    entry_count: int
    created_at: datetime
    # Market signals from the shared keyword library (None when not known yet).
    search_volume: int | None = None
    difficulty: float | None = None
    intent: str | None = None


class RankSyncResponse(BaseModel):
    synced: int
    total: int
    recorded_at: date


class RankImportResponse(BaseModel):
    """Result of importing top Search Console queries as tracked keywords."""

    imported: int  # new keywords added to tracking
    synced: int  # tracked keywords given a position from the GSC data
    total: int  # tracked-keyword count after the import
    recorded_at: date
