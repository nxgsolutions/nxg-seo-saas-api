"""Content Optimization: score a page for a target keyword and plan improvements.

One run:
  1. fetch the page (SSRF-guarded) and extract its on-page facts;
  2. pick the pages to compare with — the stored Google results for the
     keyword (DB only, never a paid call) and/or URLs the user typed — and
     fetch them in parallel (free, plain HTTP);
  3. build a deterministic checklist + 0-100 score (content_score.py);
  4. ask AI (openai_ai.write_json: OpenAI gpt-4o-mini, local model fallback) to WRITE suggestions:
     title / meta options, an outline with the missing sections, an intro and
     prioritized recommendations. If AI is off or fails, the rule-based result
     is still returned and saved — there is no sample/placeholder output.
The run is saved as a session (private to the workspace).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.content_optimization import ContentOptimization
from app.models.domain import Domain
from app.models.workspace import Workspace
from app.services import (
    audit_service, content_score, data_env, openai_ai, page_history_service, serp_scrape_service,
)
from app.services.ranked_keywords_service import norm_host

MAX_COMPETITORS = 5
MAX_HTML_BYTES = 2_500_000
SUMMARY_VERSION = 2


class ContentFetchError(Exception):
    pass


# ── Fetching ────────────────────────────────────────────────────────────────

def _fetch_html(url: str) -> tuple[str, str]:
    """(html, final_url). Raises ContentFetchError with a readable reason."""
    try:
        response, final_url, _ms = audit_service.safe_fetch(url)
    except (audit_service.InvalidUrlError, audit_service.BlockedUrlError, audit_service.FetchError) as exc:
        raise ContentFetchError(str(exc)) from exc
    ctype = response.headers.get("content-type", "")
    if ctype and "html" not in ctype.lower():
        raise ContentFetchError("Not an HTML page.")
    if response.status_code in (401, 403, 429, 503):
        raise ContentFetchError(
            f"The site blocked our reader (HTTP {response.status_code}). Its firewall or bot protection "
            "doesn't allow automated visits.")
    if response.status_code >= 400:
        raise ContentFetchError(f"The page returned HTTP {response.status_code}.")
    html = (response.content or b"")[:MAX_HTML_BYTES].decode(response.encoding or "utf-8", errors="replace")
    return html, final_url


def _competitor_targets(db: Session, workspace: Workspace, keyword: str | None, country: str,
                        own_hosts: set[str], manual: list[str]) -> tuple[list[dict], str, str | None]:
    """Pages to compare with: typed URLs first, then the stored SERP's organic results."""
    targets: list[dict] = []
    seen: set[str] = set()
    for u in manual:
        u = u.strip()
        if not u:
            continue
        if not re.match(r"^https?://", u, re.I):
            u = "https://" + u
        host = norm_host(u)
        if host and host not in own_hosts and u not in seen:
            seen.add(u)
            targets.append({"url": u, "host": host, "position": None})
    source = "manual" if targets else "none"
    captured = None
    if keyword:
        snap = serp_scrape_service.latest_snapshot(
            db, keyword, country=country, env=data_env.env_for_workspace(workspace))
        if snap:
            captured = snap.get("captured_at")
            hosts_taken = {t["host"] for t in targets}
            added = 0
            for r in snap["rows"]:
                if len(targets) >= MAX_COMPETITORS:
                    break
                if (r.get("type") or "organic") != "organic" or not r.get("url"):
                    continue
                host = norm_host(r.get("domain") or r["url"])
                if not host or host in own_hosts or host in hosts_taken:
                    continue
                hosts_taken.add(host)
                targets.append({"url": r["url"], "host": host, "position": r.get("position")})
                added += 1
            if added:
                source = "serp+manual" if source == "manual" else "serp"
    return targets[:MAX_COMPETITORS], source, captured


