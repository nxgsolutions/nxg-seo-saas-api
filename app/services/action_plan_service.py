"""Opportunity Research "Action Plan" — what to do next, from data already stored.

Three prioritized lists (DB only — no paid API calls):

  improve  — keywords we already rank #4-50 for (Search Console position, or our
             stored DataForSEO ranked keywords, which also give the page URL).
             Gain = extra visits if the keyword reached #3.
  gaps     — keywords our competitors rank top-10 for and we don't: their stored
             whole-site ranked keywords + SERP snapshots of our tracked keywords.
             Gain = visits at #3.
  targets  — keyword-research ideas in the sweet spot (difficulty <= 30,
             volume >= 100) that aren't tracked yet. Gain = visits at #3.

Competitor-brand and "near me" keywords are skipped (keyword_relevance).
`top` merges all three, ranked by gain × ease (quick wins weighted up).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.services import (
    data_env, domain_analytics_service, domain_settings, keyword_relevance,
    opportunity_service, ranked_keywords_service,
)

TARGET_POSITION = 3
IMPROVE_MIN, IMPROVE_MAX = 4, 50
GAP_MAX_COMPETITOR_POSITION = 10
GAP_MIN_VOLUME = 50
# A SERP-discovered domain must share at least this many keywords to count as a
# real competitor for gap analysis — filters one-off, off-topic domains (e.g. a
# travel site appearing once in a SERP). Curated competitors are always kept.
GAP_MIN_SHARED_KEYWORDS = 3
SWEET_MAX_KD, SWEET_MIN_VOLUME = 30, 100
HARD_KD = 70


def _ctr(position: float | None) -> float:
    """Click-through rate (%) at a position — the same curve used site-wide."""
    return opportunity_service._ctr_from_position(position)


def _variant_key(keyword: str) -> str:
    """'g.k questions' / 'g k questions' / 'gk questions' -> one topic key."""
    return re.sub(r"[^a-z0-9]", "", keyword.lower())


def _on_topic(keyword: str, topic_regex: str | None) -> bool:
    """Whole-site competitor keywords must match the DOMAIN's topic terms
    (competitors also rank for off-topic news). No topic configured
    (topic_regex is None) ⇒ everything is on-topic, so nothing is dropped."""
    if not topic_regex:
        return True
    # also the compact form, so "u.p.s.c mock test" matches "upsc"
    return (re.search(topic_regex, keyword, re.I) is not None
            or re.search(topic_regex, _variant_key(keyword)) is not None)


def _ease(kd: int | float | None) -> float:
    return 0.5 if kd is None else max(0.05, 1 - float(kd) / 100)


def _item(**kw) -> dict:
    base = {"type": None, "keyword": None, "volume": None, "difficulty": None, "position": None,
            "url": None, "competitor": None, "competitor_position": None, "competitor_url": None,
            "competitors_count": None, "gain": 0, "action": None, "reason": None, "source": None,
            "tracked": False}
    base.update(kw)
    return base


def build(db: Session, domain: Domain, *,
          country: str | None = None, language: str | None = None) -> dict:
    # Per-domain market + niche (falls back to workspace default, then in/en).
    s = domain_settings.resolve(domain, getattr(domain, "workspace", None))
    country = country or s.country
    language = language or s.language
    env = data_env.env_for_domain(db, domain)
    loc = domain_analytics_service.location_code_for(country)
    host = ranked_keywords_service.norm_host(domain.domain)

    footprint = opportunity_service.strategy(db, domain)          # curated list if any
    # Keep curated competitors always; keep SERP-discovered ones only when they
    # share enough keywords, so one-off off-topic domains don't become "gaps".
    competitors = [
        c["domain"] for c in footprint["competitors"]
        if c.get("curated") or (c.get("keywords_shared") or 0) >= GAP_MIN_SHARED_KEYWORDS
    ]

    def relevant(keyword: str) -> bool:
        return keyword_relevance.irrelevance_reason(
            keyword, own_host=host, competitor_hosts=competitors,
            brand_terms=s.brand_terms, is_local_ok=s.is_local) is None

    tracked_rows = db.execute(text(
        "SELECT lower(keyword) AS k, keyword, search_volume, keyword_difficulty, current_position "
        "FROM opportunity_keywords WHERE domain_id = :d"), {"d": str(domain.id)}).all()
    tracked = {r.k: r for r in tracked_rows}

    # ── Our rankings: stored ranked keywords (position + URL) + Search Console ─
    our_run = ranked_keywords_service.latest_run(db, host, location_code=loc, language=language, env=env)
    ours: dict[str, dict] = {}
    if our_run is not None:
        for r in ranked_keywords_service.run_rows(db, our_run):
            if r.rank_absolute is not None:
                ours[r.keyword.lower()] = {"keyword": r.keyword, "position": r.rank_absolute, "url": r.url,
                                           "volume": r.search_volume, "kd": r.keyword_difficulty,
                                           "source": "Your rankings (DataForSEO)"}
    for k, r in tracked.items():
        if r.current_position is not None:  # Search Console is first-party: it wins
            prev = ours.get(k, {})
            ours[k] = {"keyword": r.keyword, "position": int(r.current_position), "url": prev.get("url"),
                       "volume": r.search_volume or prev.get("volume"),
                       "kd": r.keyword_difficulty or prev.get("kd"), "source": "Search Console"}

    # ── 1. Improve existing rankings ─────────────────────────────────────────
    improve = []
    for k, o in ours.items():
        pos, vol = o["position"], o["volume"] or 0
        if not (IMPROVE_MIN <= pos <= IMPROVE_MAX) or not relevant(o["keyword"]):
            continue
        gain = round(vol * max(0.0, _ctr(TARGET_POSITION) - _ctr(pos)) / 100)
        improve.append(_item(
            type="improve", keyword=o["keyword"], volume=vol or None, difficulty=o["kd"], position=pos,
            url=o["url"], gain=gain, source=o["source"], tracked=k in tracked,
            action="Optimise this page" if o["url"] else "Improve this ranking",
            reason=(f"You rank #{pos}" + (" — page 1 is within reach" if pos <= 20 else "")
                    + f". Reaching #{TARGET_POSITION} could add ~{gain:,} visits/mo."),
        ))

    # ── 2. Competitor gaps ───────────────────────────────────────────────────
    gaps: dict[str, dict] = {}

    our_variants = {_variant_key(k) for k in ours}
    gap_comps: dict[str, set[str]] = {}

    def add_gap(keyword, vol, kd, comp, cpos, curl, source, *, need_topic: bool):
        if not relevant(keyword) or (vol or 0) < GAP_MIN_VOLUME:
            return
        if need_topic and not _on_topic(keyword, s.topic_regex):
            return
        k = _variant_key(keyword)          # merge spelling variants into one topic
        if k in our_variants:
            return
        is_tracked = keyword.lower() in tracked
        g = gaps.get(k)
        if g is None:
            gaps[k] = _item(type="gap", keyword=keyword, volume=vol, difficulty=kd, competitor=comp,
                            competitor_position=cpos, competitor_url=curl, competitors_count=1,
                            source=source, tracked=is_tracked)
            gap_comps[k] = {comp}
            return
        if (vol or 0) > (g["volume"] or 0):
            # keep the highest-volume variant as the headline keyword
            g.update(keyword=keyword, volume=vol, difficulty=kd)
        g["tracked"] = g["tracked"] or is_tracked
        gap_comps[k].add(comp)                 # distinct competitors, not rows
        g["competitors_count"] = len(gap_comps[k])
        if cpos is not None and (g["competitor_position"] is None or cpos < g["competitor_position"]):
            g.update(competitor=comp, competitor_position=cpos, competitor_url=curl)

    # 2a. their stored whole-site rankings (DataForSEO Labs)
    for comp in competitors:
        run = ranked_keywords_service.latest_run(db, comp, location_code=loc, language=language, env=env)
        if run is None:
            continue
        for r in ranked_keywords_service.run_rows(db, run):
            if r.rank_absolute is not None and r.rank_absolute <= GAP_MAX_COMPETITOR_POSITION:
                add_gap(r.keyword, r.search_volume, r.keyword_difficulty, comp, r.rank_absolute, r.url,
                        "Competitor rankings (DataForSEO)", need_topic=True)

    # 2b. SERP snapshots of our tracked keywords where a competitor is top-10 and we're absent
    if competitors:
        stmt = text(r"""
            WITH kids AS (
              SELECT keyword_id, keyword, search_volume, keyword_difficulty FROM opportunity_keywords
              WHERE domain_id = :d AND keyword_id IS NOT NULL AND current_position IS NULL
            ),
            latest AS (
              SELECT DISTINCT ON (keyword_id) id, keyword_id FROM serp_snapshots
              WHERE keyword_id IN (SELECT keyword_id FROM kids) AND environment = :env
              ORDER BY keyword_id, captured_at DESC
            ),
            items AS (
              SELECT l.keyword_id, regexp_replace(lower(i.domain), '^www\.', '') AS dom,
                     i.rank_absolute AS pos, i.url
              FROM latest l JOIN serp_snapshot_items i ON i.snapshot_id = l.id
            )
            SELECT k.keyword, k.search_volume AS vol, k.keyword_difficulty AS kd, it.dom, it.pos, it.url
            FROM items it JOIN kids k ON k.keyword_id = it.keyword_id
            WHERE it.dom IN :comps AND it.pos <= :maxpos
              AND NOT EXISTS (SELECT 1 FROM items o WHERE o.keyword_id = it.keyword_id AND o.dom = :host)
            ORDER BY it.pos
        """).bindparams(bindparam("comps", expanding=True))
        for r in db.execute(stmt, {"d": str(domain.id), "env": env, "comps": competitors,
                                   "maxpos": GAP_MAX_COMPETITOR_POSITION, "host": host}).all():
            add_gap(r.keyword, r.vol, r.kd, r.dom, r.pos, r.url, "Google results for your tracked keywords",
                    need_topic=False)

    for g in gaps.values():
        vol = g["volume"] or 0
        g["gain"] = round(vol * _ctr(TARGET_POSITION) / 100)
        n = g["competitors_count"]
        g["action"] = "Create a page" if not g["tracked"] else "Create or improve a page"
        g["reason"] = (f"{g['competitor']} ranks #{g['competitor_position']}"
                       + (f" (+{n - 1} more competitor{'s' if n > 2 else ''})" if n > 1 else "")
                       + f" and you don't. #{TARGET_POSITION} could bring ~{g['gain']:,} visits/mo.")

    # ── 3. New targets from keyword research ─────────────────────────────────
    target_rows = db.execute(text("""
        WITH ideas AS (
          SELECT DISTINCT rr.keyword_id FROM keyword_research_results rr
          JOIN keyword_researches r ON r.id = rr.research_id
          WHERE r.workspace_id = :w AND r.kind = 'keyword'
            -- scope to THIS domain's research + domain-less (workspace-wide)
            -- research, so another domain's niche can't leak in as a target.
            AND (r.domain_id = :did OR r.domain_id IS NULL)
        ),
        m AS (
          SELECT DISTINCT ON (km.keyword_id) km.keyword_id, km.search_volume, km.difficulty
          FROM keyword_metrics km
          WHERE km.keyword_id IN (SELECT keyword_id FROM ideas) AND km.search_volume IS NOT NULL
          ORDER BY km.keyword_id, km.metric_date DESC
        )
        SELECT k.keyword, m.search_volume AS vol, m.difficulty AS kd
        FROM m JOIN keywords k ON k.id = m.keyword_id
        WHERE m.search_volume >= :minvol AND m.difficulty IS NOT NULL AND m.difficulty <= :maxkd
    """), {"w": str(domain.workspace_id), "did": str(domain.id),
           "minvol": SWEET_MIN_VOLUME, "maxkd": SWEET_MAX_KD}).all()
    targets = []
    for r in target_rows:
        k = r.keyword.lower()
        if k in tracked or k in ours or _variant_key(r.keyword) in gaps or not relevant(r.keyword):
            continue
        gain = round(int(r.vol) * _ctr(TARGET_POSITION) / 100)
        targets.append(_item(
            type="target", keyword=r.keyword, volume=int(r.vol), difficulty=round(float(r.kd)),
            gain=gain, source="Keyword research", action="Track & create content",
            reason=f"{int(r.vol):,} searches/mo at difficulty {round(float(r.kd))}. #{TARGET_POSITION} could bring ~{gain:,} visits/mo.",
        ))

    # Full lists (no cap): the UI pages them via /action-plan/items, so the
    # list totals always match `counts`.
    def ranked(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda i: (-i["gain"], i["difficulty"] or 100))

    improve_l, gaps_l, targets_l = ranked(improve), ranked(list(gaps.values())), ranked(targets)

    # ── How the plan is calculated (kept here, in code, NOT in the UI) ──────────
    # Click-through rate (CTR) by position: CTR = 30% / position**0.8, capped 35%
    #   (see opportunity_service._ctr_from_position). Roughly #1≈30%, #3≈12.5%,
    #   #5≈8.7%, #10≈4.8%, #20≈2.7%.
    # Gain (extra visits/mo):
    #   improve (we rank #4-50): volume × (CTR at #TARGET_POSITION − CTR at our pos).
    #   gap / target:            volume × CTR at #TARGET_POSITION.
    # Priority = gain × ease² × (1.5 for "improve", else 1.0), ease = 1 − KD/100.
    #   ease² makes easy keywords dominate and hard head terms (e.g. "upsc", KD 75)
    #   fall away; "improve" is weighted up because re-ranking an existing page is
    #   faster than ranking a brand-new one.
    # Top 10 = best 4 improve + 4 gaps + 2 targets, then spare slots by priority,
    #   so one huge list can't crowd the others. The headline total_gain leaves out
    #   very hard keywords (KD >= HARD_KD).
    # Filters: competitor-brand and "near me" terms are dropped (keyword_relevance);
    #   competitor keywords must be on-topic (_on_topic); spelling variants collapse
    #   to one (_variant_key).
    def priority(i: dict) -> float:
        return i["gain"] * _ease(i["difficulty"]) ** 2 * (1.5 if i["type"] == "improve" else 1.0)
    by_prio = lambda items: sorted(items, key=priority, reverse=True)  # noqa: E731
    top = by_prio(improve_l)[:4] + by_prio(gaps_l)[:4] + by_prio(targets_l)[:2]
    chosen = {id(i) for i in top}
    top += [i for i in by_prio(improve_l + gaps_l + targets_l) if id(i) not in chosen][:10 - len(top)]
    top = by_prio(top)

    with_data = [c for c in competitors if ranked_keywords_service.latest_run(
        db, c, location_code=loc, language=language, env=env) is not None]
    return {
        "domain": domain.domain,
        "environment": env,
        "generated_at": datetime.now(timezone.utc),
        "target_position": TARGET_POSITION,
        "counts": {"improve": len(improve), "gaps": len(gaps), "targets": len(targets)},
        # Headline excludes very hard head terms (KD >= 70) — unrealistic in the short run.
        "total_gain": sum(i["gain"] for i in top if (i["difficulty"] or 0) < HARD_KD),
        "hard_kd": HARD_KD,
        "sources": {
            "search_console_keywords": sum(1 for r in tracked_rows if r.current_position is not None),
            "our_ranked_keywords": our_run.items_count if our_run else 0,
            "competitors": competitors,
            "competitors_with_rankings": with_data,
        },
        "top": top,
        "improve": improve_l,
        "gaps": gaps_l,
        "targets": targets_l,
    }


def _ensure_rank_tracked(db: Session, domain: Domain, keyword: str) -> None:
    """Also add the keyword to the domain's real Rank Tracking list so the user can
    see it (with position history) on the Rank Tracking page. Idempotent — an
    already-tracked keyword is left as-is."""
    from app.services import rank_service

    try:
        rank_service.add_keyword(db, domain, keyword)
    except rank_service.KeywordExistsError:
        pass


def track_keyword(db: Session, domain: Domain, keyword: str, *, country: str | None = None) -> dict:
    """Add a keyword to the domain's tracked list — idempotent.

    Writes to `opportunity_keywords` (feeds the Action Plan's own math) AND to the
    real Rank Tracking list, so tracked keywords actually surface for the user.
    """
    from app.services import keyword_analysis_service as kas, keyword_scoring

    if country is None:
        country = domain_settings.resolve(domain, getattr(domain, "workspace", None)).country
    kw_text = " ".join(keyword.strip().split())[:255]
    if not kw_text:
        raise ValueError("Keyword is empty")
    existing = db.execute(text(
        "SELECT id FROM opportunity_keywords WHERE domain_id = :d AND lower(keyword) = lower(:k)"),
        {"d": str(domain.id), "k": kw_text}).first()
    if existing:
        _ensure_rank_tracked(db, domain, kw_text)
        return {"keyword": kw_text, "tracked": True, "created": False}

    kw = kas._upsert_keyword(db, kw_text)
    metric = db.execute(text(
        "SELECT search_volume, difficulty FROM keyword_metrics WHERE keyword_id = :k "
        "AND search_volume IS NOT NULL ORDER BY metric_date DESC LIMIT 1"), {"k": str(kw.id)}).first()
    from app.models.opportunity import OpportunityKeyword
    db.add(OpportunityKeyword(
        domain_id=domain.id, keyword_id=kw.id, keyword=kw_text,
        search_volume=int(metric.search_volume) if metric and metric.search_volume else 0,
        keyword_difficulty=int(round(float(metric.difficulty))) if metric and metric.difficulty is not None
        else keyword_scoring.heuristic_difficulty(kw_text),
        intent=keyword_scoring.classify_intent(kw_text) or "informational",
        country_code=country,
    ))
    db.commit()
    _ensure_rank_tracked(db, domain, kw_text)
    return {"keyword": kw_text, "tracked": True, "created": True}
