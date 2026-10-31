"""Overall page SEO score: one weighted 0-100 built from the existing audit,
content and Core Web Vitals engines — no new fetching or scoring logic.

    run_page_score(url, keyword=...) -> PageScoreResult

The page is fetched once by ``audit_service.run_audit`` (which already folds in
real Core Web Vitals and the deep JSON-LD/evidence pass). Its HTML is reused by
``content_score`` so there is no second request. Each category's sub-score comes
straight from the source engines' pass/warn/fail checks, so the overall number
can be reconciled against the standalone tools — this module only groups the
checks into categories and applies product weights. Real Search Console numbers
are attached separately as context (see ``gsc_context``); they are never blended
into the score, because the score measures fixable page quality, not rankings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services import audit_service, content_score

# Product weights (sum to 100). These are a scoring opinion, not a Google formula.
# Content-led, per the on-page SEO methodology (content quality is where rankings
# are won). Core Web Vitals / PageSpeed is NOT a category here — it is context
# only (see cwv_context), so a fast but thin page can't score well on speed alone.
CATEGORY_WEIGHTS: dict[str, int] = {
    "content": 35,
    "onpage": 25,
    "technical": 20,
    "links_images": 12,
    "structured": 8,
}

CATEGORY_LABELS: dict[str, str] = {
    "content": "Content quality",
    "onpage": "On-page SEO",
    "technical": "Technical SEO",
    "links_images": "Links & images",
    "structured": "Structured data",
}

# Which category each audit check belongs to. Unknown ids fall back to on-page
# (most audit_details evidence checks are on-page), so new checks still score.
# Mobile viewport and HTML size are technical hygiene (no `performance` category
# now; `speed`/CWV is no longer a scored check at all).
_AUDIT_CATEGORY: dict[str, str] = {
    "status": "technical", "https": "technical", "canonical": "technical",
    "canonical_invalid": "technical", "duplicate_link": "technical",
    "lang": "technical", "indexable": "technical", "charset": "technical",
    "viewport": "technical", "size": "technical",
    "title": "onpage", "description": "onpage", "h1": "onpage", "og": "onpage",
    "duplicate_title": "onpage", "duplicate_meta": "onpage", "heading_order": "onpage",
    "img_alt": "links_images",
    "schema": "structured", "schema_invalid": "structured", "article_schema": "structured",
}

# Audit checks shown for context but NOT scored in a category (content depth is
# owned by the content-quality engine, so counting it twice would double-weight it).
_AUDIT_UNSCORED = {"content"}

_CREDIT = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


def _audit_category(check_id: str) -> str:
    if check_id in _AUDIT_CATEGORY:
        return _AUDIT_CATEGORY[check_id]
    if check_id.startswith("article_fields_"):
        return "structured"
    return "onpage"


@dataclass
class ScoreCheck:
    id: str
    label: str
    status: str          # pass | warn | fail
    detail: str
    weight: int = 1
    tip: str = ""


@dataclass
class ScoreCategory:
    id: str
    label: str
    weight: int          # product weight (percent)
    score: int | None    # 0-100, or None when nothing could be measured
    checks: list[ScoreCheck] = field(default_factory=list)


@dataclass
class PageScoreResult:
    url: str
    final_url: str
    status_code: int
    load_time_ms: int
    page_size_bytes: int
    keyword: str
    overall: int
    categories: list[ScoreCategory]
    # Reference numbers from the underlying engines (for reconciliation/debugging).
    audit_score: int
    content_score: int
    # Blocking problems that capped the overall score (empty when none).
    blockers: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    # Carried for page-history recording; not part of the API payload.
    html: str = field(default="", repr=False)
    facts: dict[str, Any] = field(default_factory=dict, repr=False)
    audit_checks: list[dict] = field(default_factory=list, repr=False)


def _weighted_score(checks: list[ScoreCheck]) -> int | None:
    total = sum(c.weight for c in checks)
    if total == 0:
        return None
    got = sum(c.weight * _CREDIT[c.status] for c in checks)
    return round(100 * got / total)


def _content_checks_for(group_prefixes: tuple[str, ...], content_checks: list[dict]) -> list[ScoreCheck]:
    """Content-engine checks whose id starts with any given prefix, as ScoreChecks."""
    out: list[ScoreCheck] = []
    for c in content_checks:
        if c["id"].startswith(group_prefixes):
            out.append(ScoreCheck(id=f"content_{c['id']}", label=c["label"], status=c["status"],
                                  detail=c.get("value", ""), weight=c.get("weight", 1), tip=c.get("tip", "")))
    return out


# A blocking problem ("fix these first" in the methodology) caps the overall
# score however clean the rest of the page is: a noindex, broken or title-less
# page can't rank, so it must not show a healthy number. These signals are read
# reliably from the raw HTML, so they stay hard caps.
_NOINDEX_CAP = 15
_BLOCKER_CAP = 40
# Thin content is a SOFT cap: it keeps a page out of the "Good" band but does not
# brand it "Poor". Content depth is measured from a JS-blind HTTP fetch, so on
# JS-rendered sites (Shopify collections/product grids, Next apps) the real body
# lives behind JavaScript and the raw-HTML word count under-counts — using it as
# a hard "Poor" cap mass-produces false Poor ratings. Thinness is already
# penalised proportionally in the content category (the heaviest weight), so the
# soft cap only prevents a thin page from reading as "Good".
_THIN_CAP = 79


def _classify(url: str) -> str:
    """Best-effort page type from the URL (deferred import avoids a cycle)."""
    try:
        from app.services.page_discovery_service import classify_page_type

        return classify_page_type(url)
    except Exception:  # noqa: BLE001 — classification is best-effort
        return "other"


def _blockers(audit: audit_service.AuditResult, facts: dict, page_type: str,
              word_count: int) -> tuple[list[str], int]:
    """Blocking issues found + the score ceiling they impose (100 = no cap).

    ``word_count`` is the readable main-content count (same as the editor), so a
    JS/card page with real content isn't falsely treated as thin."""
    reasons: list[str] = []
    cap = 100
    if any(c.id == "indexable" and c.status == "fail" for c in audit.checks):
        reasons.append("Set to noindex — search engines are told not to rank it.")
        cap = min(cap, _NOINDEX_CAP)
    if audit.status_code != 200:
        reasons.append(f"Returned HTTP {audit.status_code}, not 200.")
        cap = min(cap, _BLOCKER_CAP)
    if not (facts.get("title") or "").strip():
        reasons.append("No title tag.")
        cap = min(cap, _BLOCKER_CAP)
    if not facts.get("h1"):
        reasons.append("No H1 heading.")
        cap = min(cap, _BLOCKER_CAP)
    floor = content_score.depth_thresholds(page_type)[2]
    if word_count < floor:
        reasons.append(f"Severely thin content (~{word_count} words).")
        cap = min(cap, _THIN_CAP)
    return reasons, cap


