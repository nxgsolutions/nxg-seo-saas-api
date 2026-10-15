"""LLM competitor discovery (backend port of scraping/seo_competitor_finder.py).

Given a target domain: scrape its homepage (+ an about/services page) for context,
ask the LLM to summarize the business, then ask it for the top N direct
competitors with a reason and confidence level. Uses OpenAI (settings.OPENAI_*),
mirroring the standalone script.

The result feeds competitor_service.discover_and_store(), which persists the
competitors into tracked_competitors (source='ai') and snapshots ranks.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.services import audit_service

# Internal links worth scraping for extra context (about / services / pricing).
_CONTEXT_LINK = re.compile(
    r"about|who-we-are|company|service|product|feature|solution|course|plan|pricing",
    re.I,
)
_SKIP_EXT = re.compile(r"\.(pdf|jpg|jpeg|png|gif|zip|mp4|css|js)$", re.I)


class DiscoveryError(RuntimeError):
    """Discovery failed (LLM unavailable, unreachable domain, bad reply)."""


# ── Scraping ────────────────────────────────────────────────────────────────

def _parse_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    md = soup.find("meta", attrs={"name": "description"})
    meta = (md.get("content") or "").strip() if md else ""
    headings = [
        h.get_text(" ", strip=True)
        for h in soup.find_all(["h1", "h2", "h3"])
        if h.get_text(strip=True)
    ][:20]
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:4000]
    return {"title": title, "meta": meta, "headings": headings, "text": text}


def _internal_links(base_url: str, html: str, limit: int = 2) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        if urlparse(full).netloc != base_host or full in seen:
            continue
        if _SKIP_EXT.search(full):
            continue
        hay = f"{full} {a.get_text(' ', strip=True)}"
        if _CONTEXT_LINK.search(hay):
            seen.add(full)
            out.append(full)
        if len(out) >= limit:
            break
    return out


def scrape_context(target_domain: str) -> str:
    """Fetch the homepage (+ up to 2 context pages) and return a text corpus.
    Uses the SSRF-guarded fetch. Raises DiscoveryError if unreachable."""
    host = target_domain.strip().replace("https://", "").replace("http://", "").strip("/")
    try:
        resp, final_url, _ = audit_service.safe_fetch(f"https://{host}")
    except Exception as exc:  # noqa: BLE001
        raise DiscoveryError(f"Could not fetch {host}: {exc}") from exc

    pages = [_parse_page(resp.text)]
    for link in _internal_links(final_url, resp.text, limit=2):
        try:
            r, _, _ = audit_service.safe_fetch(link)
            pages.append(_parse_page(r.text))
        except Exception:  # noqa: BLE001 - a broken subpage shouldn't abort
            continue

    parts = []
    for p in pages:
        parts.append(
            f"TITLE: {p['title']}\nMETA: {p['meta']}\n"
            f"HEADINGS: {' | '.join(p['headings'])}\nTEXT: {p['text']}"
        )
    return "\n\n---\n\n".join(parts)[:12000]


# ── OpenAI ──────────────────────────────────────────────────────────────────

def _openai_json(system: str, user: str, *, max_tokens: int = 4096):
    """Call OpenAI and parse a JSON value (object or array) from the reply.
    Handles the gpt-5 family (max_completion_tokens + reasoning_effort, default
    temperature) vs gpt-4.x (max_tokens + temperature=0)."""
    if not settings.openai_configured:
        raise DiscoveryError(
            "OpenAI is not configured. Set OPENAI_API_KEY in the backend .env."
        )
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    model = settings.OPENAI_MODEL
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if model.startswith("gpt-5"):
        kwargs["max_completion_tokens"] = max_tokens
        kwargs["reasoning_effort"] = "low"
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise DiscoveryError(f"OpenAI request failed: {exc}") from exc

    text = (resp.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    # Extract the first JSON array or object.
    match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if not match:
        raise DiscoveryError("OpenAI returned no parseable JSON.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DiscoveryError("OpenAI returned invalid JSON.") from exc


# ── Pipeline ────────────────────────────────────────────────────────────────

def _extract_business(corpus: str, domain: str) -> dict:
    system = "You are a SEO analyst. Reply with a single valid JSON object only."
    user = f"""From the scraped website content of "{domain}" below, build an accurate, self-contained business profile that another analyst could use to find direct competitors WITHOUT seeing the website.

