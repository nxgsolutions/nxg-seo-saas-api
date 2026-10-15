"""Free keyword expansion via Google Autocomplete — keyless, no paid provider.

Hits Google's public Suggest endpoint (the same one that powers the search-box
dropdown). No API key, no quota. The host is fixed, so there is no SSRF surface.
Best-effort: any network/parse failure returns an empty list so the pipeline
degrades gracefully to whatever other sources are available.

This gives demand *breadth* (what people actually type) — never a volume.
"""

from __future__ import annotations

import httpx

# Fixed host — never interpolate user input into the host/path, only the query.
_SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
_TIMEOUT = httpx.Timeout(5.0)


def autocomplete(seed: str, country: str = "us", language: str = "en") -> list[str]:
    """Return Google Autocomplete suggestions for a seed keyword (best-effort)."""
    seed = seed.strip()
    if not seed:
        return []
    try:
        resp = httpx.get(
            _SUGGEST_URL,
            params={"client": "firefox", "q": seed, "gl": country, "hl": language},
            timeout=_TIMEOUT,
            follow_redirects=False,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    # Firefox client returns [query, [suggestions...]].
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
        return [s for s in data[1] if isinstance(s, str) and s.strip()]
    return []


# Question openers used to mine "People-Also-Ask"-style questions from
# Autocomplete without scraping the SERP.
_QUESTION_PREFIXES = ("how", "what", "why", "when", "which", "where", "who", "is", "can")


def questions(seed: str, country: str = "us", language: str = "en", limit: int = 12) -> list[str]:
    """Question-form suggestions for a seed, mined from Autocomplete prefixes.

    Fills the "Questions" panel with real user phrasing (e.g. "how to start
    UPSC preparation") — no SERP scraping required. Best-effort and de-duplicated.
    """
    seed = seed.strip()
    if not seed:
        return []
    seen: dict[str, None] = {}
    for prefix in _QUESTION_PREFIXES:
        for suggestion in autocomplete(f"{prefix} {seed}", country, language):
            text = suggestion.strip()
            key = text.lower()
            # Keep only genuine questions that still mention the seed topic.
            if key in seen or "?" not in text and not _looks_like_question(text):
                continue
            seen[key] = None
            if len(seen) >= limit:
                return list(seen.keys())
    return list(seen.keys())


def _looks_like_question(text: str) -> bool:
    first = text.lower().split(" ", 1)[0]
    return first in _QUESTION_PREFIXES


def expand(seeds: list[str], country: str = "us", language: str = "en") -> list[str]:
    """Expand several seeds, de-duplicated, preserving first-seen order."""
    seen: dict[str, None] = {}
    for seed in seeds:
        for suggestion in autocomplete(seed, country, language):
            key = suggestion.lower().strip()
            if key not in seen:
                seen[key] = None
    return list(seen.keys())


# --- Categorized expansion (Ubersuggest-style idea tabs) --------------------
# All keyless, all from Google Autocomplete — just seeded with different modifiers.
from concurrent.futures import ThreadPoolExecutor

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_PREPOSITIONS = ("for", "with", "without", "to", "near", "in", "on", "is", "are", "can", "of")
_COMPARISONS = ("vs", "versus", "or", "and", "like", "alternative")
_MAX_WORKERS = 10


def _fetch_all(seeds: list[str], country: str, language: str) -> dict[str, list[str]]:
    """Fetch autocomplete for many seeds concurrently. Returns seed -> suggestions."""
    unique = list(dict.fromkeys(seeds))
    out: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        for s, res in zip(unique, pool.map(lambda q: autocomplete(q, country, language), unique)):
            out[s] = res
    return out


def _bucket(fetched: dict[str, list[str]], seeds: list[str], seed_norm: str, limit: int) -> list[str]:
    """Collect suggestions from already-fetched seeds; dedupe, drop bare seed, cap."""
    seen: dict[str, None] = {}
    for s in seeds:
        for suggestion in fetched.get(s, []):
            key = suggestion.strip().lower()
            if not key or key == seed_norm or key in seen:
                continue
            seen[key] = None
            if len(seen) >= limit:
                return list(seen.keys())
    return list(seen.keys())


def ideas(seed: str, country: str = "us", language: str = "en") -> dict[str, list[str]]:
    """All idea categories for a seed, keyless. One concurrent batch of Autocomplete
    calls feeds every tab. Categories may overlap (like Ubersuggest's per-tab counts)."""
    seed = seed.strip()
    seed_norm = seed.lower()
    if not seed:
        return {k: [] for k in ("autocomplete", "related", "questions", "prepositions", "comparisons")}

    ac_seeds = [seed]
    related_seeds = [f"{seed} {c}" for c in _ALPHABET]
    q_seeds = [f"{p} {seed}" for p in _QUESTION_PREFIXES]
    prep_seeds = [f"{seed} {p}" for p in _PREPOSITIONS]
    comp_seeds = [f"{seed} {c}" for c in _COMPARISONS]

    fetched = _fetch_all(ac_seeds + related_seeds + q_seeds + prep_seeds + comp_seeds, country, language)

    q_raw = _bucket(fetched, q_seeds, seed_norm, limit=40)
    return {
        "autocomplete": _bucket(fetched, ac_seeds, seed_norm, limit=80),
        "related": _bucket(fetched, related_seeds, seed_norm, limit=80),
        "questions": [q for q in q_raw if "?" in q or _looks_like_question(q)][:30],
        "prepositions": _bucket(fetched, prep_seeds, seed_norm, limit=40),
        "comparisons": _bucket(fetched, comp_seeds, seed_norm, limit=30),
    }
