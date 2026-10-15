"""On-page keyword / content analysis.

Extracts the top keyword phrases from a page and analyzes how well a page
targets a given keyword. Pure functions are unit-testable; the fetch reuses the
SSRF-guarded fetcher in audit_service.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from app.services import audit_service
from app.services.audit_service import (  # noqa: F401 (re-exported)
    BlockedUrlError,
    FetchError,
    InvalidUrlError,
)

MAX_HTML_BYTES = 3_000_000

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "of",
    "to", "in", "on", "at", "by", "with", "from", "as", "is", "are", "was",
    "were", "be", "been", "being", "this", "that", "these", "those", "it",
    "its", "you", "your", "we", "our", "us", "they", "their", "them", "he",
    "she", "his", "her", "i", "me", "my", "so", "no", "not", "can", "will",
    "just", "do", "does", "did", "has", "have", "had", "how", "what", "when",
    "where", "who", "why", "which", "all", "any", "more", "most", "other",
    "some", "such", "than", "too", "very", "s", "t", "up", "out", "about",
    "into", "over", "also", "get", "got", "one", "two", "may", "each", "here",
    "there", "would", "could", "should", "us", "via", "per", "use", "using",
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")


@dataclass
class KeywordAnalysis:
    url: str
    word_count: int
    unigrams: list[dict[str, Any]] = field(default_factory=list)
    bigrams: list[dict[str, Any]] = field(default_factory=list)
    trigrams: list[dict[str, Any]] = field(default_factory=list)
    target: dict[str, Any] | None = None


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.extract()
    return soup.get_text(" ", strip=True)


def tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _density(count: int, total: int) -> float:
    return round(100 * count / total, 2) if total else 0.0


def top_unigrams(tokens: list[str], k: int = 10) -> list[dict[str, Any]]:
    total = len(tokens)
    counts = Counter(
        w for w in tokens if w not in STOPWORDS and len(w) > 2
    )
    return [
        {"phrase": w, "count": c, "density": _density(c, total)}
        for w, c in counts.most_common(k)
    ]


def top_ngrams(tokens: list[str], n: int, k: int = 10) -> list[dict[str, Any]]:
    total = len(tokens)
    grams: Counter[str] = Counter()
    for i in range(len(tokens) - n + 1):
        window = tokens[i : i + n]
        # Skip phrases that begin or end with a stopword, or are all short.
        if window[0] in STOPWORDS or window[-1] in STOPWORDS:
            continue
        if all(len(w) <= 2 for w in window):
            continue
        grams[" ".join(window)] += 1
    return [
        {"phrase": p, "count": c, "density": _density(c, total)}
        for p, c in grams.most_common(k)
        if c > 1
    ]


def keyword_presence(soup: BeautifulSoup, text: str, term: str) -> dict[str, Any]:
    term = term.strip()
    term_l = term.lower()
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    h1 = " ".join(h.get_text(" ", strip=True) for h in soup.find_all("h1")).lower()
    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = (desc_tag.get("content", "") if desc_tag else "").lower()

    total_words = len(tokenize(text))
    occurrences = len(re.findall(re.escape(term_l), text.lower())) if term_l else 0
    term_words = max(1, len(term_l.split()))
    density = _density(occurrences * term_words, total_words)

    in_title = term_l in title
    in_h1 = term_l in h1
    in_meta = term_l in desc

    score = 0
    if in_title:
        score += 30
    if in_h1:
        score += 25
    if in_meta:
        score += 20
    if occurrences > 0:
        score += 15
    if 0.3 <= density <= 3.0:
        score += 10

    return {
        "term": term,
        "in_title": in_title,
        "in_h1": in_h1,
        "in_meta_description": in_meta,
        "occurrences": occurrences,
        "density": density,
        "score": score,
    }


def analyze_html(html: str, url: str, term: str | None = None) -> KeywordAnalysis:
    soup = BeautifulSoup(html, "html.parser")
    text = _visible_text(soup)
    tokens = tokenize(text)

    result = KeywordAnalysis(
        url=url,
        word_count=len(tokens),
        unigrams=top_unigrams(tokens),
        bigrams=top_ngrams(tokens, 2),
        trigrams=top_ngrams(tokens, 3),
    )
    if term and term.strip():
        result.target = keyword_presence(soup, text, term)
    return result


def run_analysis(url: str, term: str | None = None) -> KeywordAnalysis:
    response, final_url, _ms = audit_service.safe_fetch(url)
    content = (response.content or b"")[:MAX_HTML_BYTES]
    html = content.decode(response.encoding or "utf-8", errors="replace")
    return analyze_html(html, final_url, term)
