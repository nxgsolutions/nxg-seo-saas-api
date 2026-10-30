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
CATEGORY_WEIGHTS: dict[str, int] = {
    "technical": 25,
    "onpage": 25,
    "content": 20,
    "performance": 15,
    "links_images": 10,
    "structured": 5,
}

CATEGORY_LABELS: dict[str, str] = {
    "technical": "Technical SEO",
    "onpage": "On-page SEO",
    "content": "Content quality",
    "performance": "Performance & mobile",
    "links_images": "Links & images",
    "structured": "Structured data",
}

# Which category each audit check belongs to. Unknown ids fall back to on-page
# (most audit_details evidence checks are on-page), so new checks still score.
_AUDIT_CATEGORY: dict[str, str] = {
    "status": "technical", "https": "technical", "canonical": "technical",
    "canonical_invalid": "technical", "duplicate_link": "technical",
    "lang": "technical", "indexable": "technical", "charset": "technical",
    "title": "onpage", "description": "onpage", "h1": "onpage", "og": "onpage",
    "duplicate_title": "onpage", "duplicate_meta": "onpage", "heading_order": "onpage",
    "speed": "performance", "viewport": "performance", "size": "performance",
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


def run_page_score(raw_url: str, *, keyword: str | None = None, device: str = "desktop",
                   with_psi: bool = True) -> PageScoreResult:
    """Fetch once, run every engine, and combine into one weighted 0-100.

    Raises the same ``audit_service`` errors (InvalidUrlError / BlockedUrlError /
    FetchError) as the audit tool, since it is the single fetch path.
    """
    audit = audit_service.run_audit(raw_url, device, with_psi=with_psi)

    # Reuse the already-fetched HTML — no second request.
    facts = content_score.extract(audit.html, audit.final_url)
    kw = " ".join((keyword or "").split())
    content_checks, content_value = content_score.score(facts, kw)

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
    # (re-normalised so a missing category never drags the score down).
    scored = [c for c in categories if c.score is not None]
    wsum = sum(c.weight for c in scored)
    overall = round(sum(c.weight * c.score for c in scored) / wsum) if wsum else 0

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
        metrics=audit.metrics,
        html=audit.html,
        facts=facts,
        audit_checks=audit_checks_flat,
    )


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
