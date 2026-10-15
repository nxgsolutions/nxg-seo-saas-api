"""Schemas for the PageSpeed Insights (Core Web Vitals) tool."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class PsiRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)
    strategy: Literal["mobile", "desktop"] = "mobile"


class PsiResponse(BaseModel):
    url: str
    strategy: str
    performance_score: int | None
    lab: dict[str, Any]
    field: dict[str, Any] | None
