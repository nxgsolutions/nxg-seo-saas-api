"""Schemas for the Page Optimization feature."""

from typing import Literal

from pydantic import BaseModel


class OptimizeRequest(BaseModel):
    url: str
    device: Literal["desktop", "mobile"] = "desktop"


class OptimizationItem(BaseModel):
    id: str
    category: str
    title: str
    impact: str  # high | medium | low
    effort: str  # quick | moderate | high
    current: str
    fix: str
    severity: str  # warn | fail


class OptimizationWin(BaseModel):
    id: str
    title: str
    category: str


class OptimizationSummary(BaseModel):
    high: int
    medium: int
    low: int
    total_issues: int
    passed: int


class OptimizeResponse(BaseModel):
    url: str
    final_url: str
    score: int
    device: str
    summary: OptimizationSummary
    recommendations: list[OptimizationItem]
    wins: list[OptimizationWin]