def compose(audit: audit_service.AuditResult, *, keyword: str | None = None,
            page_type: str = "") -> PageScoreResult:
    """Combine an already-fetched audit + content analysis into one genuine
    0-100 on-page/technical SEO score. Pure (no network), so discovery can reuse
    it on HTML it already fetched. Core Web Vitals and GSC are never inputs —
    they are context only. Blocking problems cap the result."""
    facts = content_score.extract(audit.html, audit.final_url)  # title/meta/h1/technical
    kw = " ".join((keyword or "").split())
    ptype = (page_type or _classify(audit.final_url)).lower()
    # Score CONTENT on the SAME readable main-content the editor uses (trafilatura),
    # so the Discovery/Page score matches the editor and JS/card pages (Shopify,
    # Next) with real content aren't falsely scored as thin.
    readable = content_score.readable_html(audit.html)
    content_facts, content_checks, content_value = content_score.score_fields(
        facts["title"], facts["meta"], readable, audit.final_url, kw, page_type=ptype)

    # Normalise audit checks to a common shape and bucket them by category.
    audit_checks_flat = [
        {"id": c.id, "label": c.label, "status": c.status, "detail": c.detail, "weight": c.weight}
        for c in audit.checks
    ]
    buckets: dict[str, list[ScoreCheck]] = {cid: [] for cid in CATEGORY_WEIGHTS}
    for c in audit.checks:
        if c.id in _AUDIT_UNSCORED:
            continue
        buckets[_audit_category(c.id)].append(
            ScoreCheck(id=c.id, label=c.label, status=c.status, detail=c.detail, weight=c.weight)
        )

    # Links & images also account for the content engine's link checks.
    buckets["links_images"].extend(_content_checks_for(("internal_links", "external_links"), content_checks))

    categories: list[ScoreCategory] = []
    for cid, weight in CATEGORY_WEIGHTS.items():
        if cid == "content":
            # Content quality is the content engine's own deterministic 0-100;
            # its checklist is shown as evidence (not re-weighted here).
            checks = [ScoreCheck(id=f"content_{c['id']}", label=c["label"], status=c["status"],
                                 detail=c.get("value", ""), weight=c.get("weight", 1), tip=c.get("tip", ""))
                      for c in content_checks]
            categories.append(ScoreCategory(cid, CATEGORY_LABELS[cid], weight,
                                            content_value if content_checks else None, checks))
        else:
            checks = buckets[cid]
            categories.append(ScoreCategory(cid, CATEGORY_LABELS[cid], weight, _weighted_score(checks), checks))

    # Overall = weighted mean of the categories that could be measured
    # (re-normalised so a missing category never drags the score down)...
    scored = [c for c in categories if c.score is not None]
    wsum = sum(c.weight for c in scored)
    overall = round(sum(c.weight * c.score for c in scored) / wsum) if wsum else 0
    # ...then capped by any blocking problem (thin judged on the readable body).
    blockers, cap = _blockers(audit, facts, ptype, content_facts.get("word_count", 0))
    overall = min(overall, cap)

    return PageScoreResult(
        url=audit.final_url,
        final_url=audit.final_url,
        status_code=audit.status_code,
        load_time_ms=audit.load_time_ms,
        page_size_bytes=audit.page_size_bytes,
        keyword=kw,
        overall=overall,
        categories=categories,
        audit_score=audit.score,
        content_score=content_value,
        blockers=blockers,
        metrics=audit.metrics,
        html=audit.html,
        facts=facts,
        audit_checks=audit_checks_flat,
    )