def _fetch_competitor(target: dict) -> dict:
    try:
        html, final = _fetch_html(target["url"])
        facts = content_score.extract(html, final)
        if facts["word_count"] < 100:
            return {**target, "error": "Too little readable text (likely rendered by JavaScript)."}
        return {**target, "facts": facts}
    except ContentFetchError as exc:
        return {**target, "error": str(exc)[:200]}
    except Exception:  # noqa: BLE001 — one bad page must not fail the run
        return {**target, "error": "Could not read this page."}


# ── AI writing ──────────────────────────────────────────────────────────────

AI_SYSTEM = (
    "You are a senior SEO content editor. You get facts about a web page, its target keyword, "
    "a checklist of failed checks, and what the top-ranking pages for the keyword cover. "
    "Write practical, specific improvements for THIS page in the page's own language and tone. "
    "Rules: do not invent statistics, prices, dates or claims; do not mention competitor brand names; "
    "keep the page's topic. Return ONLY a JSON object with keys:\n"
    '  "summary": "<2-3 sentences: where the page stands and the 2 biggest wins; '
    'only use numbers that appear in the input>",\n'
    '  "title_options": ["<3 titles, 45-60 characters, keyword near the start>"],\n'
    '  "meta_options": ["<2 meta descriptions, 130-155 characters, with a clear benefit>"],\n'
    '  "outline": [{"heading": "<H2 or H3 text>", "level": 2, "is_new": true, '
    '"note": "<one short line: what to cover / why>"}],\n'
    '  "intro": "<improved opening paragraph, 2-4 sentences, keyword in the first sentence>",\n'
    '  "recommendations": [{"area": "title|meta|headings|content|keywords|readability|links|media", '
    '"recommendation": "<the action to take — what to change and why, in one short line>", '
    '"suggestion": "<when the action is to rewrite or add wording (a title, meta description, heading or '
    'sentence), the EXACT ready-to-use text to paste in; leave as an empty string when the action is not '
    'a text change>", "priority": "high|medium|low"}]\n'
    "The outline should keep the page's useful existing sections (is_new=false) and add the missing "
    "subtopics the top pages cover (is_new=true), 6-12 items. Give 5-8 recommendations, most impactful first. "
    'Keep "recommendation" as the instruction (what to do); put any ready-to-use wording in "suggestion".'
)


def _ai_prompt(facts: dict, keyword: str | None, checks: list[dict], bench: dict | None,
               terms: list[dict], competitors: list[dict]) -> str:
    failed = [f"- {c['label']}: {c['value']} → {c['tip']}" for c in checks if c["status"] != "pass"]
    missing = [t["term"] for t in terms if t["status"] != "ok"][:25]
    comp_heads = []
    for c in competitors:
        if c.get("facts"):
            heads = [h["text"] for h in c["facts"]["headings"] if h["level"] == 2][:10]
            comp_heads.append(f"- Page #{c.get('position') or '?'}: " + " | ".join(heads))
    payload = {
        "url": facts["url"],
        "target_keyword": keyword or "(none given — infer the main topic)",
        "title": facts["title"],
        "meta_description": facts["meta"],
        "h1": facts["h1"],
        "headings": [f"H{h['level']}: {h['text']}" for h in facts["headings"][:30]],
        "word_count": facts["word_count"],
        "opening": facts["first_paragraph"],
        "body_excerpt": facts["body"][:3500],
    }
    lines = ["PAGE:", json.dumps(payload, ensure_ascii=False), "", "FAILED / WEAK CHECKS:", *(failed or ["(none)"])]
    if bench:
        lines += ["", f"TOP PAGES (median of {bench['pages']}): {round(bench['word_count'] or 0)} words, "
                      f"{round(bench['headings'] or 0)} subheadings."]
    if missing:
        lines += ["", "TERMS MOST TOP PAGES USE THAT THIS PAGE MISSES OR UNDERUSES: " + ", ".join(missing)]
    if comp_heads:
        lines += ["", "TOP PAGES' H2 SECTIONS:", *comp_heads]
    return "\n".join(lines)


