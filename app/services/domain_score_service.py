"""Whole-site (domain) SEO score.

Aggregates the LATEST per-page score for a domain's indexable pages — produced
by Page Discovery, which crawls each page (through the residential proxy) and
stores its full score as a PageSnapshot — and blends them with site-wide
technical signals (robots/sitemap/HTTPS/indexable coverage) into one 0-100. This
service does NOT fetch pages itself: it reads whatever Discovery last crawled, so
"Re-score" is an instant recompute of the latest crawl. Fresh page data comes
from running Page Discovery, not from here. The combining logic (aggregate /
site_technical_score) is pure and unit-tested.

    domain_overall = round(PAGE_W * page_quality_adjusted + SITE_W * site_technical)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.domain_score import DomainScore, DomainScoreJob
from app.models.site_page import PageSnapshot, SitePage
from app.models.workspace import Workspace
from app.services import crawl_service, page_discovery_service
from app.services.page_score_service import CATEGORY_WEIGHTS

# Tunables (settings-backed; see app/core/config.py).
DOMAIN_SCORE_MAX_PAGES = settings.DOMAIN_SCORE_MAX_PAGES  # safety ceiling per run
REFRESH_DAYS = settings.DOMAIN_SCORE_REFRESH_DAYS         # weekly refetch gate (0 = run anytime)
PAGE_W = 0.70                   # weight of aggregated page quality in the domain score
SITE_W = 0.30                   # weight of site-wide technical signals
WEAK_THRESHOLD = 50             # a page scoring below this is "weak"
WEAK_PENALTY = 0.30             # max downward pull when every page is weak

# Aggregation is fast (no fetching), but a run is still a background job; a
# 'running' row is considered dead if its heartbeat hasn't moved in this long.
HEARTBEAT_STALE = timedelta(minutes=20)

# Page importance when averaging (evergreen, high-value pages count for more).
TYPE_WEIGHT = {"home": 3.0, "content": 2.0, "blog": 2.0, "category": 1.5, "tag": 1.0, "other": 1.0}

# Weights for the site-wide technical checks (ids match crawl_service + our own).
_SITE_CHECK_WEIGHT = {
    "robots_found": 1, "not_blocked": 3, "sitemap_found": 2, "sitemap_urls": 1,
    "https": 3, "coverage": 2,
}
_CREDIT = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


def _ptype(p: SitePage) -> str:
    return p.page_type or "other"


# ── Page selection ───────────────────────────────────────────────────────────

def select_pages(db: Session, domain: Domain, cap: int = DOMAIN_SCORE_MAX_PAGES) -> list[SitePage]:
    """Every indexable, fetchable discovered page, home page first and then the
    rest spread across page types (so partial progress stays representative and
    one big section is scored first-in-class). Bounded by ``cap`` as a safety
    ceiling. Only crawlable, non-noindex, 200 pages are eligible."""
    rows = db.execute(
        select(SitePage).where(
            SitePage.domain_id == domain.id,
            SitePage.discovery_source.isnot(None),
            SitePage.indexable.isnot(False),  # True or unknown
            or_(SitePage.http_status.is_(None), SitePage.http_status == 200),
        )
    ).scalars().all()
    if not rows:
        return []

    def is_home(p: SitePage) -> bool:
        return _ptype(p) == "home" or urlparse(p.url).path in ("", "/")

    home = [p for p in rows if is_home(p)]
    rest = [p for p in rows if not is_home(p)]

    # Group the rest by type and round-robin across groups (heaviest types first).
    groups: dict[str, list[SitePage]] = {}
    for p in rest:
        groups.setdefault(_ptype(p), []).append(p)
    for g in groups.values():
        g.sort(key=lambda p: p.url)
    order = sorted(groups, key=lambda t: TYPE_WEIGHT.get(t, 1.0), reverse=True)

    selected: list[SitePage] = home[:1]
    while len(selected) < cap and any(groups[t] for t in order):
        for t in order:
            if groups[t] and len(selected) < cap:
                selected.append(groups[t].pop(0))
    return selected[:cap]


# ── Aggregation (pure) ──────────────────────────────────────────────────────

def aggregate(page_results: list[dict], *, weak_threshold: int = WEAK_THRESHOLD) -> dict:
    """Combine per-page results into page quality + category averages.

    ``page_results``: [{"score": int, "page_type": str, "categories": {cat: score|None}}].
    Returns {page_quality, weak_share, category_averages, scored}.
    """
    scored = [r for r in page_results if r.get("score") is not None]
    if not scored:
        return {"page_quality": 0, "weak_share": 0.0, "category_averages": {}, "scored": 0}

    wsum = sum(TYPE_WEIGHT.get(r["page_type"], 1.0) for r in scored)
    weighted_mean = sum(TYPE_WEIGHT.get(r["page_type"], 1.0) * r["score"] for r in scored) / wsum
    weak_share = sum(1 for r in scored if r["score"] < weak_threshold) / len(scored)
    page_quality = round(weighted_mean * (1 - WEAK_PENALTY * weak_share))

    category_averages: dict[str, int] = {}
    for cat in CATEGORY_WEIGHTS:
        vals = [r["categories"][cat] for r in scored if r.get("categories", {}).get(cat) is not None]
        if vals:
            category_averages[cat] = round(sum(vals) / len(vals))
    return {"page_quality": page_quality, "weak_share": round(weak_share, 3),
            "category_averages": category_averages, "scored": len(scored)}


def site_technical_score(crawl_checks: list, summary: dict, https_ok: bool) -> tuple[int, list[dict]]:
    """Weighted 0-100 for site-wide technical health + the checks for display.

    ``crawl_checks``: crawl_service.Check list (robots/sitemap). ``summary``:
    page_discovery_service.summary(). ``https_ok``: every scored page was HTTPS.
    """
    checks: list[dict] = [
        {"id": c.id, "label": c.label, "status": c.status, "detail": c.detail}
        for c in crawl_checks if c.id in _SITE_CHECK_WEIGHT
    ]

    checks.append({"id": "https", "label": "HTTPS", "status": "pass" if https_ok else "fail",
                   "detail": "All scored pages are served over HTTPS." if https_ok
                   else "Some pages are not served over HTTPS."})

    total = summary.get("total") or 0
    indexable = summary.get("indexable") or 0
    if total == 0:
        checks.append({"id": "coverage", "label": "Indexable coverage", "status": "fail",
                       "detail": "No pages discovered for this site yet."})
    else:
        ratio = indexable / total
        status = "pass" if ratio >= 0.8 else "warn" if ratio >= 0.5 else "fail"
        checks.append({"id": "coverage", "label": "Indexable coverage", "status": status,
                       "detail": f"{indexable} of {total} discovered pages are indexable ({round(ratio * 100)}%)."})

    wsum = sum(_SITE_CHECK_WEIGHT[c["id"]] for c in checks)
    got = sum(_SITE_CHECK_WEIGHT[c["id"]] * _CREDIT[c["status"]] for c in checks)
    return (round(100 * got / wsum) if wsum else 0), checks


# ── Reading one page's latest score (no network) ─────────────────────────────

def _latest_score(db: Session, page: SitePage) -> dict | None:
    """The page's latest stored score from Page Discovery (or any scorer), with
    its category breakdown. Prefers the newest PageSnapshot that carries an
    overall score (any age — we aggregate the latest crawl, never re-fetch); if a
    page was crawled before snapshots carried the breakdown, falls back to the
    plain overall number on the page row. None when the page was never scored."""
    snap = db.execute(
        select(PageSnapshot).where(
            PageSnapshot.page_id == page.id,
            PageSnapshot.overall_score.isnot(None),
        ).order_by(PageSnapshot.captured_at.desc()).limit(1)
    ).scalar_one_or_none()
    if snap is not None:
        return {"url": page.url, "page_id": str(page.id), "score": snap.overall_score,
                "page_type": _ptype(page), "categories": snap.category_scores or {}}
    if page.latest_audit_score is not None:
        return {"url": page.url, "page_id": str(page.id), "score": page.latest_audit_score,
                "page_type": _ptype(page), "categories": {}}
    return None


# ── Background worker ───────────────────────────────────────────────────────

def _finish_error(db: Session, job: DomainScoreJob, message: str) -> None:
    job.status = "error"
    job.error = message[:300]
    job.finished_at = datetime.now(timezone.utc)
    db.commit()


def run_domain_score(workspace_id, domain_id, job_id, *, session_factory=None) -> None:
    """Aggregate a whole site into one DomainScore from the latest page scores
    Page Discovery already crawled — NO page fetching here. Own DB session; never
    raises (background worker). Fast: it only reads stored snapshots and the
    site-wide signals, so a re-run is an instant recompute of the latest crawl."""
    db = (session_factory or SessionLocal)()
    job = None
    try:
        job = db.get(DomainScoreJob, job_id)
        workspace = db.get(Workspace, workspace_id)
        domain = db.get(Domain, domain_id)
        if job is None or workspace is None or domain is None or domain.workspace_id != workspace.id:
            return

        job.step, job.step_label = 1, "Finding pages"
        job.last_progress_at = datetime.now(timezone.utc)
        db.commit()
        pages = select_pages(db, domain)
        if not pages:
            _finish_error(db, job, "No pages found for this site yet. Run Page Discovery first.")
            return
        total = len(pages)
        job.pages_total = total
        db.commit()

        # Read each indexable page's latest stored score (no network). Pages
        # Discovery hasn't scored yet are counted as unscored (pages_failed) so
        # the UI can prompt a Discovery run; they don't drag the average down.
        results: list[dict] = []
        unscored = 0
        for i, page in enumerate(pages, 1):
            r = _latest_score(db, page)
            if r is not None:
                results.append(r)
            else:
                unscored += 1
            if i % 200 == 0 or i == total:
                job.step, job.step_label = 2, f"Reading page scores ({i}/{total})"
                job.pages_scored = len(results)
                job.pages_failed = unscored
                job.last_progress_at = datetime.now(timezone.utc)
                db.commit()

        job.step, job.step_label = 3, "Checking site-wide signals"
        job.last_progress_at = datetime.now(timezone.utc)
        db.commit()
        origin = "https://" + domain.domain.strip().lower().removeprefix("www.").rstrip("/")
        try:
            crawl = crawl_service.run_check(origin)
            crawl_checks = crawl.checks
        except Exception:  # noqa: BLE001 — robots/sitemap unreachable shouldn't abort the score
            crawl_checks = []
        summary = page_discovery_service.summary(db, domain)
        https_ok = bool(results) and all(r["url"].lower().startswith("https://") for r in results)
        site_technical, site_checks = site_technical_score(crawl_checks, summary, https_ok)

        agg = aggregate(results)
        overall = round(PAGE_W * agg["page_quality"] + SITE_W * site_technical)

        score = DomainScore(
            workspace_id=workspace.id, domain_id=domain.id, overall=overall,
            page_quality=agg["page_quality"], site_technical=site_technical,
            category_averages=agg["category_averages"], site_checks=site_checks,
            pages=sorted(({"url": r["url"], "page_id": r["page_id"], "score": r["score"],
                           "page_type": r["page_type"]} for r in results), key=lambda r: r["score"]),
            pages_total=summary.get("total") or 0, pages_scored=agg["scored"],
            pages_indexable=summary.get("indexable") or 0, weak_share=agg["weak_share"],
        )
        db.add(score)
        db.flush()
        job.result_id = score.id
        job.status, job.step, job.step_label = "done", 3, "Done"
        job.last_progress_at = job.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if job is not None:
            try:
                _finish_error(db, job, f"Scoring failed: {exc}")
            except Exception:  # noqa: BLE001
                db.rollback()
    finally:
        db.close()


# ── Reading ─────────────────────────────────────────────────────────────────

def latest_job(db: Session, domain: Domain) -> DomainScoreJob | None:
    return db.execute(
        select(DomainScoreJob).where(DomainScoreJob.domain_id == domain.id)
        .order_by(DomainScoreJob.started_at.desc()).limit(1)
    ).scalar_one_or_none()


def running_job(db: Session, domain: Domain) -> DomainScoreJob | None:
    """An in-flight job: status 'running' AND its heartbeat is recent. A crashed
    worker leaves a 'running' row with a stale heartbeat — treated as dead."""
    job = latest_job(db, domain)
    if job is None or job.status != "running":
        return None
    beat = job.last_progress_at or job.started_at
    if beat is None:
        return None
    beat = beat if beat.tzinfo else beat.replace(tzinfo=timezone.utc)
    return job if datetime.now(timezone.utc) - beat < HEARTBEAT_STALE else None


def supersede_stale(db: Session, domain: Domain) -> None:
    """Mark any 'running' job as failed before starting a new one."""
    db.execute(update(DomainScoreJob).where(
        DomainScoreJob.domain_id == domain.id, DomainScoreJob.status == "running"
    ).values(status="error", error="superseded", finished_at=datetime.now(timezone.utc)))
    db.commit()


def latest_score(db: Session, domain: Domain) -> DomainScore | None:
    return db.execute(
        select(DomainScore).where(DomainScore.domain_id == domain.id)
        .order_by(DomainScore.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def last_crawled_at(db: Session, domain: Domain) -> datetime | None:
    """When Page Discovery last fetched any page of this site — the real
    freshness of the data the score aggregates (distinct from when the score was
    last recomputed)."""
    return db.execute(
        select(func.max(SitePage.last_checked_at)).where(
            SitePage.domain_id == domain.id, SitePage.discovery_source.isnot(None))
    ).scalar_one_or_none()


def next_refetch_at(score: DomainScore | None) -> datetime | None:
    """When a fresh SEO Score may next be run (last run + refresh window)."""
    if score is None or score.created_at is None:
        return None
    created = score.created_at if score.created_at.tzinfo else score.created_at.replace(tzinfo=timezone.utc)
    return created + timedelta(days=REFRESH_DAYS)


def can_refetch(score: DomainScore | None) -> bool:
    """True when there's no score yet, or the last one is older than the window."""
    nxt = next_refetch_at(score)
    return nxt is None or datetime.now(timezone.utc) >= nxt


def history(db: Session, domain: Domain, limit: int = 30) -> list[DomainScore]:
    return list(db.execute(
        select(DomainScore).where(DomainScore.domain_id == domain.id)
        .order_by(DomainScore.created_at.desc()).limit(limit)
    ).scalars())
