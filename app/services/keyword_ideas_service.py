"""Keyword Ideas explorer — composes Autocomplete expansion with the pure scoring
engine, and (when configured) real Google Ads Keyword Planner metrics. Stateless
(no DB writes).

Each idea gets a rule-based intent + a keyword-shape SEO difficulty. When Google
Ads is configured, each keyword also carries real volume / CPC / PD (competition
index); otherwise those stay null.
"""

from __future__ import annotations

import re

from app.core.config import settings
from app.services import keyword_planner_service, keyword_scoring, keyword_suggest

_WORD_RE = re.compile(r"[^\s]+")
_CATEGORY_ORDER = ("autocomplete", "related", "questions", "prepositions", "comparisons")


def _row(keyword: str, metrics: dict | None) -> dict:
    return {
        "keyword": keyword,
        "intent": keyword_scoring.classify_intent(keyword),
        "difficulty": keyword_scoring.heuristic_difficulty(keyword),
        "word_count": len(_WORD_RE.findall(keyword)),
        "volume": metrics.get("avg_monthly_searches") if metrics else None,
        "cpc": (metrics.get("cpc_high") or metrics.get("cpc_low")) if metrics else None,
        "pd": metrics.get("competition_index") if metrics else None,
    }


def _planner_metrics(seed: str, country: str, language: str) -> dict[str, dict]:
    """Real Ads metrics keyed by normalized keyword. Empty if Ads unconfigured/fails."""
    if not settings.google_ads_configured:
        return {}
    try:
        data = keyword_planner_service.generate_ideas(
            seed, mode="keyword", country=country, language=language
        )
    except keyword_planner_service.PlannerError:
        return {}
    return {
        keyword_scoring.normalize_keyword(r["keyword"]): r for r in data.get("rows", [])
    }


def keyword_ideas(seed: str, country: str = "us", language: str = "en") -> dict:
    """Categorized keyword ideas for a seed, each with intent, SEO difficulty and
    (when Ads is configured) real volume / CPC / PD."""
    buckets = keyword_suggest.ideas(seed, country, language)
    metrics = _planner_metrics(seed, country, language)
    categories = []
    total = 0
    for name in _CATEGORY_ORDER:
        words = buckets.get(name, [])
        rows = [_row(k, metrics.get(keyword_scoring.normalize_keyword(k))) for k in words]
        total += len(rows)
        categories.append({"name": name, "count": len(rows), "keywords": rows})
    return {
        "seed": seed.strip(),
        "country": country,
        "language": language,
        "volume_source": "ads" if metrics else "none",
        "total": total,
        "categories": categories,
    }
