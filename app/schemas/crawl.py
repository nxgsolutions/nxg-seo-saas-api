"""Schemas for the robots.txt & sitemap checker."""

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.audit import AuditCheck


class RobotsCheckRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)


class RobotsCheckResponse(BaseModel):
    url: str
    origin: str
    robots: dict[str, Any]
    sitemap: dict[str, Any]
    checks: list[AuditCheck]
