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

# Whole-word token cues for rule-based intent. Precedence (checked in this order):
# transactional > commercial > navigational > informational, so "best price" reads
# as transactional and "how much does it cost" isn't misread as a "how" question.
# Only low-polysemy words live here; ambiguous multi-word cues are patterns below.
_TRANSACTIONAL = {
    "buy", "price", "prices", "cheap", "cheapest", "deal", "deals", "discount",
    "discounts", "coupon", "coupons", "order", "purchase", "shop", "sale", "cost",
    "costs", "subscription", "subscribe", "pricing",
}
_COMMERCIAL = {
    "best", "top", "review", "reviews", "vs", "versus", "compare", "comparison",
    "alternative", "alternatives", "software", "tool", "tools", "service",
    "services", "platform", "agency", "company", "companies", "provider",
    "providers", "vendor", "vendors",
}
_INFORMATIONAL = {
    "how", "what", "why", "when", "where", "who", "which", "guide", "guides",
    "tutorial", "tutorials", "examples", "example", "ideas", "tips", "meaning",
    "definition", "learn", "template", "templates", "checklist", "steps",
    "benefits", "difference", "explained", "faq",
}
# "near" (local) and spaced "sign in" forms are handled by patterns below, not as
# bare tokens (so "sign language" isn't misread as navigational).
_NAVIGATIONAL = {"login", "signin", "download", "app", "official", "portal"}

# Multi-word / contextual cues. A pattern hit is a stronger signal (0.9) than a
# bare token (0.8). Same precedence as the sets above.
_TRANSACTIONAL_PAT = [
    re.compile(p) for p in (
        r"\bnear me\b", r"\bfor sale\b", r"\bfor rent\b", r"\bwhere to buy\b",
        r"\bbest price\b", r"\bget a quote\b", r"\border online\b", r"\bbook (a|an|my)\b",
    )
]
_COMMERCIAL_PAT = [
    re.compile(p) for p in (r"\bvs\.?\b", r"\btop \d+\b", r"\balternatives? to\b")
]
_NAVIGATIONAL_PAT = [
    re.compile(p) for p in (
        r"\bsign in\b", r"\bsign up\b", r"\blog in\b",
        r"\bofficial (site|website|page)\b", r"\bcustomer (login|portal)\b",
    )
]
_INFORMATIONAL_PAT = [
    re.compile(p) for p in (
        r"\bhow to\b", r"\bwhat is\b", r"\bwhat are\b", r"\bwhy (is|are|do|does)\b",
        r"\bways to\b", r"\bideas for\b", r"\bhow do i\b", r"\bstep by step\b",
    )
]
# Query-structure signal: a keyword that starts like a question is informational.
_QUESTION_STARTS = {
    "how", "what", "why", "when", "where", "who", "which", "whose", "whom",
    "is", "are", "can", "could", "should", "do", "does", "did", "will",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_keyword(keyword: str) -> str:
    """Canonical form for de-duplication: lowercase, collapsed whitespace."""
    return " ".join(keyword.lower().split())


def _tokens(keyword: str) -> set[str]:
    return set(_WORD_RE.findall(keyword.lower()))


def _is_question(kw: str, tokens: set[str]) -> bool:
    if kw.strip().endswith("?"):
        return True
    words = _WORD_RE.findall(kw)
    return len(words) >= 2 and words[0] in _QUESTION_STARTS


def classify_intent_scored(keyword: str) -> tuple[str, float]:
    """Rule-based intent AND a 0-1 confidence. Returns ('unknown', 0.0) when no
    cue matches, so a SERP/AI layer can resolve the genuinely ambiguous residual.

    Confidence: multi-word pattern hit = 0.9, question structure = 0.85, bare
    token cue = 0.8. This is the deterministic Layer 1 of the hybrid classifier
    in `intent_service`."""
    kw = keyword.lower().strip()
    tokens = _tokens(kw)

    def _hit(patterns) -> bool:
        return any(p.search(kw) for p in patterns)

    if _hit(_TRANSACTIONAL_PAT):
        return "transactional", 0.9
    if tokens & _TRANSACTIONAL:
        return "transactional", 0.8
    if _hit(_COMMERCIAL_PAT):
        return "commercial", 0.9
    if tokens & _COMMERCIAL:
        return "commercial", 0.8
    if _hit(_NAVIGATIONAL_PAT):
        return "navigational", 0.9
    if tokens & _NAVIGATIONAL:
        return "navigational", 0.8
    if _hit(_INFORMATIONAL_PAT):
        return "informational", 0.9
    if _is_question(kw, tokens):
        return "informational", 0.85
    if tokens & _INFORMATIONAL:
        return "informational", 0.8
    return "unknown", 0.0


def classify_intent(keyword: str) -> str:
    """Best-effort search intent from keyword cues. Returns one of INTENTS.

    Thin wrapper over `classify_intent_scored` (drops the confidence) so every
    existing caller keeps the same string contract while benefiting from the
    upgraded rules."""
    return classify_intent_scored(keyword)[0]


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