def _clean_ai(data: dict) -> dict:
    def strs(v: Any, n: int, limit: int) -> list[str]:
        return [str(x).strip()[:limit] for x in (v or []) if str(x).strip()][:n] if isinstance(v, list) else []

    outline = []
    for o in data.get("outline") or []:
        if isinstance(o, dict) and str(o.get("heading", "")).strip():
            level = o.get("level")
            outline.append({
                "heading": str(o["heading"]).strip()[:160],
                "level": 3 if level in (3, "3") else 2,
                "note": str(o.get("note") or "").strip()[:240],
                "is_new": bool(o.get("is_new", True)),
            })
    recs = []
    for r in data.get("recommendations") or []:
        if isinstance(r, dict) and str(r.get("recommendation", "")).strip():
            pr = str(r.get("priority") or "medium").lower()
            recs.append({
                "area": str(r.get("area") or "content").lower()[:20],
                "recommendation": str(r["recommendation"]).strip()[:400],
                "suggestion": str(r.get("suggestion") or "").strip()[:600] or None,
                "priority": pr if pr in ("high", "medium", "low") else "medium",
            })
    return {
        "title_options": strs(data.get("title_options"), 3, 120),
        "meta_options": strs(data.get("meta_options"), 3, 220),
        "outline": outline[:14],
        "intro": str(data.get("intro") or "").strip()[:1200],
        "recommendations": recs[:8],
    }


_NUM = re.compile(r"\d[\d,.]*")


def _numbers_ok(summary: str, facts_text: str) -> bool:
    """Number guard: the AI summary may only use numbers present in the facts."""
    allowed = {n.replace(",", "").rstrip(".") for n in _NUM.findall(facts_text)}
    return all(n.replace(",", "").rstrip(".") in allowed for n in _NUM.findall(summary))


def _rule_summary(score: int, checks: list[dict], facts: dict, bench: dict | None, terms: list[dict]) -> str:
    passed = sum(1 for c in checks if c["status"] == "pass")
    parts = [f"Content score {score}/100 — {passed} of {len(checks)} checks pass."]
    if bench and bench.get("word_count"):
        parts.append(f"The page has {facts['word_count']:,} words; the top {bench['pages']} pages have about "
                     f"{round(bench['word_count']):,}.")
    missing = sum(1 for t in terms if t["status"] == "missing")
    if missing:
        parts.append(f"It misses {missing} terms that most top pages use.")
    worst = sorted((c for c in checks if c["status"] == "fail"), key=lambda c: -c["weight"])[:2]
    if worst:
        parts.append("Biggest fixes: " + "; ".join(c["label"].lower() for c in worst) + ".")
    return " ".join(parts)


# ── Run ─────────────────────────────────────────────────────────────────────

def _own_domain_id(db: Session, workspace: Workspace, domain_id: str | None) -> uuid.UUID | None:
    """The id if it is a website of THIS workspace, else None."""
    try:
        did = uuid.UUID(domain_id) if domain_id else None
    except ValueError:
        return None
    dom = db.get(Domain, did) if did else None
    return did if dom is not None and dom.workspace_id == workspace.id else None


def _own_hosts(db: Session, workspace: Workspace) -> set[str]:
    return {norm_host(d) for d in db.execute(
        select(Domain.domain).where(Domain.workspace_id == workspace.id)).scalars()}


