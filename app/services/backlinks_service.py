"""Backlinks — DataForSEO Backlinks API, fetched on demand and stored.

Budget rules (the live API is paid; the sandbox is free dummy data):
  * reads are DB-only; a fetch happens ONLY on an explicit refresh call;
  * a stored run is reused until the user forces a refresh (no automatic expiry);
  * rows per list are capped (DATAFORSEO_BACKLINKS_ROWS) and a link gap compares
    at most DATAFORSEO_BACKLINKS_MAX_COMPETITORS competitors;
  * live fetches are refused when DATAFORSEO_LIVE_ENABLED is off, and only allowed
    for the workspace's own site and its tracked competitors;
  * the provider-reported cost of every fetch is stored on the run.

Profile = summary + top referring domains, backlinks (one per domain) and
anchors: 4 requests. Link gap = domains linking to your competitors but not to
you (domain_intersection): 1 request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.backlink import (
    KIND_GAP, KIND_PROFILE, BacklinkAnchor, BacklinkGapDomain, BacklinkLink,
    BacklinkReferringDomain, BacklinkRun,
)
from app.models.domain import Domain
from app.services import data_env, dataforseo_client
from app.services.ranked_keywords_service import allowed_for_domain, norm_host

PRICE_PER_REQUEST = 0.024
PRICE_PER_ROW = 0.000036


class BacklinksFetchNotAllowed(Exception):
    """Live fetch refused (paid API off, or domain not yours / not a competitor)."""


class NoCompetitors(Exception):
    """A link gap needs at least one tracked competitor."""


def rows_limit() -> int:
    return max(1, min(int(settings.DATAFORSEO_BACKLINKS_ROWS), 1000))


def estimated_cost(kind: str, env: str) -> float:
    """Projected USD for one fetch (0 in the sandbox)."""
    if env == data_env.SANDBOX:
        return 0.0
    rows = rows_limit()
    if kind == KIND_PROFILE:  # summary (1 row) + 3 lists
        return round(4 * PRICE_PER_REQUEST + (1 + 3 * rows) * PRICE_PER_ROW, 4)
    return round(PRICE_PER_REQUEST + rows * PRICE_PER_ROW, 4)


def tracked_competitors(db: Session, domain: Domain) -> list[str]:
    """The domain's competitor list (oldest first), capped for the link gap."""
    rows = db.execute(
        text("SELECT competitor FROM tracked_competitors WHERE domain_id = :d ORDER BY created_at, competitor"),
        {"d": str(domain.id)},
    ).scalars().all()
    return [norm_host(c) for c in rows][: settings.DATAFORSEO_BACKLINKS_MAX_COMPETITORS]


def can_fetch(target: str, env: str, allowed: set[str]) -> bool:
    if env == data_env.SANDBOX:
        return True
    return settings.DATAFORSEO_LIVE_ENABLED and norm_host(target) in allowed


def _check(target: str, env: str, allowed: set[str]) -> None:
    if env == data_env.SANDBOX:
        return
    if not settings.DATAFORSEO_LIVE_ENABLED:
        raise BacklinksFetchNotAllowed(
            "Fetching new backlink data is turned off right now — saved data is still shown.")
    if norm_host(target) not in allowed:
        raise BacklinksFetchNotAllowed(
            f"{target} isn't your domain or one of your tracked competitors. "
            "Add it as a competitor first.")


def _now() -> datetime:
    # Python clock, not SQL now(): now() is fixed per transaction, so two fetches
    # in one transaction would tie and "newest run" would be ambiguous.
    return datetime.now(timezone.utc)


