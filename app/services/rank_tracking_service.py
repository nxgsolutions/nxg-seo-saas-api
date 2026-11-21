"""Automated rank tracking: fetch a keyword's SERP, store history, derive the
own-vs-competitor rank timeline, and process keywords that are due.

One SERP fetch per tracked keyword captures everything we need: our own organic
position + every competitor's position and URL. We store our position in
``rank_entries`` (so the summary cards + the "you" line stay authoritative) and
derive competitor lines at read time straight from ``serp_snapshot_items`` — no
duplicated competitor table.

Spend policy ("Sandbox auto, live click"): sandbox workspaces call the free
DataForSEO sandbox and are always eligible for the scheduler; live workspaces
are only auto-processed when ``RANK_TRACKING_LIVE_ENABLED`` is on — otherwise a
live fetch happens only on an explicit user click (``force=True``).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.domain import Domain
from app.models.keyword_analysis import Keyword
from app.models.ranking import RankEntry, TrackedKeyword
from app.models.workspace import Workspace
from app.services import (
    data_env,
    domain_settings,
    keyword_scoring,
    rank_service,
    serp_scrape_service,
)
from app.services.keyword_analysis_service import _NON_COMPETITOR_HOSTS
from app.services.serp_scrape_service import ScrapeBlockedError, ScrapeError

logger = logging.getLogger(__name__)


def _norm_host(host: str) -> str:
    h = re.sub(r"^https?://", "", (host or "").strip().lower()).split("/")[0]
    return h[4:] if h.startswith("www.") else h


# Huge general-purpose brands that rank for all sorts of queries but are never a
# site's SEO competitor — big Q&A/content platforms and major news/media. Kept
# separate from _NON_COMPETITOR_HOSTS (search/social/CDN) and applied on top for
# the rank-tracking competitor graph. Niche brands (e.g. edtech peers) are NOT
# here — they stay as real competitors. Substring match on the host.
_EXCLUDED_BRANDS = (
    # Q&A / content / document platforms
    "quora.", "medium.com", "scribd.", "slideshare.", "academia.edu",
    "researchgate.", "blogspot.", "wordpress.com", "issuu.", "flipkart.",
    # Major Indian news / media
    "timesofindia", "indiatimes", "hindustantimes", "ndtv.", "indianexpress",
    "jagran", "news18", "livemint", "business-standard", "thehindu",
    "deccanherald", "firstpost", "zeenews", "aajtak", "abplive", "indiatoday",
    "financialexpress", "economictimes", "livehindustan",
)


def _is_competitor_host(host: str, own_host: str) -> bool:
    if not host or host == own_host:
        return False
    if any(bad in host for bad in _NON_COMPETITOR_HOSTS):
        return False
    if any(bad in host for bad in _EXCLUDED_BRANDS):
        return False
    return True


def _keyword_id(db: Session, tracked_kw: TrackedKeyword) -> str | None:
    """The global keyword_id SERP snapshots are keyed by (link or normalized term)."""
    if tracked_kw.keyword_id is not None:
        return str(tracked_kw.keyword_id)
    normalized = keyword_scoring.normalize_keyword(tracked_kw.term)
    kid = db.execute(
        select(Keyword.id).where(Keyword.normalized_keyword == normalized)
    ).scalar_one_or_none()
    return str(kid) if kid else None


def _snapshot_date(snap: dict) -> date:
    captured = snap.get("captured_at")
    if captured:
        try:
            return datetime.fromisoformat(captured).date()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _own_position(rows: list[dict], own_host: str) -> tuple[int | None, str | None]:
    """Our best organic position + its URL from a snapshot's rows. None = not ranking."""
    best: tuple[int, str | None] | None = None
    for r in rows:
        if _norm_host(r.get("domain") or "") != own_host:
            continue
        pos = r.get("rank_group") or r.get("position")
        if pos is None:
            continue
        if best is None or pos < best[0]:
            best = (int(pos), r.get("url"))
    return (best[0], best[1]) if best else (None, None)


def _recent_snapshot(
    db: Session, term: str, country: str, language: str, env: str, max_age_hours: int
) -> dict | None:
    """A stored SERP snapshot younger than ``max_age_hours`` (reuse to avoid a
    same-day double charge), else None."""
    snap = serp_scrape_service.latest_snapshot(
        db, term, country=country, language=language, env=env
    )
    if not snap or not snap.get("captured_at"):
        return None
    try:
        captured = datetime.fromisoformat(snap["captured_at"])
    except ValueError:
        return None
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - captured
    return snap if age <= timedelta(hours=max_age_hours) else None