def run_page_score(raw_url: str, *, keyword: str | None = None, device: str = "desktop",
                   with_psi: bool = True, page_type: str = "") -> PageScoreResult:
    """Fetch once and combine into one genuine weighted 0-100.

    Raises the same ``audit_service`` errors (InvalidUrlError / BlockedUrlError /
    FetchError) as the audit tool, since it is the single fetch path. ``with_psi``
    only populates the Core Web Vitals CONTEXT panel — it never changes the score.
    """
    audit = audit_service.run_audit(raw_url, device, with_psi=with_psi)
    return compose(audit, keyword=keyword, page_type=page_type)


def gsc_context(db, workspace, domain, page_url: str, days: int = 28) -> dict | None:
    """Best-effort real Search Console numbers for the page, as CONTEXT only.

    Returns None when GSC isn't connected, the page has no verified property, or
    anything fails — the score stands on its own. A local import avoids coupling
    the pure scorer to the GSC/DB layer.
    """
    if domain is None:
        return None
    try:
        from app.services import gsc_service

        perf = gsc_service.page_performance(db, workspace, domain, page_url, days=days)
    except Exception:  # noqa: BLE001 — not connected / no property / network / expired token
        return None
    if not perf.get("has_data"):
        return None
    return perf


def cwv_context(metrics: dict | None) -> dict | None:
    """Core Web Vitals / page-speed verdict for display as CONTEXT only — it is
    never part of the score. Returns None when there's nothing to show."""
    status = (metrics or {}).get("speed_status")
    if not status:
        return None
    return {
        "status": status,
        "detail": metrics.get("speed_detail", ""),
        "source": metrics.get("speed_source"),
        "core_web_vitals": metrics.get("core_web_vitals"),
    }
