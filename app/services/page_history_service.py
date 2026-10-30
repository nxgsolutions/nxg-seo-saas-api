"""Page history — store every fetch of a page as a timestamped snapshot and
explain what changed, then line changes up with Search Console numbers.

    record(db, workspace, url, html=..., source="audit", ...)   # after any fetch
    timeline(db, workspace, page)                                # snapshots + changes
    performance(db, workspace, page, days)                       # daily GSC + before/after per change

Snapshots are compared with the previous snapshot of the same page. On-page
facts always come from one extractor (content_score.extract) so different
tools never produce fake differences; technical facts and checks are only
compared between audits. Search Console rows are stored per day
(page_search_daily) so the history survives GSC's 16-month window.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.models.site_page import PageSearchDaily, PageSnapshot, SitePage
from app.models.workspace import Workspace
from app.services import content_score

MAX_SNAPSHOTS_PER_PAGE = 300
FACTS_VERSION = 2  # 2: links counted inside paragraphs only
BODY_TEXT_CHARS = 40_000
WINDOW_DAYS = 28          # before / after window around a change
MIN_AFTER_DAYS = 7        # fewer completed days after a change = "too early to tell"


# ── URL identity ────────────────────────────────────────────────────────────

def url_key(url: str) -> str:
    """Host without www + path without trailing slash (+ query); scheme and fragment ignored."""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return host + path + (f"?{p.query}" if p.query else "")


def _domain_for(db: Session, workspace: Workspace, url: str) -> uuid.UUID | None:
    host = url_key(url).split("/", 1)[0]
    for d in db.execute(select(Domain).where(Domain.workspace_id == workspace.id)).scalars():
        dh = d.domain.lower().removeprefix("www.")
        if host == dh or host.endswith("." + dh):
            return d.id
    return None


# ── Facts ───────────────────────────────────────────────────────────────────

def page_facts(extracted: dict) -> dict:
    """The comparable on-page facts kept in every snapshot."""
    return {
        # Bump when how a fact is measured changes; such facts are then not
        # compared with older snapshots (no false "changes").
        "v": FACTS_VERSION,
        "title": extracted.get("title", ""),
        "meta": extracted.get("meta", ""),
        "h1": extracted.get("h1", [])[:3],
        "headings": [{"level": h["level"], "text": h["text"]} for h in extracted.get("headings", [])][:40],
        "word_count": extracted.get("word_count"),
        "first_paragraph": (extracted.get("first_paragraph") or "")[:400],
        "images": extracted.get("images"),
        "images_missing_alt": extracted.get("images_missing_alt"),
        "internal_links": extracted.get("internal_links"),
        "external_links": extracted.get("external_links"),
        "has_schema": extracted.get("has_schema"),
        "reading_ease": extracted.get("reading_ease"),
    }


def audit_tech(status_code: int, load_ms: int, size: int, metrics: dict, checks: list[dict]) -> dict:
    """Technical facts from an audit (compared only between audits)."""
    indexable = next((c["status"] != "fail" for c in checks if c.get("id") == "indexable"), None)

    def m(key: str, cast=None):
        # None = this (older) audit didn't record it → never reported as a change.
        if key not in metrics:
            return None
        v = metrics[key]
        return cast(v) if cast else (v if v is not None else "")

    return {
        "status_code": status_code,
        "load_ms": load_ms,
        "page_size": size,
        "canonical": m("canonical_url"),
        "indexable": indexable,
        "lang": m("lang"),
        "viewport": m("has_viewport", bool),
        "https": m("is_https", bool),
        "og_title": m("og_title"),
        "og_image": m("og_image", bool),
        "structured_data": m("has_structured_data", bool),
    }


# ── Diff (pure) ─────────────────────────────────────────────────────────────

def _chg(kind: str, field: str, label: str, before, after) -> dict:
    return {"kind": kind, "field": field, "label": label, "before": before, "after": after}


def _text_similarity(a: str, b: str) -> float:
    wa, wb = a.split()[:4000], b.split()[:4000]
    if not wa and not wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def diff(prev: PageSnapshot | dict | None, cur: dict) -> list[dict]:
    """Changes from the previous snapshot to the current one.

    `prev` / `cur` carry: facts, tech (or None), checks (or None), body_text, content_hash.
    """
    if prev is None:
        return []
    get = (lambda k: getattr(prev, k)) if not isinstance(prev, dict) else prev.get
    pf, cf = get("facts") or {}, cur.get("facts") or {}
    out: list[dict] = []

    for field, label in (("title", "Title"), ("meta", "Meta description")):
        a, b = (pf.get(field) or "").strip(), (cf.get(field) or "").strip()
        if field in pf and a != b:
            out.append(_chg("meta", field, label, a or None, b or None))
    a, b = " | ".join(pf.get("h1") or []), " | ".join(cf.get("h1") or [])
    if "h1" in pf and a != b:
        out.append(_chg("meta", "h1", "H1 heading", a or None, b or None))

    if "headings" in pf:
        ph = [h["text"] for h in pf.get("headings") or []]
        chh = [h["text"] for h in cf.get("headings") or []]
        added = [h for h in chh if h not in ph]
        removed = [h for h in ph if h not in chh]
        if added:
            out.append(_chg("content", "headings_added", f"{len(added)} subheading(s) added", None, added[:8]))
        if removed:
            out.append(_chg("content", "headings_removed", f"{len(removed)} subheading(s) removed", removed[:8], None))

    pw, cw = pf.get("word_count"), cf.get("word_count")
    if pw is not None and cw is not None and abs(cw - pw) >= max(30, pw * 0.05):
        out.append(_chg("content", "word_count", "Content length", pw, cw))

    p_hash, p_body = get("content_hash"), get("body_text")
    if p_hash and cur.get("content_hash") and p_hash != cur["content_hash"] and p_body and cur.get("body_text"):
        sim = _text_similarity(p_body, cur["body_text"])
        if sim < 0.98:
            out.append(_chg("content", "body_text", "Page text edited", None, round((1 - sim) * 100)))

    same_method = pf.get("v") == cf.get("v")
    for field, label, threshold in (("images", "Images", 1), ("images_missing_alt", "Images without alt text", 1),
                                    ("internal_links", "Internal links", 2), ("external_links", "Outbound links", 2)):
        if "links" in field and not same_method:
            continue
        a, b = pf.get(field), cf.get(field)
        if a is not None and b is not None and abs(b - a) >= threshold:
            out.append(_chg("links" if "links" in field else "media", field, label, a, b))
    if pf.get("has_schema") is not None and cf.get("has_schema") is not None and pf["has_schema"] != cf["has_schema"]:
        out.append(_chg("technical", "has_schema", "Structured data", pf["has_schema"], cf["has_schema"]))

    pt, ct = get("tech"), cur.get("tech")
    if pt and ct:
        for field, label in (("status_code", "HTTP status"), ("canonical", "Canonical URL"), ("indexable", "Indexable"),
                             ("lang", "Language"), ("viewport", "Mobile viewport"), ("https", "HTTPS"),
                             ("og_title", "Social title (og:title)"), ("og_image", "Social image (og:image)")):
            if pt.get(field) is not None and ct.get(field) is not None and pt[field] != ct[field]:
                out.append(_chg("technical", field, label, pt.get(field), ct.get(field)))

    pc, cc = get("checks"), cur.get("checks")
    if pc and cc:
        # Response time / HTML size vary run to run — not treated as page changes.
        noisy = {"speed", "size"}
        before = {c["id"]: c for c in pc if c.get("id") not in noisy}
        rank = {"fail": 0, "warn": 1, "pass": 2}
        fixed = [c["label"] for c in cc if c["id"] in before and rank[c["status"]] > rank[before[c["id"]]["status"]]]
        worse = [c["label"] for c in cc if c["id"] in before and rank[c["status"]] < rank[before[c["id"]]["status"]]]
        if fixed:
            out.append(_chg("checks", "fixed", f"{len(fixed)} issue(s) fixed", None, fixed))
        if worse:
            out.append(_chg("checks", "regressed", f"{len(worse)} new issue(s)", None, worse))
    return out


# ── Recording ───────────────────────────────────────────────────────────────

def _text_hash(body: str) -> str:
    return hashlib.sha256(content_score.norm(body).encode()).hexdigest()


def get_or_create_page(db: Session, workspace: Workspace, url: str, *, title: str = "") -> SitePage:
    key = url_key(url)
    page = db.execute(select(SitePage).where(SitePage.workspace_id == workspace.id, SitePage.url_key == key)
                      ).scalar_one_or_none()
    if page is None:
        page = SitePage(workspace_id=workspace.id, url_key=key, url=url, title=title[:512],
                        domain_id=_domain_for(db, workspace, url), snapshot_count=0)
        db.add(page)
        db.flush()
    return page


def record(db: Session, workspace: Workspace, url: str, *, html: str | None = None, extracted: dict | None = None,
           source: str, source_id: uuid.UUID | None = None, device: str | None = None,
           audit_score: int | None = None, content_score_value: int | None = None, keyword: str | None = None,
           overall_score: int | None = None, category_scores: dict | None = None,
           tech: dict | None = None, checks: list[dict] | None = None,
           captured_at: datetime | None = None, commit: bool = True) -> PageSnapshot:
    """Store a snapshot of the page (call after any successful fetch)."""
    if extracted is None:
        extracted = content_score.extract(html or "", url)
    facts = page_facts(extracted)
    body = (extracted.get("body") or "")[:BODY_TEXT_CHARS]
    page = get_or_create_page(db, workspace, url, title=facts["title"])
    prev = db.execute(select(PageSnapshot).where(PageSnapshot.page_id == page.id)
                      .order_by(PageSnapshot.captured_at.desc()).limit(1)).scalar_one_or_none()
    # Technical facts / checks are compared with the last AUDIT (other tools don't measure them).
    prev_audit = prev if prev is not None and prev.tech is not None else db.execute(
        select(PageSnapshot).where(PageSnapshot.page_id == page.id, PageSnapshot.tech.isnot(None))
        .order_by(PageSnapshot.captured_at.desc()).limit(1)).scalar_one_or_none()

    cur = {"facts": facts, "tech": tech, "checks": checks, "body_text": body,
           "content_hash": _text_hash(body) if body else None}
    changes = diff(prev, {**cur, "tech": None, "checks": None})
    if tech is not None and prev_audit is not None:
        audit_only = diff({"facts": {}, "tech": prev_audit.tech, "checks": prev_audit.checks},
                          {"facts": {}, "tech": tech, "checks": checks})
        changes += audit_only

    snap = PageSnapshot(
        page_id=page.id, workspace_id=workspace.id, source=source, source_id=source_id, device=device,
        audit_score=audit_score, content_score=content_score_value, keyword=keyword,
        overall_score=overall_score, category_scores=category_scores,
        facts=facts, tech=tech, checks=checks, content_hash=cur["content_hash"], body_text=body or None,
        changes=changes, captured_at=captured_at or datetime.now(timezone.utc),
    )
    db.add(snap)
    now = snap.captured_at
    page.url = url
    page.title = facts["title"][:512] or page.title
    page.last_checked_at = now
    page.snapshot_count = (page.snapshot_count or 0) + 1
    if audit_score is not None:
        page.latest_audit_score = audit_score
    if changes:
        page.last_changed_at = now
    if page.domain_id is None:
        page.domain_id = _domain_for(db, workspace, url)
    db.flush()
    _prune(db, page.id)
    if commit:
        db.commit()
    return snap


def _prune(db: Session, page_id: uuid.UUID) -> None:
    keep = select(PageSnapshot.id).where(PageSnapshot.page_id == page_id) \
        .order_by(PageSnapshot.captured_at.desc()).limit(MAX_SNAPSHOTS_PER_PAGE)
    db.query(PageSnapshot).filter(PageSnapshot.page_id == page_id, PageSnapshot.id.notin_(keep)) \
        .delete(synchronize_session=False)


def safe_record(db: Session, *args, **kwargs) -> None:
    """Record without ever failing the calling feature (history is a side effect)."""
    try:
        record(db, *args, **kwargs)
    except Exception:  # noqa: BLE001
        db.rollback()


# ── Reading ─────────────────────────────────────────────────────────────────

def get_page(db: Session, workspace: Workspace, page_id: str) -> SitePage | None:
    try:
        pid = uuid.UUID(page_id)
    except ValueError:
        return None
    return db.execute(select(SitePage).where(SitePage.id == pid, SitePage.workspace_id == workspace.id)
                      ).scalar_one_or_none()


def list_pages(db: Session, workspace: Workspace, *, domain_id: str | None, q: str, page: int, page_size: int,
               sort: str | None, descending: bool) -> tuple[list[dict], int]:
    where = [SitePage.workspace_id == workspace.id]
    if domain_id == "other":
        where.append(SitePage.domain_id.is_(None))
    elif domain_id:
        try:
            where.append(SitePage.domain_id == uuid.UUID(domain_id))
        except ValueError:
            return [], 0
    if q:
        like = f"%{q.lower()}%"
        where.append(func.lower(SitePage.url_key).like(like) | func.lower(SitePage.title).like(like))
    cols = {"score": SitePage.latest_audit_score, "checked": SitePage.last_checked_at,
            "changed": SitePage.last_changed_at, "url": SitePage.url_key, "snapshots": SitePage.snapshot_count}
    col = cols.get(sort or "", SitePage.last_checked_at)
    order = col.desc().nulls_last() if descending else col.asc().nulls_last()
    total = db.execute(select(func.count()).select_from(SitePage).where(*where)).scalar_one()
    rows = db.execute(select(SitePage).where(*where).order_by(order, SitePage.id)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()

    # Score change between the last two audits of each page on this page of results.
    ids = [r.id for r in rows]
    prev_scores: dict[uuid.UUID, list[int]] = {}
    if ids:
        ranked = select(PageSnapshot.page_id, PageSnapshot.audit_score,
                        func.row_number().over(partition_by=PageSnapshot.page_id,
                                               order_by=PageSnapshot.captured_at.desc()).label("rn")
                        ).where(PageSnapshot.page_id.in_(ids), PageSnapshot.audit_score.isnot(None)).subquery()
        for pid, score, _rn in db.execute(select(ranked).where(ranked.c.rn <= 2)).all():
            prev_scores.setdefault(pid, []).append(score)
    out = []
    for r in rows:
        s = prev_scores.get(r.id, [])
        out.append({**page_out(r), "score_delta": (s[0] - s[1]) if len(s) == 2 else None})
    return out, total


def page_out(p: SitePage) -> dict:
    return {"id": str(p.id), "url": p.url, "url_key": p.url_key, "title": p.title,
            "domain_id": str(p.domain_id) if p.domain_id else None, "latest_audit_score": p.latest_audit_score,
            "snapshot_count": p.snapshot_count, "first_seen_at": p.first_seen_at,
            "last_checked_at": p.last_checked_at, "last_changed_at": p.last_changed_at}


def timeline(db: Session, workspace: Workspace, page: SitePage, limit: int = 100) -> list[dict]:
    snaps = db.execute(select(PageSnapshot).where(PageSnapshot.page_id == page.id)
                       .order_by(PageSnapshot.captured_at.desc()).limit(limit)).scalars().all()
    return [{"id": str(s.id), "captured_at": s.captured_at, "source": s.source,
             "source_id": str(s.source_id) if s.source_id else None, "device": s.device,
             "audit_score": s.audit_score, "content_score": s.content_score,
             "overall_score": s.overall_score, "category_scores": s.category_scores, "keyword": s.keyword,
             "facts": s.facts, "tech": s.tech, "changes": s.changes or []} for s in snaps]


# ── Search Console: daily rows + before/after per change ────────────────────

def _store_daily(db: Session, page: SitePage, rows: list[dict]) -> None:
    for r in rows:
        values = {"page_id": page.id, "day": date.fromisoformat(r["keys"][0]), "clicks": int(r.get("clicks", 0)),
                  "impressions": int(r.get("impressions", 0)), "ctr": float(r.get("ctr", 0.0)),
                  "position": float(r["position"]) if r.get("position") is not None else None,
                  "fetched_at": datetime.now(timezone.utc)}
        db.execute(insert(PageSearchDaily).values(**values).on_conflict_do_update(
            constraint="uq_page_search_day",
            set_={k: values[k] for k in ("clicks", "impressions", "ctr", "position", "fetched_at")}))
    db.commit()


def sync_search_daily(db: Session, workspace: Workspace, page: SitePage, days: int) -> str:
    """Fetch daily GSC numbers for the page and store them. Returns a status:
    ok | not_connected | no_property | no_website | error."""
    from app.services import gsc_service

    if page.domain_id is None:
        return "no_website"
    domain = db.get(Domain, page.domain_id)
    if domain is None:
        return "no_website"
    try:
        token, site_url = gsc_service._resolve_property(db, workspace, domain)
    except gsc_service.GscNotConnectedError:
        return "not_connected"
    except gsc_service.GscPropertyError:
        return "no_property"
    except Exception:  # noqa: BLE001 — expired token, network …
        return "error"
    end = date.today() - timedelta(days=gsc_service.DATA_LATENCY_DAYS)
    start = end - timedelta(days=days - 1)
    # Search Console needs the exact URL; try it with and without the trailing slash.
    candidates = [page.url, page.url.rstrip("/") if page.url.endswith("/") else page.url + "/"]
    for candidate in dict.fromkeys(candidates):
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date"], "rowLimit": 1000,
                "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "equals",
                                                        "expression": candidate}]}]}
        try:
            rows = gsc_service.search_analytics(token, site_url, body)
        except Exception:  # noqa: BLE001
            return "error"
        if rows:
            _store_daily(db, page, rows)
            break
    return "ok"


def _window(rows: list[PageSearchDaily], start: date, end: date) -> dict | None:
    sel = [r for r in rows if start <= r.day <= end]
    if not sel:
        return None
    clicks = sum(r.clicks for r in sel)
    impr = sum(r.impressions for r in sel)
    weighted = [(r.position, r.impressions) for r in sel if r.position is not None and r.impressions]
    pos = sum(p * i for p, i in weighted) / sum(i for _, i in weighted) if weighted else None
    return {"days": (end - start).days + 1, "clicks": clicks, "impressions": impr,
            "ctr": round(clicks / impr, 4) if impr else 0.0, "position": round(pos, 1) if pos else None}


def performance(db: Session, workspace: Workspace, page: SitePage, *, days: int = 180, sync: bool = True) -> dict:
    """Daily search series + before/after numbers for every snapshot that had changes."""
    status = sync_search_daily(db, workspace, page, days) if sync else "ok"
    since = date.today() - timedelta(days=days + 5)
    rows = db.execute(select(PageSearchDaily).where(PageSearchDaily.page_id == page.id, PageSearchDaily.day >= since)
                      .order_by(PageSearchDaily.day)).scalars().all()
    last_day = rows[-1].day if rows else None

    impacts = []
    changed = db.execute(select(PageSnapshot).where(PageSnapshot.page_id == page.id)
                         .order_by(PageSnapshot.captured_at)).scalars().all()
    all_rows = db.execute(select(PageSearchDaily).where(PageSearchDaily.page_id == page.id)).scalars().all()
    for s in changed:
        if not s.changes:
            continue
        d = s.captured_at.date()
        before = _window(all_rows, d - timedelta(days=WINDOW_DAYS), d - timedelta(days=1))
        after_end = min(d + timedelta(days=WINDOW_DAYS), last_day) if last_day else None
        after = _window(all_rows, d + timedelta(days=1), after_end) if after_end and after_end > d else None
        too_early = after is None or after["days"] < MIN_AFTER_DAYS
        impacts.append({"snapshot_id": str(s.id), "date": d.isoformat(), "before": before,
                        "after": None if too_early else after,
                        "after_days": after["days"] if after else 0, "too_early": too_early})
    return {
        "status": status,
        "series": [{"day": r.day.isoformat(), "clicks": r.clicks, "impressions": r.impressions,
                    "ctr": r.ctr, "position": r.position} for r in rows],
        "impacts": impacts,
        "window_days": WINDOW_DAYS,
    }


# ── Backfill (migration 0035) ───────────────────────────────────────────────

def backfill_from_audits(db: Session) -> int:
    """Turn saved page audits into page-history snapshots (oldest first).

    Old audits only stored title / meta / H1 and technical facts, so those are
    the only fields compared for them; later snapshots add the rest."""
    from app.models.page_audit import PageAudit

    n = 0
    audits = db.execute(select(PageAudit).order_by(PageAudit.created_at)).scalars().all()
    for a in audits:
        ws = db.get(Workspace, a.workspace_id)
        if ws is None:
            continue
        m = a.metrics or {}
        page = get_or_create_page(db, ws, a.url, title=m.get("title") or "")
        facts = {"title": m.get("title") or "", "meta": m.get("description") or "",
                 "h1": [m["h1_text"]] if m.get("h1_text") else []}
        checks = [{"id": c.get("id"), "label": c.get("label"), "status": c.get("status")} for c in (a.checks or [])]
        tech = audit_tech(a.status_code, a.load_time_ms, a.page_size_bytes, m, checks)
        prev = db.execute(select(PageSnapshot).where(PageSnapshot.page_id == page.id)
                          .order_by(PageSnapshot.captured_at.desc()).limit(1)).scalar_one_or_none()
        changes = diff(prev, {"facts": facts, "tech": tech, "checks": checks})
        db.add(PageSnapshot(page_id=page.id, workspace_id=ws.id, captured_at=a.created_at, source="audit",
                            source_id=a.id, device=m.get("device") or "desktop", audit_score=a.score,
                            facts=facts, tech=tech, checks=checks, changes=changes))
        page.url = a.url
        page.title = facts["title"][:512] or page.title
        page.first_seen_at = min(page.first_seen_at or a.created_at, a.created_at)
        page.last_checked_at = a.created_at
        page.latest_audit_score = a.score
        page.snapshot_count = (page.snapshot_count or 0) + 1
        if changes:
            page.last_changed_at = a.created_at
        db.flush()
        n += 1
    db.commit()
    return n


def for_run(db: Session, workspace: Workspace, source_id: uuid.UUID) -> tuple[uuid.UUID | None, list[dict]]:
    """(page id, changes) recorded for a tool run (e.g. an audit), if any."""
    snap = db.execute(select(PageSnapshot).where(PageSnapshot.workspace_id == workspace.id,
                                                 PageSnapshot.source_id == source_id)
                      .order_by(PageSnapshot.captured_at.desc()).limit(1)).scalar_one_or_none()
    return (snap.page_id, snap.changes or []) if snap else (None, [])