def fetch_keyword_serp(
    db: Session,
    domain: Domain,
    tracked_kw: TrackedKeyword,
    *,
    env: str | None = None,
    force: bool = False,
) -> dict:
    """Fetch (or reuse) one keyword's SERP, record our position, and advance the
    schedule. ``force`` (a user click) always fetches fresh; otherwise a snapshot
    younger than RANK_TRACKING_DEDUP_HOURS is reused.

    Raises ScrapeError/ScrapeBlockedError on a provider failure — the keyword is
    left in status 'error' with last_error set, and NO rank entry is written (an
    API failure must not look like a ranking loss).
    """
    if env is None:
        env = data_env.env_for_domain(db, domain)

    # Concurrency guard: never run two fetches for the same keyword at once.
    if tracked_kw.tracking_status == "processing" and not force:
        return {"skipped": "already processing", "term": tracked_kw.term}

    conf = domain_settings.resolve(domain, getattr(domain, "workspace", None))
    country, language = conf.country, conf.language

    tracked_kw.tracking_status = "processing"
    db.commit()

    try:
        snap = None
        if not force:
            snap = _recent_snapshot(
                db, tracked_kw.term, country, language, env,
                settings.RANK_TRACKING_DEDUP_HOURS,
            )
            reused = snap is not None
        else:
            reused = False
        if snap is None:
            snap = serp_scrape_service.analyze_keyword(
                db, tracked_kw.term, country=country, language=language,
                depth=settings.RANK_TRACKING_DEPTH, provider="dataforseo", env=env,
            )
    except (ScrapeError, ScrapeBlockedError) as exc:
        db.rollback()
        kw = db.get(TrackedKeyword, tracked_kw.id)
        if kw is not None:
            kw.tracking_status = "error"
            kw.last_error = str(exc)[:500]
            db.commit()
        logger.warning("rank-tracking fetch failed for %r: %s", tracked_kw.term, exc)
        raise

    rows = snap.get("rows") or []
    own_host = _norm_host(domain.domain)
    our_pos, our_url = _own_position(rows, own_host)
    recorded_at = _snapshot_date(snap)

    # record_position commits; it handles the null position ("Not Ranking") too.
    rank_service.record_position(
        db, tracked_kw, our_pos, recorded_at, url=our_url, source="serp"
    )

    kw = db.get(TrackedKeyword, tracked_kw.id)
    now = datetime.now(timezone.utc)
    kw.last_fetched_at = now
    kw.next_fetch_at = now + timedelta(days=kw.tracking_frequency_days)
    kw.tracking_status = "idle"
    kw.last_error = None
    db.commit()

    competitors = sum(
        1 for r in rows if _is_competitor_host(_norm_host(r.get("domain") or ""), own_host)
    )
    return {
        "term": tracked_kw.term,
        "reused": reused,
        "our_position": our_pos,
        "recorded_at": recorded_at.isoformat(),
        "results": len(rows),
        "competitors": competitors,
        "env": env,
    }


def fetch_keyword_task(domain_id, tracked_keyword_id, *, only_free: bool = True) -> None:
    """Background-task entry point (own DB session). Used to fetch a keyword's
    initial SERP right after it is added. ``only_free`` restricts it to sandbox
    workspaces so adding a keyword never triggers paid live spend."""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        kw = db.get(TrackedKeyword, tracked_keyword_id)
        dom = db.get(Domain, domain_id)
        if kw is None or dom is None:
            return
        env = data_env.env_for_domain(db, dom)
        if only_free and env != data_env.SANDBOX:
            return
        fetch_keyword_serp(db, dom, kw, env=env, force=True)
    except (ScrapeError, ScrapeBlockedError) as exc:
        logger.warning("background rank-tracking fetch failed: %s", exc)
    except Exception:  # noqa: BLE001 - background task must never raise
        logger.exception("background rank-tracking fetch crashed")
    finally:
        db.close()


