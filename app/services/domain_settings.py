"""Effective per-domain settings — market + niche, with a fallback chain.

Every setting resolves **domain value → workspace default → global default**:
  country / language : domain.country/language → workspace.default_* → in / en
  topic_terms        : the domain's on-topic terms (auto-derived + user-edited);
                       EMPTY ⇒ topic_regex is None ⇒ NO topic filtering.
  brand_terms        : competitor/own brands to drop from targets (empty ⇒ none).
  is_local           : keep "near me" keywords when true.

This replaced the global UPSC constants (a hardcoded relevant-keyword regex and
keyword_relevance.KNOWN_BRAND_TERMS) that used to be applied to every domain: the
regex is gone, and KNOWN_BRAND_TERMS is now only an opt-in seed. Per-domain
topic_terms/brand_terms drive filtering instead, so the app serves any niche.
Pure/deterministic — no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

GLOBAL_COUNTRY = "in"
GLOBAL_LANGUAGE = "en"


@dataclass(frozen=True)
class DomainSettings:
    country: str
    language: str
    topic_terms: list[str] = field(default_factory=list)
    brand_terms: list[str] = field(default_factory=list)
    is_local: bool = False
    # Compiled OR-regex of the topic terms, or None when there are none
    # (None ⇒ callers treat every keyword as on-topic).
    topic_regex: str | None = None


def split_terms(raw: str | None) -> list[str]:
    """Parse a stored comma/newline-separated term list into clean lowercase terms."""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for part in re.split(r"[,\n]", raw):
        term = " ".join(part.strip().lower().split())
        if term:
            seen.setdefault(term, None)
    return list(seen)


def compile_topic_regex(terms: list[str]) -> str | None:
    """OR-regex of the terms (each escaped), or None when the list is empty."""
    if not terms:
        return None
    return "|".join(re.escape(t) for t in terms)


def _fallback(*values: str | None) -> str:
    for v in values:
        if v and v.strip():
            return v.strip().lower()
    return ""


def resolve(domain, workspace=None) -> DomainSettings:
    """Build the effective settings for a domain (workspace may be None)."""
    ws_country = getattr(workspace, "default_country", None)
    ws_language = getattr(workspace, "default_language", None)
    country = _fallback(getattr(domain, "country", None), ws_country, GLOBAL_COUNTRY)
    language = _fallback(getattr(domain, "language", None), ws_language, GLOBAL_LANGUAGE)
    topic_terms = split_terms(getattr(domain, "topic_terms", None))
    brand_terms = split_terms(getattr(domain, "brand_terms", None))
    return DomainSettings(
        country=country or GLOBAL_COUNTRY,
        language=language or GLOBAL_LANGUAGE,
        topic_terms=topic_terms,
        brand_terms=brand_terms,
        is_local=bool(getattr(domain, "is_local", False)),
        topic_regex=compile_topic_regex(topic_terms),
    )