Return ONLY JSON with keys:
- "business_description": 2-3 sentences: what the business does, its delivery model (e.g. online platform / app / coaching), and what makes it distinctive (e.g. AI-powered, adaptive). Be specific to THIS site, not generic.
- "industry": short industry/niche label.
- "services": array of 5-10 CONCRETE services, products or features actually offered. Capture distinctive and technology/AI-powered features EXPLICITLY and separately when the site mentions them. Use the site's own wording; do not invent features that aren't there.
- "target_audience": who the customers are, including the market/country and their stage/segment.
- "market": the primary country/market (e.g. "India").

Website content:
{corpus}
"""
    data = _openai_json(system, user, max_tokens=2048)
    if not isinstance(data, dict):
        raise DiscoveryError("Business extraction returned an unexpected shape.")
    return data


def _competitor_prompt(biz: dict, market: str, limit: int) -> str:
    services = biz.get("services") or []
    services_txt = "\n".join(str(s) for s in services) if services else "n/a"
    return f"""Find exactly {limit} direct competitors in {market} for the following business.

Business Description:
{biz.get('business_description', 'n/a')}

Industry:
{biz.get('industry', 'n/a')}

Services / Features:
{services_txt}

Target Audience:
{biz.get('target_audience', 'n/a')}

Select competitors by how closely their products, services, features, target audience and business model overlap with the business above — not by company size or popularity. Pay special attention to the distinctive or specialized items in the Services / Features list and prioritize competitors that offer those same capabilities, including newer or digital-first platforms, not only large established players. Rank the most similar first.

For each competitor, provide: name, domain, reason, confidence_level.
The reason must explain the specific feature/service overlap with the business above.
Confidence level must be one of: Very High, High, Medium, Low.

Return exactly {limit} competitors as a JSON object of this shape:
{{"competitors": [{{"name": "Example", "domain": "example.com", "reason": "...", "confidence_level": "High"}}]}}
Return only valid JSON. No markdown, explanations or extra text."""


def _norm_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[4:] if d.startswith("www.") else d


def discover(
    target_domain: str, *, limit: int = 10, extra_services: list[str] | None = None
) -> dict:
    """Scrape the domain, summarize the business, and return top competitors.
    `extra_services` are appended to the scraped services (for features not yet on
    the site, e.g. "AI-based Mains answer evaluation") to steer the match.
    Returns {"business": {...}, "market": str, "competitors": [{name, domain,
    reason, confidence_level}]}."""
    corpus = scrape_context(target_domain)
    biz = _extract_business(corpus, target_domain)

    if extra_services:
        scraped = biz.get("services") or []
        existing = {str(s).lower() for s in scraped}
        biz["services"] = scraped + [
            s.strip() for s in extra_services
            if s.strip() and s.strip().lower() not in existing
        ]

    market = str(biz.get("market") or "India").strip() or "India"

    system = (
        "You are an SEO competitor-research analyst. Reply with only valid JSON, "
        "no markdown."
    )
    data = _openai_json(system, _competitor_prompt(biz, market, limit), max_tokens=4096)

    # Accept a bare array or an object wrapping the array.
    rows = data if isinstance(data, list) else (
        data.get("competitors") or data.get("results") or []
    )
    self_host = _norm_domain(target_domain)
    out, seen = [], set()
    for c in rows:
        if not isinstance(c, dict):
            continue
        dom = _norm_domain(c.get("domain") or "")
        if not dom or "." not in dom or dom == self_host or dom in seen:
            continue
        seen.add(dom)
        out.append({
            "name": (c.get("name") or dom).strip()[:255],
            "domain": dom,
            "reason": (c.get("reason") or "").strip()[:600],
            "confidence_level": (c.get("confidence_level") or c.get("confidence") or "").strip()[:20],
        })
        if len(out) >= limit:
            break

    return {"business": biz, "market": market, "competitors": out}