def rank_history(
    db: Session,
    domain: Domain,
    tracked_kw: TrackedKeyword,
    *,
    start: date | None = None,
    end: date | None = None,
    env: str | None = None,
    top: int = 8,
) -> dict:
    """Build the multi-series rank timeline for one tracked keyword:
    our line (from rank_entries) + one line per competitor domain that appears in
    the keyword's SERP snapshots (derived live from serp_snapshot_items).
    """
    if env is None:
        env = data_env.env_for_domain(db, domain)
    own_host = _norm_host(domain.domain)

    # ── our line (authoritative, includes any GSC/manual entries) ─────────────
    entries = rank_service.list_entries(db, tracked_kw)
    if start is not None:
        entries = [e for e in entries if e.recorded_at >= start]
    if end is not None:
        entries = [e for e in entries if e.recorded_at <= end]
    you_map: dict[date, dict] = {
        e.recorded_at: {"position": e.position, "url": e.url} for e in entries
    }

    # ── competitor lines (and our SERP positions) from snapshot items ─────────
    kid = _keyword_id(db, tracked_kw)
    comp_dates: set[date] = set()
    # host -> {date -> (pos, url)}
    host_series: dict[str, dict[date, tuple[int, str | None]]] = {}
    host_hits: dict[str, int] = {}
    if kid is not None:
        params: dict = {"kid": kid, "env": env}
        where_dates = ""
        if start is not None:
            where_dates += " AND s.captured_at::date >= :start"
            params["start"] = start
        if end is not None:
            where_dates += " AND s.captured_at::date <= :end"
            params["end"] = end
        sql = text(
            f"""
            SELECT s.captured_at::date AS d,
                   i.domain AS host,
                   min(coalesce(i.rank_group, i.rank_absolute)) AS pos,
                   (array_agg(i.url ORDER BY coalesce(i.rank_group, i.rank_absolute)))[1] AS url
            FROM serp_snapshots s
            JOIN serp_snapshot_items i ON i.snapshot_id = s.id
            WHERE s.keyword_id = :kid
              AND s.environment = :env
              AND i.type = 'organic'
              {where_dates}
            GROUP BY s.captured_at::date, i.domain
            ORDER BY d ASC
            """
        )
        for row in db.execute(sql, params).mappings():
            d, host, pos = row["d"], _norm_host(row["host"] or ""), row["pos"]
            if not host or pos is None:
                continue
            comp_dates.add(d)
            if host == own_host:
                # fill our line for snapshot dates we have no explicit entry for
                you_map.setdefault(d, {"position": int(pos), "url": row["url"]})
                continue
            if not _is_competitor_host(host, own_host):
                continue
            host_series.setdefault(host, {})[d] = (int(pos), row["url"])
            host_hits[host] = host_hits.get(host, 0) + 1

    # top competitors by how often they appear, then best average position
    ranked_hosts = sorted(
        host_series.keys(),
        key=lambda h: (
            -host_hits.get(h, 0),
            sum(p for p, _ in host_series[h].values()) / max(1, len(host_series[h])),
        ),
    )[:top]

    dates = sorted(set(you_map.keys()) | comp_dates)

    def _pts(getter) -> list[dict]:
        return [getter(d) for d in dates]

    you_points = _pts(
        lambda d: {
            "date": d.isoformat(),
            "position": (you_map.get(d) or {}).get("position"),
            "url": (you_map.get(d) or {}).get("url"),
        }
    )
    competitor_series = []
    for host in ranked_hosts:
        series = host_series[host]
        competitor_series.append({
            "host": host,
            "points": [
                {
                    "date": d.isoformat(),
                    "position": (series.get(d) or (None, None))[0],
                    "url": (series.get(d) or (None, None))[1],
                }
                for d in dates
            ],
        })

    # ── summary (from our own line) ───────────────────────────────────────────
    ordered = [e for e in entries]
    positions = [e.position for e in ordered if e.position is not None]
    current = ordered[-1].position if ordered else None
    previous = ordered[-2].position if len(ordered) >= 2 else None
    best = min(positions) if positions else None
    change = (previous - current) if (current is not None and previous is not None) else None

    return {
        "keyword": tracked_kw.term,
        "you_host": own_host,
        "dates": [d.isoformat() for d in dates],
        "you": {"host": own_host, "points": you_points},
        "competitors": competitor_series,
        "meta": {
            "current": current,
            "previous": previous,
            "change": change,
            "best": best,
            "last_tracked": dates[-1].isoformat() if dates else None,
            "last_fetched_at": tracked_kw.last_fetched_at.isoformat() if tracked_kw.last_fetched_at else None,
            "next_fetch_at": tracked_kw.next_fetch_at.isoformat() if tracked_kw.next_fetch_at else None,
            "tracking_frequency_days": tracked_kw.tracking_frequency_days,
            "tracking_status": tracked_kw.tracking_status,
            "tracking_enabled": tracked_kw.tracking_enabled,
        },
    }


