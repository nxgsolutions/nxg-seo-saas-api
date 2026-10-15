"""Pydantic schemas for the page audit tool."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AuditRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)
    device: Literal["desktop", "mobile"] = "desktop"


class AuditCheck(BaseModel):
    id: str
    label: str
    status: str  # pass | warn | fail
    detail: str


class AuditResponse(BaseModel):
    url: str
    final_url: str
    status_code: int
    load_time_ms: int
    page_size_bytes: int
    score: int
    checks: list[AuditCheck]
    metrics: dict[str, Any]
