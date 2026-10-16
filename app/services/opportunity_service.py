"""Opportunity Research service — aggregates + queries over the opportunity_*
tables. All read-only and domain-scoped; structured so the seeded data can later
be swapped for GSC/Ads/DataForSEO ingestion without changing these signatures.
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword, OpportunityPage, OpportunityTrend
from app.services import data_env, dataforseo_client, domain_analytics_service

# Big generic properties that appear in almost every SERP — they are noise, not
# SEO competitors. Excluded from competitor analysis (compared www-stripped).
_GENERIC_DOMAINS = frozenset({
    "youtube.com", "m.youtube.com", "wikipedia.org", "en.wikipedia.org",
    "quora.com", "reddit.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "linkedin.com", "pinterest.com", "medium.com",
    "amazon.in", "amazon.com", "flipkart.com", "google.com", "play.google.com",
    "apps.apple.com", "books.google.com", "scholar.google.com", "t.me",
})


def _norm_host(host: str) -> str:
    """Registrable-ish host: lowercased, scheme/path stripped, leading www removed."""
    h = re.sub(r"^https?://", "", (host or "").strip().lower()).split("/")[0]
    return h[4:] if h.startswith("www.") else h

_SORT_COLUMNS = {
    "keyword": OpportunityKeyword.keyword,
    "volume": OpportunityKeyword.search_volume,
    "yoy": OpportunityKeyword.yoy_change,
    "kd": OpportunityKeyword.keyword_difficulty,
    "score": OpportunityKeyword.opportunity_score,
    "position": OpportunityKeyword.current_position,
}


def _compact(n: float) -> str:
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{round(n)}"


def _pct(now: float, prev: float) -> float:
    if not prev:
        return 0.0
    return round((now - prev) / prev * 100, 1)


def _ctr_from_position(pos: float | None) -> float:
    """Rough organic CTR curve from average position (data-derived, deterministic)."""
    if not pos or pos <= 0:
        return 0.0
    return round(min(35.0, 30.0 / (pos ** 0.8)), 1)


def _month_label(d: date) -> str:
    return d.strftime("%b '%y")


def overview(db: Session, domain: Domain) -> dict:
    """Clear, real numbers only: how many keywords we track, how many rank, how
    many rank well, and the average position. No modeled aggregates (e.g. summed
    search volume) or formula-based deltas — every tile is a plain count/number
    a user can verify."""
    total_keywords = db.execute(
        select(func.count()).select_from(OpportunityKeyword)
        .where(OpportunityKeyword.domain_id == domain.id)
    ).scalar_one()
    avg_position = db.execute(
        select(func.avg(OpportunityKeyword.current_position))
        .where(OpportunityKeyword.domain_id == domain.id,
               OpportunityKeyword.current_position.isnot(None))
    ).scalar_one()

    # Real traffic delta only when we have two trend points from Search Console.
    trend = db.execute(
        select(OpportunityTrend)
        .where(OpportunityTrend.domain_id == domain.id)
        .order_by(OpportunityTrend.captured_on)
    ).scalars().all()
    latest = trend[-1] if trend else None
    prev = trend[-2] if len(trend) >= 2 else None
    organic = latest.organic_traffic if latest else 0
    organic_delta = round(_pct(latest.organic_traffic, prev.organic_traffic), 1) if prev else 0

    # Rank distribution (real, from our positions on the opportunity set).
    dist_rows = db.execute(
        select(OpportunityKeyword.current_position)
        .where(OpportunityKeyword.domain_id == domain.id,
               OpportunityKeyword.current_position.isnot(None))
    ).scalars().all()
    buckets = {"1-3": 0, "4-10": 0, "11-20": 0, "21+": 0}
    for p in dist_rows:
        if p <= 3:
            buckets["1-3"] += 1
        elif p <= 10:
            buckets["4-10"] += 1
        elif p <= 20:
            buckets["11-20"] += 1
        else:
            buckets["21+"] += 1
    rank_distribution = [{"label": k, "count": v} for k, v in buckets.items()]

    ranking_count = len(dist_rows)
    top3 = buckets["1-3"]
    top10 = buckets["1-3"] + buckets["4-10"]

    # delta 0 = "no change to show" (the frontend hides the arrow then).
    kpis = [
        {"label": "Keywords tracked", "value": _compact(total_keywords), "delta": 0, "up": True},
        {"label": "Ranking in Google", "value": _compact(ranking_count), "delta": 0, "up": True},
        {"label": "In top 10", "value": _compact(top10), "delta": 0, "up": True},
        {"label": "In top 3", "value": _compact(top3), "delta": 0, "up": True},
        {"label": "Avg. position", "value": (f"#{round(float(avg_position), 1)}" if avg_position else "—"), "delta": 0, "up": True},
    ]
    if organic:  # only when Search Console gives a real organic-traffic number
        kpis.insert(1, {"label": "Organic traffic / mo", "value": _compact(organic),
                        "delta": organic_delta, "up": organic_delta >= 0})

    return {"domain": domain.domain, "kpis": kpis, "rank_distribution": rank_distribution}


def trend(db: Session, domain: Domain) -> dict:
    rows = db.execute(
        select(OpportunityTrend)
        .where(OpportunityTrend.domain_id == domain.id)
        .order_by(OpportunityTrend.captured_on)
    ).scalars().all()
    return {
        "domain": domain.domain,
        "points": [
            {
                "label": _month_label(r.captured_on),
                "captured_on": r.captured_on,
                "organic_traffic": r.organic_traffic,
                "ranking_keywords": r.ranking_keywords,
                "serp_opportunities": r.serp_opportunities,
            }
            for r in rows
        ],
    }


def keywords(
    db: Session, domain: Domain, *, q: str = "", intent: str = "all",
    min_volume: int = 0, sort: str = "score", direction: str = "desc",
    page: int = 1, page_size: int = 8,
) -> dict:
    conds = [OpportunityKeyword.domain_id == domain.id]
    if q.strip():
        conds.append(OpportunityKeyword.keyword.ilike(f"%{q.strip()}%"))
    if intent and intent != "all":
        conds.append(OpportunityKeyword.intent == intent)
    if min_volume > 0:
        conds.append(OpportunityKeyword.search_volume >= min_volume)

    total = db.execute(
        select(func.count()).select_from(OpportunityKeyword).where(*conds)
    ).scalar_one()

    col = _SORT_COLUMNS.get(sort, OpportunityKeyword.opportunity_score)
    order = col.asc() if direction == "asc" else col.desc()
    page = max(1, page)
    page_size = max(1, min(page_size, 100))

    rows = db.execute(
        select(OpportunityKeyword, OpportunityPage.url)
        .join(OpportunityPage, OpportunityPage.id == OpportunityKeyword.page_id, isouter=True)
        .where(*conds)
        .order_by(order, OpportunityKeyword.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    out = []
    for kw, page_url in rows:
        comps = kw.competitors or []
        out.append({
            "id": str(kw.id),
            "keyword": kw.keyword,
            "search_volume": kw.search_volume,
            "yoy_change": round(kw.yoy_change, 1),
            "keyword_difficulty": kw.keyword_difficulty,
            "current_position": kw.current_position,
            "intent": kw.intent,
            "opportunity_score": kw.opportunity_score,
            "ranking_url": kw.ranking_url,
            "page_url": page_url,
            "serp_features": kw.serp_features or [],
            "competitors": [
                {"domain": c.get("domain", ""), "position": int(c.get("position", 0))}
                for c in comps if isinstance(c, dict)
            ],
        })
    return {
        "domain": domain.domain, "total": total, "page": page,
        "page_size": page_size, "rows": out,
    }


def pages(db: Session, domain: Domain) -> dict:
    page_rows = db.execute(
        select(OpportunityPage)
        .where(OpportunityPage.domain_id == domain.id)
        .order_by(OpportunityPage.created_at)
    ).scalars().all()

    out = []
    for p in page_rows:
        kws = db.execute(
            select(OpportunityKeyword)
            .where(OpportunityKeyword.page_id == p.id)
            .order_by(OpportunityKeyword.opportunity_score.desc())
        ).scalars().all()
        positions = [k.current_position for k in kws if k.current_position]
        out.append({
            "id": str(p.id),
            "url": p.url,
            "title": p.title,
            "keyword_count": len(kws),
            "total_volume": sum(k.search_volume for k in kws),
            "avg_position": round(sum(positions) / len(positions), 1) if positions else None,
            "keywords": [
                {
                    "keyword": k.keyword,
                    "search_volume": k.search_volume,
                    "current_position": k.current_position,
                    "intent": k.intent,
                    "opportunity_score": k.opportunity_score,
                    "serp_features": k.serp_features or [],
                }
                for k in kws
            ],
        })
    return {"domain": domain.domain, "total": len(out), "rows": out}


def change(db: Session, domain: Domain) -> dict:
    """Real month-over-month SEARCH-VOLUME change for the domain's keyword universe.

    Computed from `keyword_metrics` (DataForSEO Google Ads monthly history). This
    is genuine first-party-of-record volume — no seeded numbers. The rank-vs-demand
    *contribution* split and rank-based gainers/losers need SERP history / GSC and
    are omitted (returned empty) until that data exists, rather than invented.
    """
    series_rows = db.execute(text(
        """
        WITH kids AS (
          SELECT DISTINCT keyword_id FROM opportunity_keywords
          WHERE domain_id = :did AND keyword_id IS NOT NULL
        )
        SELECT km.metric_date AS md, SUM(km.search_volume)::bigint AS vol
        FROM keyword_metrics km
        WHERE km.keyword_id IN (SELECT keyword_id FROM kids)
          AND EXTRACT(day FROM km.metric_date) = 1
        GROUP BY km.metric_date ORDER BY km.metric_date
        """
    ), {"did": str(domain.id)}).all()

    series = [{"month": _month_label(r.md), "volume": int(r.vol or 0)} for r in series_rows]
    curr_dt = series_rows[-1].md if series_rows else None
    prev_dt = series_rows[-2].md if len(series_rows) >= 2 else None
    curr_v = int(series_rows[-1].vol or 0) if series_rows else 0
    prev_v = int(series_rows[-2].vol or 0) if len(series_rows) >= 2 else 0
    delta = curr_v - prev_v

    gainers: list[dict] = []
    losers: list[dict] = []
    if curr_dt and prev_dt:
        movers = db.execute(text(
            """
            WITH kids AS (
              SELECT DISTINCT keyword_id FROM opportunity_keywords
              WHERE domain_id = :did AND keyword_id IS NOT NULL
            )
            SELECT k.keyword,
                   COALESCE(MAX(CASE WHEN km.metric_date = :prev_d THEN km.search_volume END), 0) AS prev_v,
                   COALESCE(MAX(CASE WHEN km.metric_date = :curr_d THEN km.search_volume END), 0) AS curr_v
            FROM keyword_metrics km
            JOIN keywords k ON k.id = km.keyword_id
            WHERE km.keyword_id IN (SELECT keyword_id FROM kids)
              AND km.metric_date IN (:prev_d, :curr_d)
            GROUP BY k.keyword
            """
        ), {"did": str(domain.id), "prev_d": prev_dt, "curr_d": curr_dt}).all()
        moved = [
            {"keyword": m.keyword, "prev": int(m.prev_v), "curr": int(m.curr_v),
             "delta": int(m.curr_v) - int(m.prev_v)}
            for m in movers if int(m.curr_v) != int(m.prev_v)
        ]
        moved.sort(key=lambda x: x["delta"], reverse=True)
        gainers = [m for m in moved if m["delta"] > 0][:5]
        losers = [m for m in reversed(moved) if m["delta"] < 0][:5]

    return {
        "domain": domain.domain,
        "series": series,
        "prev_label": series[-2]["month"] if len(series) >= 2 else "",
        "curr_label": series[-1]["month"] if series else "",
        "prev_volume": prev_v,
        "curr_volume": curr_v,
        "delta": delta,
        "delta_pct": _pct(curr_v, prev_v),
        "gainers": gainers,
        "losers": losers,
    }


def curated_competitors(db: Session, domain: Domain) -> list[str]:
    """The user's own competitor list for the domain (Competitor Analysis page,
    `tracked_competitors`), normalized; our own host excluded."""
    host = _norm_host(domain.domain)
    hosts = db.execute(text(
        "SELECT competitor FROM tracked_competitors WHERE domain_id = :d ORDER BY created_at"
    ), {"d": str(domain.id)}).scalars().all()
    return [h for h in dict.fromkeys(_norm_host(h) for h in hosts) if h and h != host]


def strategy(db: Session, domain: Domain, scope: str = "auto") -> dict:
    """Competitive Strategy — REAL competitor footprint on the domain's tracked
    keyword universe, from the latest SERP snapshot per keyword. Shows who ranks
    for our keywords, how many they share, and their average position.

    Which competitors: `scope`
      auto    — the user's curated list (tracked_competitors) when it has any,
                else the top domains found in our SERPs;
      curated — the curated list only (zero-overlap competitors included);
      serp    — the top 12 domains in our SERPs (discovery view).

    Whole-site metrics (total organic traffic, ranking-keyword counts, traffic
    share) live in `competitor_analytics`, fed by DataForSEO Labs.
    """
    curated = curated_competitors(db, domain)
    use_curated = scope in ("auto", "curated") and bool(curated)
    total_keywords = db.execute(text(
        "SELECT COUNT(DISTINCT keyword_id) FROM opportunity_keywords "
        "WHERE domain_id = :did AND keyword_id IS NOT NULL"
    ), {"did": str(domain.id)}).scalar_one()

    our_ranked = db.execute(text(
        "SELECT COUNT(*) FROM opportunity_keywords "
        "WHERE domain_id = :did AND current_position IS NOT NULL"
    ), {"did": str(domain.id)}).scalar_one()

    # Normalize each result host (strip leading www) so a domain isn't split into
    # www / non-www rows, exclude our own domain and the generic giants.
    stmt = text(
        """
        WITH kids AS (
          SELECT DISTINCT keyword_id FROM opportunity_keywords
          WHERE domain_id = :did AND keyword_id IS NOT NULL
        ),
        latest AS (
          SELECT DISTINCT ON (keyword_id) id, keyword_id
          FROM serp_snapshots
          WHERE keyword_id IN (SELECT keyword_id FROM kids)
            AND environment = :env
          ORDER BY keyword_id, captured_at DESC
        ),
        items AS (
          SELECT regexp_replace(lower(i.domain), '^www\\.', '') AS domain,
                 l.keyword_id, i.rank_absolute
          FROM latest l
          JOIN serp_snapshot_items i ON i.snapshot_id = l.id
          WHERE i.domain <> ''
        ),
        ours AS (
          -- We rank: in the SERP snapshot, or GSC has a position (often deeper
          -- than the snapshot depth).
          SELECT keyword_id FROM items WHERE domain = :host
          UNION
          SELECT keyword_id FROM opportunity_keywords
          WHERE domain_id = :did AND keyword_id IS NOT NULL
            AND current_position IS NOT NULL
        )
        SELECT domain,
               COUNT(DISTINCT keyword_id) AS shared,
               COUNT(DISTINCT keyword_id)
                 FILTER (WHERE keyword_id IN (SELECT keyword_id FROM ours)) AS common,
               ROUND(AVG(rank_absolute), 1) AS avg_pos,
               MIN(rank_absolute) AS best_pos
        FROM items
        WHERE domain <> :host AND domain NOT IN :generics
        """ + ("AND domain IN :only" if use_curated else "") + """
        GROUP BY domain
        ORDER BY shared DESC, avg_pos ASC
        """ + ("" if use_curated else "LIMIT 12")
    ).bindparams(bindparam("generics", expanding=True))
    params = {
        "did": str(domain.id),
        "env": data_env.env_for_domain(db, domain),
        "host": _norm_host(domain.domain),
        "generics": list(_GENERIC_DOMAINS),
    }
    if use_curated:
        stmt = stmt.bindparams(bindparam("only", expanding=True))
        params["only"] = curated
    rows = db.execute(stmt, params).all()

    competitors = [
        {"domain": r.domain, "keywords_shared": int(r.shared),
         # common = we both rank; gap = they rank, we don't (tracked keywords).
         "common_keywords": int(r.common), "gap_keywords": int(r.shared) - int(r.common),
         "avg_position": float(r.avg_pos), "best_position": int(r.best_pos),
         "curated": use_curated or r.domain in curated}
        for r in rows
    ]
    if use_curated:
        # Curated competitors not (yet) seen in our SERPs still belong in the list.
        seen = {c["domain"] for c in competitors}
        competitors += [
            {"domain": h, "keywords_shared": 0, "common_keywords": 0, "gap_keywords": 0,
             "avg_position": None, "best_position": None, "curated": True}
            for h in curated if h not in seen
        ]
    return {
        "domain": domain.domain,
        "total_keywords": int(total_keywords),
        "our_ranked": int(our_ranked),
        "scope": "curated" if use_curated else "serp",
        "curated_count": len(curated),
        "competitors": competitors,
    }


def competitor_analytics(
    db: Session, domain: Domain, *, country: str = domain_analytics_service.DEFAULT_COUNTRY,
    language: str = "en",
) -> dict:
    """Whole-site competitor analytics — estimated organic traffic, total ranking
    keywords, top-3/top-10 counts, traffic share and domain rank — for our domain
    plus the competitors found in our tracked-keyword SERPs.

    Read-only: served from `domain_analytics_snapshots` (filled by
    `domain_analytics_service.refresh` from DataForSEO Labs). Domains not fetched
    yet are listed in `missing`; nothing is estimated or invented.
    """
    host = _norm_host(domain.domain)
    footprint = {c["domain"]: c for c in strategy(db, domain)["competitors"]}
    targets = [host, *footprint]
    loc = domain_analytics_service.location_code_for(country)
    env = data_env.env_for_domain(db, domain)
    stored = domain_analytics_service.latest(
        db, targets, location_code=loc, language=language, env=env)

    rows = []
    for t in targets:
        snap = stored.get(t)
        if snap is None:
            continue
        shared = footprint.get(t)
        rows.append({
            "domain": t,
            "is_you": t == host,
            "organic_traffic": round(snap.organic_etv),
            "ranking_keywords": snap.organic_count,
            "top3_keywords": snap.pos_1 + snap.pos_2_3,
            "top10_keywords": snap.pos_1 + snap.pos_2_3 + snap.pos_4_10,
            "top20_keywords": snap.pos_1 + snap.pos_2_3 + snap.pos_4_10 + snap.pos_11_20,
            "new_keywords": snap.is_new,
            "lost_keywords": snap.is_lost,
            "traffic_value": round(snap.traffic_cost),
            "domain_rank": snap.domain_rank,
            "backlinks": snap.backlinks,
            "referring_domains": snap.referring_domains,
            "keywords_shared": shared["keywords_shared"] if shared else None,
            "common_keywords": shared["common_keywords"] if shared else None,
            "gap_keywords": shared["gap_keywords"] if shared else None,
            "traffic_share": 0.0,
            "fetched_at": snap.fetched_at,
        })

    total_traffic = sum(r["organic_traffic"] for r in rows)
    for r in rows:
        r["traffic_share"] = (
            round(r["organic_traffic"] / total_traffic * 100, 1) if total_traffic else 0.0)
    rows.sort(key=lambda r: r["organic_traffic"], reverse=True)

    return {
        "domain": domain.domain,
        "is_sample": env == data_env.SANDBOX,
        "environment": env,
        "configured": dataforseo_client.is_configured(),
        "country": country,
        "source": "DataForSEO Labs (domain_rank_overview) + Backlinks (bulk_ranks, bulk_backlinks)",
        "note": "Whole-site estimates from DataForSEO, stored in your database. "
                "Traffic share is relative to the domains listed.",
        "fetched_at": max((r["fetched_at"] for r in rows), default=None),
        "missing": [t for t in targets if t not in stored],
        "rows": rows,
    }


def refresh_competitor_analytics(
    db: Session, domain: Domain, *, country: str = domain_analytics_service.DEFAULT_COUNTRY,
    language: str = "en", force: bool = False,
) -> dict:
    """Fetch stale/missing whole-site metrics from DataForSEO, store them, and
    return the fresh `competitor_analytics` payload plus a refresh summary."""
    host = _norm_host(domain.domain)
    targets = [host, *(c["domain"] for c in strategy(db, domain)["competitors"])]
    summary = domain_analytics_service.refresh(
        db, targets, location_code=domain_analytics_service.location_code_for(country),
        language=language, force=force, env=data_env.env_for_domain(db, domain),
    )
    return {**competitor_analytics(db, domain, country=country, language=language),
            "refresh": summary}


def warm_competitor_backlinks(
    db: Session, domain: Domain, *, country: str = domain_analytics_service.DEFAULT_COUNTRY,
    language: str = "en", force: bool = False,
) -> dict:
    """Cheap backlinks-only warm-up for OUR domain + its competitors.

    Fills the Authority / Backlinks / Referring-domains columns used by the
    Keyword Breakdown, without buying the whole-site Labs traffic overview.
    Returns {fetched, cached, failed, error}.
    """
    host = _norm_host(domain.domain)
    targets = [host, *(c["domain"] for c in strategy(db, domain)["competitors"])]
    return domain_analytics_service.warm_backlinks(
        db, targets, location_code=domain_analytics_service.location_code_for(country),
        language=language, force=force, env=data_env.env_for_domain(db, domain),
    )


def competitor_keywords(
    db: Session, domain: Domain, competitor: str, *, kind: str = "all",
) -> dict:
    """The keywords a competitor ranks for (among our tracked keywords) vs ours —
    Ubersuggest-style detail: their ranking URL, position, volume, estimated
    visits, CPC, paid difficulty and SEO difficulty.

    Real data only, from what's already stored: the latest SERP snapshot per
    keyword (positions + URLs), opportunity_keywords (volume, SEO difficulty,
    intent) and the newest Google Ads metric (CPC, competition index = paid
    difficulty). `our_position` null = a keyword gap (they rank, we don't).
    kind: 'all' | 'common' (we both rank) | 'gap'. Est. visits = volume × CTR at
    their position (same curve as the overview CTR)."""
    host = _norm_host(domain.domain)
    comp = _norm_host(competitor)

    rows = db.execute(text(
        r"""
        WITH kids AS (
          SELECT DISTINCT keyword_id FROM opportunity_keywords
          WHERE domain_id = :did AND keyword_id IS NOT NULL
        ),
        latest AS (
          SELECT DISTINCT ON (keyword_id) id, keyword_id
          FROM serp_snapshots
          WHERE keyword_id IN (SELECT keyword_id FROM kids)
            AND environment = :env
          ORDER BY keyword_id, captured_at DESC
        ),
        items AS (
          SELECT l.keyword_id,
                 regexp_replace(lower(i.domain), '^www\.', '') AS dom,
                 i.rank_absolute, i.url
          FROM latest l
          JOIN serp_snapshot_items i ON i.snapshot_id = l.id
        ),
        agg AS (
          SELECT keyword_id,
                 MIN(CASE WHEN dom = :comp THEN rank_absolute END) AS their_pos,
                 MIN(CASE WHEN dom = :host THEN rank_absolute END) AS our_pos
          FROM items
          GROUP BY keyword_id
          HAVING MIN(CASE WHEN dom = :comp THEN rank_absolute END) IS NOT NULL
        )
        SELECT k.keyword AS keyword, a.their_pos, a.our_pos,
               (SELECT x.url FROM items x WHERE x.keyword_id = a.keyword_id
                  AND x.dom = :comp ORDER BY x.rank_absolute LIMIT 1) AS their_url,
               (SELECT x.url FROM items x WHERE x.keyword_id = a.keyword_id
                  AND x.dom = :host ORDER BY x.rank_absolute LIMIT 1) AS our_url,
               ok.current_position AS gsc_pos,
               ok.search_volume AS vol, ok.keyword_difficulty AS kd, ok.intent,
               COALESCE(ok.cpc, km.cpc) AS cpc, km.competition AS paid_difficulty
        FROM agg a
        JOIN keywords k ON k.id = a.keyword_id
        LEFT JOIN LATERAL (
          SELECT search_volume, keyword_difficulty, intent, cpc, current_position
          FROM opportunity_keywords
          WHERE keyword_id = a.keyword_id AND domain_id = :did
          LIMIT 1
        ) ok ON true
        LEFT JOIN LATERAL (
          SELECT cpc, competition FROM keyword_metrics
          WHERE keyword_id = a.keyword_id
            AND (cpc IS NOT NULL OR competition IS NOT NULL)
          ORDER BY metric_date DESC, created_at DESC
          LIMIT 1
        ) km ON true
        ORDER BY a.their_pos ASC, ok.search_volume DESC NULLS LAST
        """
    ), {"did": str(domain.id), "host": host, "comp": comp,
        "env": data_env.env_for_domain(db, domain)}).all()

    out = []
    gaps = 0
    for r in rows:
        # Our position: the SERP snapshot, else GSC (usually deeper than depth 20).
        our_pos = r.our_pos if r.our_pos is not None else r.gsc_pos
        is_gap = our_pos is None
        gaps += 1 if is_gap else 0
        if (kind == "gap" and not is_gap) or (kind == "common" and is_gap):
            continue
        vol = int(r.vol) if r.vol is not None else None
        their_pos = int(r.their_pos)
        out.append({
            "keyword": r.keyword,
            "their_position": their_pos,
            "our_position": int(our_pos) if our_pos is not None else None,
            "our_position_source": ("serp" if r.our_pos is not None
                                    else "gsc" if r.gsc_pos is not None else None),
            "search_volume": vol,
            "is_gap": is_gap,
            "their_url": r.their_url,
            "our_url": r.our_url,
            "est_visits": (round(vol * _ctr_from_position(their_pos) / 100, 1)
                           if vol is not None else None),
            "cpc": float(r.cpc) if r.cpc is not None else None,
            "paid_difficulty": int(r.paid_difficulty) if r.paid_difficulty is not None else None,
            "seo_difficulty": int(r.kd) if r.kd else None,
            "intent": r.intent,
        })
    return {
        "domain": domain.domain, "competitor": comp,
        "total": len(rows), "gaps": gaps, "common": len(rows) - gaps,
        "kind": kind, "rows": out,
    }