def competitor_timeline(
    db: Session,
    domain: Domain,
    *,
    env: str | None = None,
    freq: str = "weekly",
    start: date | None = None,
    end: date | None = None,
    keyword_ids: list[str] | None = None,
    competitors: list[str] | None = None,
    top: int = 8,
    min_hits: int = 2,
) -> dict:
    """Aggregated competitor rank over time across the domain's tracked keywords.

    For each period (weekly=Mon / monthly=1st) we take the latest SERP snapshot
    per keyword, then average each host's organic position across the selected
    keywords — so one line per competitor (and our own) shows how their typical
    rank moves. Reuses the rank-history chart shape (dates/you/competitors).

    ``keyword_ids`` filters to specific tracked keywords (ids); ``competitors``
    filters the plotted hosts. ``all_competitors`` in the result is the full
    candidate list (ignoring the competitor filter) so a multi-select stays stable.
    """
    if env is None:
        env = data_env.env_for_domain(db, domain)
    own_host = _norm_host(domain.domain)
    bucket = {"daily": "day", "weekly": "week", "monthly": "month"}.get(freq, "week")

    # Resolve the tracked keywords in scope → their global keyword_ids.
    tk_stmt = select(TrackedKeyword).where(
        TrackedKeyword.domain_id == domain.id,
        TrackedKeyword.keyword_id.isnot(None),
    )
    if keyword_ids:
        try:
            ids = [uuid.UUID(k) for k in keyword_ids]
            tk_stmt = tk_stmt.where(TrackedKeyword.id.in_(ids))
        except (ValueError, TypeError):
            pass
    tracked = list(db.execute(tk_stmt).scalars())
    kw_label = f"{len(tracked)} keyword" + ("" if len(tracked) == 1 else "s")
    global_ids = [str(t.keyword_id) for t in tracked if t.keyword_id is not None]
    empty = {
        "label": kw_label, "you_host": own_host, "freq": freq,
        "dates": [], "you": {"host": own_host, "points": []},
        "competitors": [], "all_competitors": [],
    }
    if not global_ids:
        return empty

    # Effective range for both the query and the continuous axis, capped at 12
    # months so the graph always spans the full selected range.
    end_d = end or datetime.now(timezone.utc).date()
    start_d = start or (end_d - timedelta(days=365))
    if (end_d - start_d).days > 366:
        start_d = end_d - timedelta(days=366)

    params: dict = {"env": env, "start": start_d, "end": end_d}
    where_dates = " AND s.captured_at::date >= :start AND s.captured_at::date <= :end"
    sql = text(
        f"""
        WITH latest AS (
            SELECT s.keyword_id, date_trunc(:bucket, s.captured_at) AS period,
                   max(s.captured_at) AS cap
            FROM serp_snapshots s
            WHERE s.keyword_id IN :kids AND s.environment = :env {where_dates}
            GROUP BY s.keyword_id, date_trunc(:bucket, s.captured_at)
        )
        SELECT l.period::date AS period, i.domain AS host,
               avg(coalesce(i.rank_group, i.rank_absolute))::float AS pos
        FROM latest l
        JOIN serp_snapshots s
          ON s.keyword_id = l.keyword_id AND s.captured_at = l.cap AND s.environment = :env
        JOIN serp_snapshot_items i ON i.snapshot_id = s.id AND i.type = 'organic'
        GROUP BY l.period, i.domain
        ORDER BY l.period ASC
        """
    ).bindparams(bindparam("kids", expanding=True))
    params["kids"] = global_ids
    params["bucket"] = bucket

    comp_filter = {_norm_host(c) for c in competitors} if competitors else None
    host_series: dict[str, dict[date, float]] = {}
    host_hits: dict[str, int] = {}
    you_map: dict[date, float] = {}
    for row in db.execute(sql, params).mappings():
        d, host, pos = row["period"], _norm_host(row["host"] or ""), row["pos"]
        if not host or pos is None:
            continue
        if host == own_host:
            you_map[d] = round(float(pos), 1)
            continue
        if not _is_competitor_host(host, own_host):
            continue
        host_series.setdefault(host, {})[d] = round(float(pos), 1)
        host_hits[host] = host_hits.get(host, 0) + 1

    # Full candidate list (best average first) for a stable multi-select.
    all_hosts = sorted(
        host_series.keys(),
        key=lambda h: (
            -host_hits.get(h, 0),
            sum(host_series[h].values()) / max(1, len(host_series[h])),
        ),
    )
    all_competitors = all_hosts[: max(top, 15)]
    # Default view keeps only competitors that RECUR across fetches (>= min_hits
    # periods), so one-off hosts from a single SERP don't clutter the chart. When
    # the user explicitly picks competitors (comp_filter) we honour them as-is;
    # when there's barely any history (<2 data periods) we don't filter at all.
    data_periods = set(you_map.keys())
    for h in host_series:
        data_periods |= set(host_series[h].keys())
    enough_history = len(data_periods) >= 2
    plotted = [h for h in all_hosts if (comp_filter is None or h in comp_filter)]
    if comp_filter is None and enough_history:
        plotted = [h for h in plotted if host_hits.get(h, 0) >= min_hits]
    plotted = plotted[:top]

    # Continuous period axis across the FULL selected range so the graph spans
    # every weekly/monthly tick; periods with no data become gaps.
    def _period_start(d: date) -> date:
        if bucket == "month":
            return d.replace(day=1)
        if bucket == "week":
            return d - timedelta(days=d.weekday())
        return d

    def _next_period(p: date) -> date:
        if bucket == "month":
            return (p.replace(day=28) + timedelta(days=4)).replace(day=1)
        if bucket == "week":
            return p + timedelta(days=7)
        return p + timedelta(days=1)

    dates: list[date] = []
    p = _period_start(start_d)
    last = _period_start(end_d)
    while p <= last:
        dates.append(p)
        p = _next_period(p)
    you_points = [
        {"date": d.isoformat(), "position": you_map.get(d), "url": None} for d in dates
    ]
    competitor_series = [
        {
            "host": h,
            "points": [
                {"date": d.isoformat(), "position": host_series[h].get(d), "url": None}
                for d in dates
            ],
        }
        for h in plotted
    ]
    return {
        "label": kw_label,
        "you_host": own_host,
        "freq": freq,
        "dates": [d.isoformat() for d in dates],
        "you": {"host": own_host, "points": you_points},
        "competitors": competitor_series,
        "all_competitors": all_competitors,
    }