def optimize(db: Session, workspace: Workspace, url: str, *, keyword: str | None = None,
             country: str = "in", domain_id: str | None = None,
             competitor_urls: list[str] | None = None, use_ai: bool = True) -> ContentOptimization:
    keyword = " ".join((keyword or "").split())[:200] or None
    country = (country or "in").lower()[:8]
    html, final_url = _fetch_html(url)
    facts = content_score.extract(html, final_url)
    if facts["word_count"] < 30:
        raise ContentFetchError("The page has almost no readable text (it may be rendered by JavaScript).")

    own = _own_hosts(db, workspace) | {facts["host"]}
    targets, source, captured = _competitor_targets(db, workspace, keyword, country, own, competitor_urls or [])
    # Typed URLs that were left out (your own website) — shown so nothing vanishes silently.
    skipped = [u.strip() for u in (competitor_urls or [])
               if u.strip() and norm_host(u if re.match(r"^https?://", u.strip(), re.I) else "https://" + u.strip()) in own]
    with ThreadPoolExecutor(max_workers=MAX_COMPETITORS) as pool:
        fetched = list(pool.map(_fetch_competitor, targets)) if targets else []

    comp_facts = [c["facts"] for c in fetched if c.get("facts")]
    bench = content_score.benchmark(comp_facts, keyword or "")
    terms = content_score.term_coverage(facts, bench)
    checks, score = content_score.score(facts, keyword or "", bench, terms)

    competitors = []
    for c in fetched:
        f = c.get("facts")
        competitors.append({
            "position": c.get("position"), "url": c["url"], "host": c["host"],
            "title": (f or {}).get("title", ""),
            "word_count": f["word_count"] if f else None,
            "headings": len(f["headings"]) if f else None,
            "top_headings": [h["text"] for h in f["headings"] if h["level"] == 2][:8] if f else [],
            "reading_ease": f["reading_ease"] if f else None,
            "images": f["images"] if f else None,
            "keyword_in_title": (content_score.match(f["title"], keyword) == "full") if f and keyword else None,
            "keyword_in_h1": (content_score.match(" ".join(f["h1"]), keyword) == "full") if f and keyword else None,
            "error": c.get("error"),
        })

    summary = _rule_summary(score, checks, facts, bench, terms)
    summary_source = "rules"
    ai = None
    ai_error = None
    model_used = "rules"
    if use_ai:
        passed = sum(c["status"] == "pass" for c in checks)
        prompt = (f"SCORE: {score}/100 — {passed} of {len(checks)} checks pass.\n"
                  + _ai_prompt(facts, keyword, checks, bench, terms, fetched))
        try:
            data, model_used = openai_ai.write_json(AI_SYSTEM, prompt)
            ai = _clean_ai(data)
            s = str(data.get("summary") or "").strip()
            # Number guard: only numbers the AI was given (prompt + rule summary).
            if s and _numbers_ok(s, prompt + " " + summary):
                summary, summary_source = s[:800], "ai"
        except openai_ai.OpenAiError as exc:
            # The real reason (provider / key / quota) goes to the log, not the user.
            logging.getLogger(__name__).warning("AI writing failed: %s", exc)
            ai_error = "AI suggestions are unavailable right now. Please try again later."

    page = {k: facts[k] for k in (
        "title", "meta", "h1", "headings", "word_count", "paragraphs", "avg_paragraph_words", "lists",
        "images", "images_missing_alt", "internal_links", "external_links", "reading_ease", "has_schema")}
    result = {
        "version": SUMMARY_VERSION,
        "content_score": score,
        "summary": summary,
        "summary_source": summary_source,
        "keyword": keyword,
        "country": country,
        "page": page,
        "checks": checks,
        "competitor_source": source if competitors else "none",
        "serp_captured_at": captured,
        "competitors": competitors,
        "skipped_competitors": skipped[:5],
        "benchmark": {k: v for k, v in bench.items() if k != "terms"} if bench else None,
        "terms": terms,
        "ai": ai,
        "ai_error": ai_error,
    }

    did = _own_domain_id(db, workspace, domain_id)

    session = ContentOptimization(
        workspace_id=workspace.id,
        domain_id=did,
        url=final_url,
        title=facts["title"][:512],
        keyword=keyword,
        country_code=country,
        model_used=model_used,
        content_score=score,
        result=result,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    # Page history: timestamped snapshot of the page + what changed since last time.
    page_history_service.safe_record(
        db, workspace, final_url, extracted=facts, source="content", source_id=session.id,
        content_score_value=score, keyword=keyword)
    return session


# ── Sessions ────────────────────────────────────────────────────────────────

def _visible(workspace: Workspace):
    # Old placeholder runs (model_used='sample') are never shown.
    return (ContentOptimization.workspace_id == workspace.id) & (ContentOptimization.model_used != "sample")


def list_sessions(db: Session, workspace: Workspace, *, page: int, page_size: int,
                  q: str = "") -> tuple[list[ContentOptimization], int]:
    where = _visible(workspace)
    if q:
        like = f"%{q.lower()}%"
        where = where & (func.lower(ContentOptimization.url).like(like)
                         | func.lower(ContentOptimization.title).like(like)
                         | func.lower(func.coalesce(ContentOptimization.keyword, "")).like(like))
    total = db.execute(select(func.count()).select_from(ContentOptimization).where(where)).scalar_one()
    rows = db.execute(
        select(ContentOptimization).where(where)
        .order_by(ContentOptimization.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return list(rows), total


def get_session(db: Session, workspace: Workspace, session_id: str) -> ContentOptimization | None:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        return None
    return db.execute(
        select(ContentOptimization).where(ContentOptimization.id == sid, _visible(workspace))
    ).scalar_one_or_none()


def history(db: Session, workspace: Workspace, s: ContentOptimization, limit: int = 12) -> list[ContentOptimization]:
    """Runs for the same URL (+ keyword) up to this one, oldest → newest."""
    where = _visible(workspace) & (ContentOptimization.url == s.url) & (ContentOptimization.created_at <= s.created_at)
    if s.keyword:
        where = where & (ContentOptimization.keyword == s.keyword)
    rows = db.execute(
        select(ContentOptimization).where(where)
        .order_by(ContentOptimization.created_at.desc()).limit(limit)
    ).scalars().all()
    return list(reversed(rows))


# ── Keyword picker ──────────────────────────────────────────────────────────

def keyword_options(db: Session, workspace: Workspace, *, domain_id: str | None, url: str | None,
                    country: str = "in") -> list[dict]:
    """Keywords to optimize for: ones this URL already ranks for (GSC), the
    website's tracked keywords, then its highest-volume known keywords.
    Each says whether Google results are stored for it (free comparison)."""
    did = _own_domain_id(db, workspace, domain_id)
    if did is None:
        return []

    out: dict[str, dict] = {}
    if url:
        u = url.strip().rstrip("/")
        for r in db.execute(text("""
            SELECT keyword, search_volume, current_position FROM opportunity_keywords
            WHERE domain_id = :d AND rtrim(ranking_url, '/') = :u
            ORDER BY search_volume DESC NULLS LAST LIMIT 15
        """), {"d": str(did), "u": u}).all():
            out.setdefault(r.keyword.lower(), {"keyword": r.keyword, "source": "page",
                                               "search_volume": r.search_volume, "position": r.current_position})
    for r in db.execute(text("SELECT term FROM tracked_keywords WHERE domain_id = :d ORDER BY created_at DESC LIMIT 15"),
                        {"d": str(did)}).all():
        out.setdefault(r.term.lower(), {"keyword": r.term, "source": "tracked", "search_volume": None, "position": None})
    for r in db.execute(text("""
        SELECT keyword, search_volume, current_position FROM opportunity_keywords
        WHERE domain_id = :d ORDER BY search_volume DESC NULLS LAST LIMIT 10
    """), {"d": str(did)}).all():
        out.setdefault(r.keyword.lower(), {"keyword": r.keyword, "source": "site",
                                           "search_volume": r.search_volume, "position": r.current_position})

    rows = list(out.values())[:30]
    if rows:
        env = data_env.env_for_workspace(workspace)
        have = set(db.execute(text("""
            SELECT DISTINCT lower(k.keyword) FROM keywords k
            JOIN serp_snapshots s ON s.keyword_id = k.id
            WHERE lower(k.keyword) = ANY(:kws) AND s.environment = :env AND s.country_code = :c
        """), {"kws": [r["keyword"].lower() for r in rows], "env": env, "c": country}).scalars())
        for r in rows:
            r["has_serp"] = r["keyword"].lower() in have
    return rows
