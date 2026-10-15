"""Page details + content suggestions for one action-plan keyword.

For a keyword (and optionally your ranking URL / the competitor's URL) this:
  1. picks YOUR page — the given URL, else your best-matching ranked/known page
     (keyword words in its URL), else recommends a new page;
  2. fetches it (SSRF-guarded, free) and checks the on-page basics against the
     keyword: title, H1, meta description, URL, word count, keyword mentions;
  3. fetches up to 2 competitor pages for the same keyword (the given competitor
     URL + top results from the stored SERP snapshot) — title, word count, H2s;
  4. returns rule-based, explainable suggestions: keyword placement, length vs
     competitors, subtopics they cover that you don't, questions to answer.

No paid API calls. Fetch failures degrade gracefully (the rest still returns).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from statistics import median
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.services import (
    audit_service, data_env, domain_analytics_service, domain_settings,
    ranked_keywords_service,
)

STOP = frozenset("a an and are as at be by for from how in is it of on or the to what when where which who why with vs".split())
MAX_COMPETITOR_PAGES = 2
_FETCH_WORKERS = 3


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in STOP]


def _contains_keyword(haystack: str, keyword: str) -> bool:
    """All meaningful keyword words appear (any order) — how SEOs check placement."""
    hay = set(_tokens(haystack))
    words = _tokens(keyword)
    return bool(words) and all(w in hay for w in words)


def _fetch_with_retry(url: str):
    """One retry on network errors (flaky connections / TLS timeouts)."""
    try:
        return audit_service.safe_fetch(url)
    except audit_service.FetchError:
        return audit_service.safe_fetch(url)


def _page(url: str) -> dict:
    """Fetch + extract the on-page basics. Never raises."""
    try:
        resp, final_url, _ms = _fetch_with_retry(url)
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
            return {"url": url, "ok": False, "error": f"HTTP {resp.status_code}"}
        soup = BeautifulSoup(resp.text, "html.parser")
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")][:3]
        # Subtopic headings from the main content only (not menus / sidebars).
        main = soup.find("main") or soup.find("article") or soup
        for tag in main.find_all(["nav", "aside", "footer"]):
            tag.extract()
        h2 = [h.get_text(" ", strip=True) for h in main.find_all(["h2", "h3"])]
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.extract()
        body = soup.get_text(" ", strip=True)
        words = [w for w in re.split(r"\s+", body) if w]
        return {
            "url": final_url, "ok": True, "title": title,
            "meta_description": (meta.get("content") or "").strip() if meta else "",
            "h1": h1, "headings": [h for h in h2 if 3 <= len(h) <= 120][:40],
            "word_count": len(words), "text": " ".join(words[:4000]),
        }
    except Exception as exc:  # noqa: BLE001 - a page we can't fetch must not break the panel
        return {"url": url, "ok": False, "error": str(exc)[:160]}


def _find_our_page(db: Session, domain: Domain, keyword: str, env: str, loc: int,
                   language: str) -> str | None:
    """Best existing page of ours for the keyword: most keyword words in its URL."""
    host = ranked_keywords_service.norm_host(domain.domain)
    urls: set[str] = set()
    run = ranked_keywords_service.latest_run(db, host, location_code=loc, language=language, env=env)
    if run is not None:
        urls |= {r.url for r in ranked_keywords_service.run_rows(db, run) if r.url}
    urls |= set(db.execute(text("SELECT url FROM opportunity_pages WHERE domain_id = :d"),
                           {"d": str(domain.id)}).scalars())
    words = set(_tokens(keyword))
    best, best_score = None, 0.0
    for u in urls:
        path_words = set(_tokens(urlparse(u).path.replace("-", " ").replace("_", " ")))
        score = len(words & path_words) / max(1, len(words))
        if score > best_score:
            best, best_score = u, score
    return best if best_score >= 0.5 else None


def _competitor_urls(db: Session, domain: Domain, keyword: str, env: str, given: str | None) -> list[str]:
    host = ranked_keywords_service.norm_host(domain.domain)
    urls = [given] if given else []
    rows = db.execute(text(r"""
        SELECT i.url, regexp_replace(lower(i.domain), '^www\.', '') AS dom
        FROM keywords k
        JOIN LATERAL (
          SELECT id FROM serp_snapshots s WHERE s.keyword_id = k.id AND s.environment = :env
          ORDER BY s.captured_at DESC LIMIT 1
        ) snap ON true
        JOIN serp_snapshot_items i ON i.snapshot_id = snap.id
        WHERE lower(k.keyword) = lower(:kw) AND i.url IS NOT NULL
        ORDER BY i.rank_absolute
        LIMIT 10
    """), {"kw": keyword, "env": env}).all()
    skip = ("youtube.com", "wikipedia.org", "quora.com", "reddit.com", "facebook.com")
    for r in rows:
        if r.dom != host and not any(s in r.dom for s in skip) and r.url not in urls:
            urls.append(r.url)
    return urls[:MAX_COMPETITOR_PAGES]


def _questions(db: Session, keyword: str) -> list[str]:
    """Related questions we already store (research 'questions' + question-form keywords)."""
    rows = db.execute(text("""
        SELECT DISTINCT rk.keyword FROM keywords k
        JOIN keyword_relations r ON r.keyword_id = k.id AND r.relation_type = 'question'
        JOIN keywords rk ON rk.id = r.related_keyword_id
        WHERE lower(k.keyword) = lower(:kw)
        LIMIT 8
    """), {"kw": keyword}).scalars().all()
    if len(rows) < 5:
        words = _tokens(keyword)[:3]
        if words:
            like = " AND ".join(f"lower(keyword) LIKE :w{i}" for i in range(len(words)))
            params = {f"w{i}": f"%{w}%" for i, w in enumerate(words)}
            rows += db.execute(text(
                f"SELECT keyword FROM keywords WHERE {like} "
                "AND keyword ~* '^(what|how|why|which|when|who|is|can|does)\\s' LIMIT 8"), params).scalars().all()
    return list(dict.fromkeys(rows))[:6]


def analyze(db: Session, domain: Domain, keyword: str, *, url: str | None = None,
            competitor_url: str | None = None, country: str | None = None) -> dict:
    s = domain_settings.resolve(domain, getattr(domain, "workspace", None))
    country = country or s.country
    env = data_env.env_for_domain(db, domain)
    loc = domain_analytics_service.location_code_for(country)
    keyword = " ".join(keyword.split())

    our_url = url or _find_our_page(db, domain, keyword, env, loc, s.language)
    comp_urls = _competitor_urls(db, domain, keyword, env, competitor_url)
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        ours_f = pool.submit(_page, our_url) if our_url else None
        comps = list(pool.map(_page, comp_urls))
    ours = ours_f.result() if ours_f else None
    questions = _questions(db, keyword)

    comp_ok = [c for c in comps if c.get("ok")]
    comp_words = [c["word_count"] for c in comp_ok if c["word_count"] > 0]
    target_words = int(median(comp_words)) if comp_words else None

    suggestions: list[dict] = []

    def add(priority: str, area: str, text_: str) -> None:
        suggestions.append({"priority": priority, "area": area, "text": text_})

    checks: list[dict] = []
    if ours and ours.get("ok"):
        in_title = _contains_keyword(ours["title"], keyword)
        in_h1 = any(_contains_keyword(h, keyword) for h in ours["h1"])
        in_meta = _contains_keyword(ours["meta_description"], keyword)
        in_url = _contains_keyword(urlparse(ours["url"]).path.replace("-", " "), keyword)
        mentions = len(re.findall(re.escape(keyword.lower()), ours["text"].lower()))
        checks = [
            {"label": "Keyword in title", "ok": in_title, "value": ours["title"] or "—"},
            {"label": "Keyword in H1", "ok": in_h1, "value": ours["h1"][0] if ours["h1"] else "No H1"},
            {"label": "Keyword in meta description", "ok": in_meta, "value": ours["meta_description"] or "Missing"},
            {"label": "Keyword in URL", "ok": in_url, "value": urlparse(ours["url"]).path or "/"},
            {"label": "Exact keyword mentions", "ok": mentions >= 2, "value": str(mentions)},
            {"label": "Word count", "ok": target_words is None or ours["word_count"] >= 0.8 * target_words,
             "value": f"{ours['word_count']:,}" + (f" (competitors ~{target_words:,})" if target_words else "")},
        ]
        if not in_title:
            add("high", "Title", f"Put “{keyword}” in the title tag, near the start (keep it under ~60 characters).")
        if not in_h1:
            add("high", "Heading", f"Use “{keyword}” in the page's H1 heading." if ours["h1"] else f"Add an H1 heading that contains “{keyword}”.")
        if not in_meta:
            add("medium", "Meta", f"Write a meta description (~150 characters) that includes “{keyword}” and a reason to click.")
        if mentions < 2:
            add("medium", "Content", f"Mention “{keyword}” naturally in the first paragraph and a couple more times in the body.")
        if target_words and ours["word_count"] < 0.8 * target_words:
            add("high", "Length", f"Expand the page from {ours['word_count']:,} to about {target_words:,} words — the ranking competitors are that deep.")
        our_text = ours["text"].lower() + " " + " ".join(ours["headings"]).lower()
    elif ours is not None:
        # The page exists but couldn't be loaded — never tell the user to create it.
        our_text = ""
        add("high", "Page", f"Couldn't load your page right now ({ours.get('error', 'unreachable')}). "
            "Try again in a moment; the suggestions below come from the competitor pages.")
    else:
        our_text = ""
        add("high", "Page", f"Create a dedicated page for “{keyword}”: keyword in the URL, title and H1"
            + (f", about {target_words:,} words" if target_words else "") + ".")

    # Subtopics competitors cover (their headings) that your page doesn't.
    missing: list[str] = []
    kw_words = set(_tokens(keyword))
    for c in comp_ok:
        brand = ranked_keywords_service.norm_host(c["url"]).split(".")[0]
        for h in c["headings"]:
            words = set(_tokens(h))
            if len(words) < 2 or h in missing:
                continue
            # On-topic only: shares a keyword word or matches the relevance rule,
            # and isn't the competitor's own brand/section name.
            if not (words & kw_words or (s.topic_regex and re.search(s.topic_regex, h, re.I))):
                continue
            if brand and brand[:5] in "".join(_tokens(h)):
                continue
            covered = sum(1 for w in words if w in our_text) / len(words)
            if covered < 0.5:
                missing.append(h)
    for h in missing[:6]:
        add("medium", "Subtopic", f"Cover this subtopic the competitors have: “{h}”.")
    for q in questions[:4]:
        if not our_text or not _contains_keyword(our_text, q):
            add("low", "FAQ", f"Answer the question “{q}” (an FAQ section works well).")

    order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: order[s["priority"]])
    strip = lambda p: {k: v for k, v in p.items() if k != "text"} if p else None  # noqa: E731
    return {
        "keyword": keyword,
        "your_page": strip(ours),
        "page_found": bool(our_url),
        "checks": checks,
        "competitor_pages": [strip(c) for c in comps],
        "target_word_count": target_words,
        "questions": questions,
        "suggestions": suggestions,
    }