def keyword_reporting(
    db: Session,
    domain: Domain,
    *,
    freq: str = "monthly",
    start: date | None = None,
    end: date | None = None,
    env: str | None = None,
    device: str | None = None,
    location_code: int | None = None,
) -> dict:
    """BrightEdge-style "Total Ranked Keywords" report: per reporting period, how
    many of the domain's tracked keywords rank on Page 1 (1-10), Page 2 (11-20)
    and Page 3 (21-30).

    Derived entirely from the stored depth-100 SERP snapshots — our position for a
    keyword in a period is min(rank_group) among that keyword's latest snapshot's
    organic items whose host == our host (one position per keyword; multiple
    ranking URLs collapse to the best). A keyword with a snapshot in the period but
    no own-host item = fetched-but-not-found (`not_ranked`, not in the stack); no
    snapshot = not fetched (absent). Positions 31-100 are "ranked beyond page 3"
    and excluded from the three bands. Continuous axis (missing periods → 0 bars).
    """
    if env is None:
        env = data_env.env_for_domain(db, domain)
    own_host = _norm_host(domain.domain)
    bucket = {"daily": "day", "weekly": "week", "monthly": "month"}.get(freq, "month")

    kids = [
        str(t.keyword_id)
        for t in db.execute(
            select(TrackedKeyword).where(
                TrackedKeyword.domain_id == domain.id,
                TrackedKeyword.keyword_id.isnot(None),
            )
        ).scalars()
        if t.keyword_id is not None
    ]

    end_d = end or datetime.now(timezone.utc).date()
    start_d = start or (end_d - timedelta(days=90))
    if (end_d - start_d).days > 366:  # 12-month cap
        start_d = end_d - timedelta(days=366)

    def _pstart(d: date) -> date:
        if bucket == "month":
            return d.replace(day=1)
        if bucket == "week":
            return d - timedelta(days=d.weekday())
        return d

    def _pnext(p: date) -> date:
        if bucket == "month":
            return (p.replace(day=28) + timedelta(days=4)).replace(day=1)
        if bucket == "week":
            return p + timedelta(days=7)
        return p + timedelta(days=1)

    dates: list[date] = []
    p = _pstart(start_d)
    last = _pstart(end_d)
    while p <= last:
        dates.append(p)
        p = _pnext(p)

    by_period: dict[date, dict] = {}
    if kids:
        params: dict = {"env": env, "start": start_d, "end": end_d,
                        "bucket": bucket, "kids": kids, "ourhost": own_host}
        extra = ""
        if device:
            extra += " AND s.device = :device"
            params["device"] = device
        if location_code is not None:
            extra += " AND s.location_code = :loc"
            params["loc"] = location_code
        sql = text(
            f"""
            WITH latest AS (
                SELECT s.keyword_id AS kid,
                       date_trunc(:bucket, s.captured_at)::date AS period,
                       max(s.captured_at) AS cap
                FROM serp_snapshots s
                WHERE s.keyword_id IN :kids AND s.environment = :env
                  AND s.captured_at::date BETWEEN :start AND :end {extra}
                GROUP BY s.keyword_id, date_trunc(:bucket, s.captured_at)
            ),
            ourpos AS (
                SELECT l.kid, l.period,
                       min(coalesce(i.rank_group, i.rank_absolute)) AS pos
                FROM latest l
                JOIN serp_snapshots s
                  ON s.keyword_id = l.kid AND s.captured_at = l.cap AND s.environment = :env
                JOIN serp_snapshot_items i
                  ON i.snapshot_id = s.id AND i.type = 'organic'
                 AND regexp_replace(lower(i.domain), '^www\\.', '') = :ourhost
                GROUP BY l.kid, l.period
            )
            SELECT l.period AS period, l.kid AS kid, o.pos AS pos
            FROM latest l
            LEFT JOIN ourpos o ON o.kid = l.kid AND o.period = l.period
            """
        ).bindparams(bindparam("kids", expanding=True))
        for r in db.execute(sql, params).mappings():
            d = by_period.setdefault(
                r["period"], {"page1": 0, "page2": 0, "page3": 0, "page4plus": 0, "not_ranked": 0}
            )
            pos = r["pos"]
            if pos is None:
                d["not_ranked"] += 1
            elif 1 <= pos <= 10:
                d["page1"] += 1
            elif 11 <= pos <= 20:
                d["page2"] += 1
            elif 21 <= pos <= 30:
                d["page3"] += 1
            elif 31 <= pos <= 100:
                d["page4plus"] += 1  # ranked beyond page 3 but present in the top 100

    periods = []
    for d in dates:
        v = by_period.get(d, {"page1": 0, "page2": 0, "page3": 0, "page4plus": 0, "not_ranked": 0})
        periods.append({
            "date": d.isoformat(),
            "page1": v["page1"], "page2": v["page2"], "page3": v["page3"],
            "page4plus": v["page4plus"],
            "total": v["page1"] + v["page2"] + v["page3"] + v["page4plus"],
            "not_ranked": v["not_ranked"],
        })
    return {"freq": freq, "dates": [d.isoformat() for d in dates], "periods": periods}


