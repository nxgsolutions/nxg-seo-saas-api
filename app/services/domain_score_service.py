"""Whole-site (domain) SEO score.

Aggregates a capped, representative sample of a domain's per-page overall scores
(reused from page_snapshots when fresh, otherwise freshly scored) and blends
them with site-wide technical signals (robots/sitemap/HTTPS/indexable coverage)
into one 0-100. The heavy work runs in a background job (run_domain_score); the
combining logic (aggregate / site_technical_score) is pure and unit-tested.

    domain_overall = round(PAGE_W * page_quality_adjusted + SITE_W * site_technical)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.domain_score import DomainScore, DomainScoreJob
from app.models.site_page import PageSnapshot, SitePage
from app.models.workspace import Workspace
from app.services import crawl_service, page_discovery_service, page_history_service, page_score_service
from app.services.audit_service import BlockedUrlError, FetchError, InvalidUrlError
from app.services.page_score_service import CATEGORY_WEIGHTS

# Tunables.
DOMAIN_SCORE_MAX_PAGES = 25     # sample cap per run
FRESH_DAYS = 14                 # reuse a page's stored score if newer than this
PAGE_W = 0.70                   # weight of aggregated page quality in the domain score
SITE_W = 0.30                   # weight of site-wide technical signals
WEAK_THRESHOLD = 50             # a page scoring below this is "weak"
WEAK_PENALTY = 0.30             # max downward pull when every page is weak

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


# ── Sampling ──────────────────────────────────────────────────────────────

def select_sample(db: Session, domain: Domain, cap: int = DOMAIN_SCORE_MAX_PAGES) -> list[SitePage]:
    """A representative sample of the domain's discovered pages: the home page,
    then the rest spread across page types (so one big section can't dominate),
    capped at ``cap``. Only crawlable, non-noindex pages are eligible."""
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

    sample: list[SitePage] = home[:1]
    while len(sample) < cap and any(groups[t] for t in order):
        for t in order:
            if groups[t] and len(sample) < cap:
                sample.append(groups[t].pop(0))
    return sample[:cap]


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
    page_discovery_service.summary(). ``https_ok``: every sampled page was HTTPS.
    """
    checks: list[dict] = [
        {"id": c.id, "label": c.label, "status": c.status, "detail": c.detail}
        for c in crawl_checks if c.id in _SITE_CHECK_WEIGHT
    ]

    checks.append({"id": "https", "label": "HTTPS", "status": "pass" if https_ok else "fail",
                   "detail": "All sampled pages are served over HTTPS." if https_ok
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


# ── Scoring one page (reuse-or-run) ─────────────────────────────────────────

def _score_one(db: Session, workspace: Workspace, page: SitePage, fresh_before: datetime) -> dict | None:
    """One page's result, reusing a recent stored score or computing a new one.
    Returns None when the page can't be fetched/scored."""
    snap = db.execute(
        select(PageSnapshot).where(
            PageSnapshot.page_id == page.id,
            PageSnapshot.overall_score.isnot(None),
            PageSnapshot.captured_at >= fresh_before,
        ).order_by(PageSnapshot.captured_at.desc()).limit(1)
    ).scalar_one_or_none()
    if snap is not None:
        return {"url": page.url, "page_id": str(page.id), "score": snap.overall_score,
                "page_type": _ptype(page), "categories": snap.category_scores or {}, "fetched": False}

    try:
        result = page_score_service.run_page_score(page.url)
    except (InvalidUrlError, BlockedUrlError, FetchError):
        return None

    checks = [{"id": c["id"], "label": c["label"], "status": c["status"]} for c in result.audit_checks]
    cats = {c.id: c.score for c in result.categories}
    try:
        page_history_service.record(
            db, workspace, result.final_url, html=result.html, source="score",
            audit_score=result.audit_score, content_score_value=result.content_score,
            overall_score=result.overall, category_scores=cats, checks=checks,
            tech=page_history_service.audit_tech(result.status_code, result.load_time_ms,
                                                 result.page_size_bytes, result.metrics, checks),
        )
    except Exception:  # noqa: BLE001 — history is a side effect, never fail the score
        db.rollback()
    return {"url": result.final_url, "page_id": str(page.id), "score": result.overall,
            "page_type": _ptype(page), "categories": cats, "fetched": True}


# ── Background worker ───────────────────────────────────────────────────────

def _finish_error(db: Session, job: DomainScoreJob, message: str) -> None:
    job.status = "error"
    job.error = message[:300]
    job.finished_at = datetime.now(timezone.utc)
    db.commit()


def run_domain_score(workspace_id, domain_id, job_id, *, session_factory=None) -> None:
    """Score a whole site into one DomainScore. Own DB session; never raises
    (background worker — the request has already returned). Updates the job's
    progress as it goes so the UI can poll."""
    db = (session_factory or SessionLocal)()
    job = None
    try:
        job = db.get(DomainScoreJob, job_id)
        workspace = db.get(Workspace, workspace_id)
        domain = db.get(Domain, domain_id)
        if job is None or workspace is None or domain is None or domain.workspace_id != workspace.id:
            return

        job.step, job.step_label = 1, "Finding pages"
        db.commit()
        sample = select_sample(db, domain)
        if not sample:
            _finish_error(db, job, "No pages found for this site yet. Run Page Discovery first.")
            return
        job.pages_total = len(sample)
        db.commit()

        fresh_before = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)
        results: list[dict] = []
        for i, page in enumerate(sample, 1):
            job.step, job.step_label = 2, f"Scoring pages ({i}/{len(sample)})"
            r = _score_one(db, workspace, page, fresh_before)
            if r is not None:
                results.append(r)
            job.pages_scored = len(results)
            db.commit()

        job.step, job.step_label = 3, "Checking site-wide signals"
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
        job.finished_at = datetime.now(timezone.utc)
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
    """An in-flight job (started < 15 min ago); older 'running' rows are stale."""
    job = latest_job(db, domain)
    if job is None or job.status != "running" or job.started_at is None:
        return None
    started = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=timezone.utc)
    return job if datetime.now(timezone.utc) - started < timedelta(minutes=15) else None


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


def history(db: Session, domain: Domain, limit: int = 30) -> list[DomainScore]:
    return list(db.execute(
        select(DomainScore).where(DomainScore.domain_id == domain.id)
        .order_by(DomainScore.created_at.desc()).limit(limit)
    ).scalars())
