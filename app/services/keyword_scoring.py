"""Pure keyword scoring rule engine — no I/O, no network, fully deterministic.

Every function here takes primitives and returns primitives so the whole module
is trivially unit-testable (important: this box runs Python 3.14 where the
backend can't be pip-installed, but pure functions still run under pytest on a
3.11-3.13 box). All SEO "intelligence" is COMPUTED here from free first-party
signals — never fetched from a paid provider.

Signal sources feeding these functions:
- GSC: impressions (our stand-in for search volume), clicks, average position.
- SERP crawl: results_count, exact-match titles, distinct ranking domains.
- Seed keyword: for relevance via token overlap.
- Provenance: how many independent sources surfaced the keyword.
"""

from __future__ import annotations

import re

INTENTS = ("informational", "navigational", "commercial", "transactional", "unknown")

# Token cues for rule-based intent. Order matters: transactional and commercial
# are checked before informational because "best price" is commercial, not a
# "what/how" question.
_TRANSACTIONAL = {
    "buy", "price", "prices", "cheap", "deal", "deals", "discount", "coupon",
    "order", "purchase", "shop", "sale", "cost", "subscription", "pricing",
}
_COMMERCIAL = {
    "best", "top", "review", "reviews", "vs", "versus", "compare", "comparison",
    "alternative", "alternatives", "software", "tool", "tools", "services",
}
_INFORMATIONAL = {
    "how", "what", "why", "when", "where", "who", "guide", "tutorial",
    "examples", "example", "ideas", "tips", "meaning", "definition", "learn",
}
_NAVIGATIONAL = {"login", "signin", "sign", "download", "app", "near", "official"}

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_keyword(keyword: str) -> str:
    """Canonical form for de-duplication: lowercase, collapsed whitespace."""
    return " ".join(keyword.lower().split())


def _tokens(keyword: str) -> set[str]:
    return set(_WORD_RE.findall(keyword.lower()))


def classify_intent(keyword: str) -> str:
    """Best-effort search intent from token cues. Returns one of INTENTS.

    Deliberately conservative — returns 'unknown' when no cue matches, so the
    (optional) AI layer only has to resolve the genuinely ambiguous keywords.
    """
    tokens = _tokens(keyword)
    if tokens & _TRANSACTIONAL:
        return "transactional"
    if tokens & _COMMERCIAL:
        return "commercial"
    if tokens & _NAVIGATIONAL:
        return "navigational"
    if tokens & _INFORMATIONAL:
        return "informational"
    return "unknown"


def relevance_score(keyword: str, seed: str) -> float:
    """0-100 Jaccard-style token overlap between a keyword and the seed."""
    kt, st = _tokens(keyword), _tokens(seed)
    if not kt or not st:
        return 0.0
    overlap = len(kt & st)
    union = len(kt | st)
    return round(100.0 * overlap / union, 2)


def estimate_difficulty(
    results_count: int | None = None,
    exact_title_matches: int | None = None,
    distinct_domains: int | None = None,
) -> int | None:
    """0-100 keyword difficulty computed from live-SERP signals only.

    Higher when many pages target the exact phrase in their titles and when a
    diverse set of established domains rank (a crowded, contested SERP). Returns
    None when we have no SERP signal at all, rather than inventing a number.
    """
    if results_count is None and exact_title_matches is None and distinct_domains is None:
        return None

    score = 0.0
    # Sheer index size: log-ish buckets so 1M vs 10M doesn't swing wildly.
    if results_count is not None:
        if results_count >= 10_000_000:
            score += 45
        elif results_count >= 1_000_000:
            score += 35
        elif results_count >= 100_000:
            score += 25
        elif results_count >= 10_000:
            score += 15
        else:
            score += 5
    # Exact-phrase titles in the top results = strong on-page competition.
    if exact_title_matches is not None:
        score += min(exact_title_matches, 10) * 3.5  # up to +35
    # Domain diversity in the top 10 = many capable competitors.
    if distinct_domains is not None:
        score += min(distinct_domains, 10) * 2.0  # up to +20

    return max(0, min(100, round(score)))


def heuristic_difficulty(keyword: str) -> int:
    """A rough 0-100 difficulty from keyword shape alone (no SERP needed).

    The classic long-tail heuristic: the more words a query has, the more
    specific it is and the easier it usually is to rank for. Used as a labelled
    low-confidence fallback until a real SERP-crawl difficulty replaces it — so
    the KD column is never blank, but callers know it's an estimate.
    """
    words = len(_WORD_RE.findall(keyword.lower()))
    # 1 word ≈ hard (70), 2 ≈ 58, 3 ≈ 46, 4 ≈ 34, 5 ≈ 22, 6+ ≈ floor 15.
    return max(15, 82 - 12 * words)


def opportunity_score(
    impressions: int | None,
    position: float | None,
    difficulty: int | None,
) -> float:
    """0-100 opportunity: real demand × striking-distance position × easiness.

    The sweet spot is a keyword with meaningful GSC impressions, a position just
    off page one (roughly 5-20, where small gains move real traffic), on a SERP
    that isn't brutally hard. Keywords already at #1, or with no demand, score
    low because there's little upside to chase.
    """
    if not impressions:
        return 0.0

    # Demand component (log-scaled impressions → 0..1).
    from math import log10

    demand = min(1.0, log10(impressions + 1) / 4.0)  # ~10k impressions ≈ 1.0

    # Position component: peaks around positions 5-20 (striking distance).
    if position is None:
        pos_factor = 0.5
    elif position <= 3:
        pos_factor = 0.15  # already winning; little upside
    elif position <= 20:
        pos_factor = 1.0
    elif position <= 50:
        pos_factor = 0.6
    else:
        pos_factor = 0.3

    # Easiness component from difficulty (unknown difficulty = neutral 0.6).
    easiness = 0.6 if difficulty is None else (1.0 - difficulty / 100.0)

    return round(100.0 * demand * pos_factor * easiness, 2)


def confidence_score(source_count: int, has_gsc: bool) -> float:
    """0-100 confidence in a keyword's metrics.

    More independent sources corroborating a keyword → higher confidence, with a
    bonus when Google Search Console (ground-truth first-party data) is one of
    them.
    """
    base = min(source_count, 5) * 15.0  # up to 75
    if has_gsc:
        base += 25.0
    return round(min(100.0, base), 2)