def reporting_facets(
    db: Session,
    domain: Domain,
    *,
    env: str | None = None,
) -> dict:
    """Distinct device + location values present in this domain's stored SERP
    snapshots. Used to drive the Keyword Reporting filters — a selector is only
    rendered when more than one value exists (so single-value controls stay
    hidden today, and auto-appear once Iteration 5 captures more)."""
    if env is None:
        env = data_env.env_for_domain(db, domain)

    kids = [
        t.keyword_id
        for t in db.execute(
            select(TrackedKeyword).where(
                TrackedKeyword.domain_id == domain.id,
                TrackedKeyword.keyword_id.isnot(None),
            )
        ).scalars()
        if t.keyword_id is not None
    ]
    if not kids:
        return {"devices": [], "locations": []}

    sql = text(
        """
        SELECT DISTINCT s.device AS device, s.location_code AS loc
        FROM serp_snapshots s
        WHERE s.keyword_id IN :kids AND s.environment = :env
        """
    ).bindparams(bindparam("kids", expanding=True))
    rows = list(db.execute(sql, {"kids": [str(k) for k in kids], "env": env}).mappings())

    devices = sorted({r["device"] for r in rows if r["device"]})
    codes = sorted({r["loc"] for r in rows if r["loc"] is not None})

    from app.services.domain_analytics_service import GEO_TARGETS

    # location_code (int) → human name, reversed from the canonical geo map.
    code_to_name = {code: name for _cc, (code, name) in GEO_TARGETS.items()}
    locations = [
        {"code": code, "name": code_to_name.get(code, f"Location {code}")}
        for code in codes
    ]
    return {"devices": devices, "locations": locations}