def _dt(value) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def _int(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def latest_run(db: Session, kind: str, target: str, env: str) -> BacklinkRun | None:
    return db.execute(
        select(BacklinkRun)
        .where(BacklinkRun.environment == env, BacklinkRun.kind == kind,
               BacklinkRun.target == norm_host(target))
        .order_by(BacklinkRun.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# ── Fetch (the only paths that call DataForSEO) ─────────────────────────────

def fetch_profile(db: Session, target: str, *, env: str, allowed: set[str],
                  force: bool = False) -> tuple[BacklinkRun, bool]:
    """Stored profile for `target`, fetching it only if missing or forced.
    Returns (run, fetched_now)."""
    target = norm_host(target)
    existing = latest_run(db, KIND_PROFILE, target, env)
    if existing and not force:
        return existing, False
    _check(target, env, allowed)

    rows = rows_limit()
    summary = dataforseo_client.backlinks_summary(target, env=env)
    refs = dataforseo_client.backlinks_referring_domains(target, limit=rows, env=env)
    links = dataforseo_client.backlinks_list(target, limit=rows, env=env)
    anchors = dataforseo_client.backlinks_anchors(target, limit=rows, env=env)
    s = summary["summary"]

    run = BacklinkRun(
        environment=env, kind=KIND_PROFILE, target=target, fetched_at=_now(),
        rank=_int(s.get("rank")), backlinks=_int(s.get("backlinks")),
        referring_domains=_int(s.get("referring_domains")),
        referring_domains_nofollow=_int(s.get("referring_domains_nofollow")),
        referring_main_domains=_int(s.get("referring_main_domains")),
        referring_ips=_int(s.get("referring_ips")),
        broken_backlinks=_int(s.get("broken_backlinks")),
        spam_score=_int(s.get("backlinks_spam_score")),
        first_seen=_dt(s.get("first_seen")),
        total_count=_int(refs.get("total_count")),
        items_count=len(refs["items"]) + len(links["items"]) + len(anchors["items"]),
        requests=4,
        cost=round(sum(x["cost"] for x in (summary, refs, links, anchors)), 5),
        summary={k: s.get(k) for k in (
            "referring_links_types", "referring_links_attributes", "referring_links_tld",
            "referring_links_platform_types", "referring_links_countries", "info",
            "referring_pages", "crawled_pages")},
    )
    db.add(run)
    db.flush()
    db.add_all([BacklinkReferringDomain(
        run_id=run.id, domain=i["domain"][:255], rank=_int(i["rank"]), backlinks=_int(i["backlinks"]),
        nofollow_backlinks=_int(i["nofollow_backlinks"]), spam_score=_int(i["spam_score"]),
        first_seen=_dt(i["first_seen"]), lost_date=_dt(i["lost_date"]),
    ) for i in refs["items"]])
    db.add_all([BacklinkLink(
        run_id=run.id, url_from=i["url_from"], domain_from=(i["domain_from"] or "")[:255] or None,
        url_to=i["url_to"], anchor=i["anchor"], item_type=(i["item_type"] or "")[:20] or None,
        dofollow=i["dofollow"], is_new=i["is_new"], is_lost=i["is_lost"], is_broken=i["is_broken"],
        domain_from_rank=_int(i["domain_from_rank"]), page_from_rank=_int(i["page_from_rank"]),
        page_from_title=i["page_from_title"], spam_score=_int(i["spam_score"]),
        first_seen=_dt(i["first_seen"]), last_seen=_dt(i["last_seen"]),
    ) for i in links["items"]])
    db.add_all([BacklinkAnchor(
        run_id=run.id, anchor=i["anchor"], backlinks=_int(i["backlinks"]),
        referring_domains=_int(i["referring_domains"]),
        referring_domains_nofollow=_int(i["referring_domains_nofollow"]),
        rank=_int(i["rank"]), spam_score=_int(i["spam_score"]), first_seen=_dt(i["first_seen"]),
    ) for i in anchors["items"]])
    db.commit()
    return run, True


def fetch_gap(db: Session, domain: Domain, *, env: str, force: bool = False) -> tuple[BacklinkRun, bool]:
    """Stored link gap for the domain vs its tracked competitors, fetched only if
    missing, forced, or the competitor list changed. Returns (run, fetched_now)."""
    ours = norm_host(domain.domain)
    competitors = tracked_competitors(db, domain)
    if not competitors:
        raise NoCompetitors("Add at least one competitor to find link opportunities.")
    existing = latest_run(db, KIND_GAP, ours, env)
    if existing and not force and sorted(existing.competitors) == sorted(competitors):
        return existing, False
    for c in competitors:
        _check(c, env, allowed_for_domain(db, domain))

    res = dataforseo_client.backlinks_gap(competitors, ours, limit=rows_limit(), env=env)
    run = BacklinkRun(environment=env, kind=KIND_GAP, target=ours, competitors=competitors, fetched_at=_now(),
                      total_count=_int(res.get("total_count")), items_count=len(res["items"]),
                      requests=1, cost=round(res["cost"], 5))
    db.add(run)
    db.flush()
    db.add_all([BacklinkGapDomain(
        run_id=run.id, domain=(i["domain"] or "")[:255], rank=_int(i["rank"]),
        competitors_linking=i["competitors_linking"], links=i["links"], spam_score=_int(i["spam_score"]),
    ) for i in res["items"] if i.get("domain")])
    db.commit()
    return run, True


# ── Read (DB only) ──────────────────────────────────────────────────────────

def _run_meta(run: BacklinkRun | None) -> dict:
    return {
        "fetched_at": run.fetched_at if run else None,
        "cost": float(run.cost) if run and run.cost is not None else None,
        "requests": run.requests if run else None,
    }


def _rows(db: Session, model, run_id, order):
    return db.execute(select(model).where(model.run_id == run_id).order_by(*order)).scalars().all()


def profile_payload(db: Session, domain: Domain, target: str | None) -> dict:
    env = data_env.env_for_domain(db, domain)
    ours = norm_host(domain.domain)
    target = norm_host(target or ours)
    allowed = allowed_for_domain(db, domain)
    run = latest_run(db, KIND_PROFILE, target, env)
    out = {
        "target": target, "is_you": target == ours, "environment": env,
        "live_enabled": settings.DATAFORSEO_LIVE_ENABLED,
        "can_fetch": can_fetch(target, env, allowed),
        "estimated_cost": estimated_cost(KIND_PROFILE, env),
        **_run_meta(run), "summary": None,
        "referring_domains": [], "backlinks": [], "anchors": [],
    }
    if not run:
        return out
    nofollow = run.referring_domains_nofollow or 0
    out["summary"] = {
        "rank": run.rank, "backlinks": run.backlinks, "referring_domains": run.referring_domains,
        "dofollow_referring_domains": (run.referring_domains - nofollow) if run.referring_domains is not None else None,
        "referring_main_domains": run.referring_main_domains, "referring_ips": run.referring_ips,
        "broken_backlinks": run.broken_backlinks, "spam_score": run.spam_score,
        "first_seen": run.first_seen, "link_types": run.summary.get("referring_links_types") or {},
        "link_attributes": run.summary.get("referring_links_attributes") or {},
    }
    links = _rows(db, BacklinkLink, run.id, (BacklinkLink.domain_from_rank.desc().nulls_last(),))
    out["backlinks"] = [{
        "url_from": r.url_from, "domain_from": r.domain_from, "url_to": r.url_to, "anchor": r.anchor,
        "item_type": r.item_type, "dofollow": r.dofollow, "is_new": r.is_new, "is_lost": r.is_lost,
        "is_broken": r.is_broken, "domain_from_rank": r.domain_from_rank, "page_from_title": r.page_from_title,
        "spam_score": r.spam_score, "first_seen": r.first_seen, "last_seen": r.last_seen,
    } for r in links]

    # "Authority" of a linking domain = its own domain rank (domain_from_rank on
    # the backlinks rows). The referring_domains endpoint's `rank` is a different
    # figure (0 for nearly every domain) and reads as "authority 0", so it is not
    # shown: a domain with no saved backlink row gets None ("—", unknown).
    authority: dict[str, int] = {}
    for link in links:
        host = norm_host(link.domain_from or "")
        if host and link.domain_from_rank is not None:
            authority[host] = max(authority.get(host, 0), link.domain_from_rank)

    def _authority(domain: str) -> int | None:
        host = norm_host(domain)
        found = [v for h, v in authority.items() if h == host or h.endswith("." + host)]
        return max(found) if found else None

    refs = [{
        "domain": r.domain, "rank": _authority(r.domain), "backlinks": r.backlinks,
        "nofollow_backlinks": r.nofollow_backlinks, "spam_score": r.spam_score,
        "first_seen": r.first_seen, "lost_date": r.lost_date,
    } for r in _rows(db, BacklinkReferringDomain, run.id, (BacklinkReferringDomain.backlinks.desc().nulls_last(),))]
    refs.sort(key=lambda r: (r["rank"] is None, -(r["rank"] or 0), -(r["backlinks"] or 0)))
    out["referring_domains"] = refs
    # Share of the stored linking domains DataForSEO flags as likely spam (>= 30).
    scored = [r for r in refs if r["spam_score"] is not None]
    out["summary"]["spammy_share"] = (
        round(sum(1 for r in scored if r["spam_score"] >= 30) / len(scored), 2) if scored else None)
    out["anchors"] = [{
        "anchor": r.anchor, "backlinks": r.backlinks, "referring_domains": r.referring_domains,
        "referring_domains_nofollow": r.referring_domains_nofollow, "spam_score": r.spam_score,
        "first_seen": r.first_seen,
    } for r in _rows(db, BacklinkAnchor, run.id, (BacklinkAnchor.backlinks.desc().nulls_last(),))]
    return out


def gap_payload(db: Session, domain: Domain) -> dict:
    env = data_env.env_for_domain(db, domain)
    ours = norm_host(domain.domain)
    competitors = tracked_competitors(db, domain)
    run = latest_run(db, KIND_GAP, ours, env)
    allowed = allowed_for_domain(db, domain)
    out = {
        "your_domain": ours, "environment": env, "live_enabled": settings.DATAFORSEO_LIVE_ENABLED,
        "competitors": competitors,
        "run_competitors": list(run.competitors) if run else [],
        "competitors_changed": bool(run) and sorted(run.competitors) != sorted(competitors),
        "can_fetch": bool(competitors) and all(can_fetch(c, env, allowed) for c in competitors),
        "estimated_cost": estimated_cost(KIND_GAP, env),
        "total_count": run.total_count if run else None,
        **_run_meta(run), "domains": [],
    }
    if run:
        out["domains"] = [{
            "domain": r.domain, "rank": r.rank, "competitors_linking": r.competitors_linking,
            "links": r.links, "spam_score": r.spam_score,
        } for r in _rows(db, BacklinkGapDomain, run.id,
                         (BacklinkGapDomain.competitors_linking.desc(), BacklinkGapDomain.rank.desc().nulls_last()))]
    return out


def options(db: Session, domain: Domain) -> dict:
    """Domains to pick from: you + your competitors, with whether a profile is saved."""
    env = data_env.env_for_domain(db, domain)
    ours = norm_host(domain.domain)
    targets = [ours] + [c for c in tracked_competitors(db, domain) if c != ours]
    extra = [norm_host(c) for c in db.execute(
        text("SELECT competitor FROM tracked_competitors WHERE domain_id = :d ORDER BY created_at"),
        {"d": str(domain.id)}).scalars()]
    targets += [c for c in extra if c not in targets]
    saved = {r.target: r.fetched_at for r in db.execute(
        select(BacklinkRun).where(BacklinkRun.environment == env, BacklinkRun.kind == KIND_PROFILE,
                                  BacklinkRun.target.in_(targets))
        .order_by(BacklinkRun.target, BacklinkRun.fetched_at.desc()).distinct(BacklinkRun.target)
    ).scalars()}
    return {
        "your_domain": ours, "environment": env, "live_enabled": settings.DATAFORSEO_LIVE_ENABLED,
        "domains": [{"domain": t, "is_you": t == ours, "fetched_at": saved.get(t)} for t in targets],
    }


# ── Details (DB only): breakdowns, history, changes, paged lists ───────────

def _top(d: dict | None, n: int = 8, *, blank: str = "Unknown") -> list[dict]:
    items = [{"label": (k or blank), "count": int(v or 0)} for k, v in (d or {}).items()]
    items.sort(key=lambda x: -x["count"])
    return items[:n]


def details(db: Session, domain: Domain, target: str | None) -> dict:
    """Breakdowns of the stored profile + fetch history + what changed since the previous fetch."""
    env = data_env.env_for_domain(db, domain)
    target = norm_host(target or domain.domain)
    runs = db.execute(
        select(BacklinkRun).where(BacklinkRun.environment == env, BacklinkRun.kind == KIND_PROFILE,
                                  BacklinkRun.target == target)
        .order_by(BacklinkRun.fetched_at.desc()).limit(20)).scalars().all()
    out: dict = {"target": target, "breakdowns": None, "link_status": None, "history": [], "changes": None}
    if not runs:
        return out
    run = runs[0]
    s = run.summary or {}
    attrs = s.get("referring_links_attributes") or {}
    pages = s.get("referring_pages") or run.backlinks or 0
    nofollow = int(attrs.get("nofollow") or 0)
    out["breakdowns"] = {
        "follow": [{"label": "Dofollow", "count": max(0, pages - nofollow)}, {"label": "Nofollow", "count": nofollow},
                   *[{"label": k.capitalize(), "count": int(attrs.get(k) or 0)} for k in ("ugc", "sponsored") if attrs.get(k)]],
        "link_types": [{**x, "label": x["label"].replace("_", " ").capitalize()} for x in _top(s.get("referring_links_types"))],
        "tlds": [{"label": f".{x['label']}", "count": x["count"]} for x in _top(s.get("referring_links_tld"))],
        "countries": _top(s.get("referring_links_countries")),
        "platforms": [{**x, "label": x["label"].replace("_", " ").capitalize()} for x in _top(s.get("referring_links_platform_types"))],
        "referring_pages": s.get("referring_pages"),
        "crawled_pages": s.get("crawled_pages"),
    }
    links = _rows(db, BacklinkLink, run.id, (BacklinkLink.id,))
    out["link_status"] = {
        "saved": len(links),
        "dofollow": sum(1 for x in links if x.dofollow),
        "nofollow": sum(1 for x in links if x.dofollow is False),
        "new": sum(1 for x in links if x.is_new),
        "lost": sum(1 for x in links if x.is_lost),
        "broken": sum(1 for x in links if x.is_broken),
    }
    out["history"] = [{
        "fetched_at": r.fetched_at, "rank": r.rank, "backlinks": r.backlinks,
        "referring_domains": r.referring_domains, "spam_score": r.spam_score,
        "cost": float(r.cost) if r.cost is not None else None,
    } for r in runs]
    if len(runs) > 1:
        prev = runs[1]
        now_d = {x.domain for x in _rows(db, BacklinkReferringDomain, run.id, (BacklinkReferringDomain.domain,))}
        old_d = {x.domain for x in _rows(db, BacklinkReferringDomain, prev.id, (BacklinkReferringDomain.domain,))}

        def delta(a, b):
            return None if a is None or b is None else a - b

        out["changes"] = {
            "previous_fetched_at": prev.fetched_at,
            "backlinks": delta(run.backlinks, prev.backlinks),
            "referring_domains": delta(run.referring_domains, prev.referring_domains),
            "rank": delta(run.rank, prev.rank),
            "gained_domains": sorted(now_d - old_d)[:50],
            "lost_domains": sorted(old_d - now_d)[:50],
        }
    return out


LISTS = ("domains", "links", "anchors", "pages")


def list_rows(db: Session, domain: Domain, target: str | None, which: str, *, q: str = "",
              filter_: str = "all", sort: str | None = None, descending: bool = True,
              page: int = 1, page_size: int = 25) -> tuple[list[dict], dict]:
    """One page of a stored list (referring domains, backlinks, anchors or your linked pages)."""
    from app.services.pagination import paginate

    payload = profile_payload(db, domain, target)
    q = (q or "").strip().lower()
    if which == "domains":
        rows = payload["referring_domains"]
        for r in rows:
            r["dofollow_backlinks"] = None if r["backlinks"] is None else max(0, r["backlinks"] - (r["nofollow_backlinks"] or 0))
        if q:
            rows = [r for r in rows if q in r["domain"].lower()]
        rows = {
            "dofollow": [r for r in rows if (r["dofollow_backlinks"] or 0) > 0],
            "nofollow": [r for r in rows if r["backlinks"] and (r["dofollow_backlinks"] or 0) == 0],
            "clean": [r for r in rows if (r["spam_score"] or 0) < 30],
            "spam": [r for r in rows if (r["spam_score"] or 0) >= 30],
            "lost": [r for r in rows if r["lost_date"]],
        }.get(filter_, rows)
        keys = {"authority": "rank", "backlinks": "backlinks", "spam": "spam_score", "first_seen": "first_seen", "domain": "domain"}
        key = keys.get(sort or "", None)
    elif which == "links":
        rows = payload["backlinks"]
        if q:
            rows = [r for r in rows if q in (r["url_from"] or "").lower() or q in (r["anchor"] or "").lower()
                    or q in (r["page_from_title"] or "").lower()]
        rows = {
            "dofollow": [r for r in rows if r["dofollow"]],
            "nofollow": [r for r in rows if r["dofollow"] is False],
            "new": [r for r in rows if r["is_new"]],
            "lost": [r for r in rows if r["is_lost"]],
            "broken": [r for r in rows if r["is_broken"]],
        }.get(filter_, rows)
        keys = {"authority": "domain_from_rank", "first_seen": "first_seen", "last_seen": "last_seen", "spam": "spam_score"}
        key = keys.get(sort or "", None)
    elif which == "anchors":
        rows = payload["anchors"]
        if q:
            rows = [r for r in rows if q in (r["anchor"] or "").lower()]
        keys = {"backlinks": "backlinks", "domains": "referring_domains", "first_seen": "first_seen"}
        key = keys.get(sort or "", None)
    elif which == "pages":
        grouped: dict[str, dict] = {}
        for link in payload["backlinks"]:
            url = link["url_to"] or "(unknown)"
            g = grouped.setdefault(url, {"url": url, "links": 0, "domains": set(), "dofollow": 0, "best_authority": None,
                                          "anchors": set()})
            g["links"] += 1
            if link["domain_from"]:
                g["domains"].add(link["domain_from"])
            if link["dofollow"]:
                g["dofollow"] += 1
            if link["domain_from_rank"] is not None:
                g["best_authority"] = max(g["best_authority"] or 0, link["domain_from_rank"])
            if link["anchor"]:
                g["anchors"].add(link["anchor"])
        rows = [{**g, "domains": len(g["domains"]), "anchors": sorted(g["anchors"])[:5]} for g in grouped.values()]
        if q:
            rows = [r for r in rows if q in r["url"].lower()]
        keys = {"links": "links", "domains": "domains", "authority": "best_authority"}
        key = keys.get(sort or "", "links")
    else:
        raise ValueError("Unknown list.")
    return paginate(rows, page=page, page_size=page_size, sort_key=key, descending=descending)