def update_tracking(
    db: Session,
    kw: TrackedKeyword,
    *,
    frequency_days: int | None = None,
    enabled: bool | None = None,
) -> TrackedKeyword:
    """Apply a per-keyword tracking change and keep ``next_fetch_at`` consistent so
    the scheduler follows the change immediately (not only after the next fetch).

    - Frequency change → re-anchor next_fetch_at to (last_fetched_at or now) +
      frequency, never scheduled in the past.
    - Resume (enabled False→True) → if the keyword is overdue (or never scheduled),
      make it due now so it's picked up on the next run.
    - Pause (True→False) → leave next_fetch_at and all history untouched; the
      due-query already excludes disabled keywords.
    """
    now = datetime.now(timezone.utc)
    if frequency_days is not None:
        kw.tracking_frequency_days = frequency_days
        base = kw.last_fetched_at or now
        nxt = base + timedelta(days=frequency_days)
        kw.next_fetch_at = nxt if nxt > now else now
    if enabled is not None:
        kw.tracking_enabled = enabled
        if enabled and (kw.next_fetch_at is None or kw.next_fetch_at <= now):
            kw.next_fetch_at = now
    db.commit()
    db.refresh(kw)
    return kw


def run_due(
    db: Session,
    *,
    limit: int | None = None,
    include_live: bool | None = None,
    domain: Domain | None = None,
) -> dict:
    """Fetch every tracked keyword whose next_fetch_at is due.

    Sandbox keywords are always eligible (free). Live keywords are processed only
    when ``include_live`` is true (defaults to settings.RANK_TRACKING_LIVE_ENABLED).
    Optionally scope to a single ``domain`` (the per-domain "run due" endpoint).
    """
    if limit is None:
        limit = settings.RANK_TRACKING_MAX_PER_RUN
    if include_live is None:
        include_live = settings.RANK_TRACKING_LIVE_ENABLED

    stmt = (
        select(TrackedKeyword)
        .where(
            TrackedKeyword.tracking_enabled.is_(True),
            TrackedKeyword.next_fetch_at.isnot(None),
            TrackedKeyword.next_fetch_at <= func.now(),
            TrackedKeyword.tracking_status != "processing",
        )
        .order_by(TrackedKeyword.next_fetch_at.asc())
        .limit(limit)
    )
    if domain is not None:
        stmt = stmt.where(TrackedKeyword.domain_id == domain.id)
    due = list(db.execute(stmt).scalars())

    fetched = failed = skipped_live = 0
    aborted: str | None = None
    domain_cache: dict = {}

    for kw in due:
        dom = domain if domain is not None else domain_cache.get(kw.domain_id)
        if dom is None:
            dom = db.get(Domain, kw.domain_id)
            domain_cache[kw.domain_id] = dom
        if dom is None:
            continue
        env = data_env.env_for_domain(db, dom)
        if env == data_env.LIVE and not include_live:
            skipped_live += 1
            continue
        if env == data_env.LIVE and not settings.DATAFORSEO_LIVE_ENABLED:
            skipped_live += 1
            continue
        try:
            fetch_keyword_serp(db, dom, kw, env=env)
            fetched += 1
        except (ScrapeError, ScrapeBlockedError) as exc:
            failed += 1
            # A provider auth/balance error hits every call — stop rather than
            # burn attempts (and money). Matches serp_sync's abort behaviour.
            aborted = f"{type(exc).__name__}: {exc}"
            logger.warning("rank_tracking run aborting after provider error: %s", exc)
            break
        except Exception:  # noqa: BLE001 - one bad keyword must not kill the run
            db.rollback()
            failed += 1
            logger.exception("rank_tracking fetch crashed for %r", kw.term)

    return {
        "due": len(due),
        "fetched": fetched,
        "failed": failed,
        "skipped_live": skipped_live,
        "aborted": aborted,
    }
