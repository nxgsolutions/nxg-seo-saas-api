"""Keywords a domain ACTUALLY ranks for — DataForSEO Labs ranked_keywords,
fetched on demand, stored, and compared against our own domain.

Budget rules (the live API is paid):
  * fetches happen ONLY on an explicit refresh call — reads are DB-only;
  * a stored run younger than PROVIDER_CACHE_TTL_DAYS is reused unless forced;
  * keywords per fetch are capped (DATAFORSEO_RANKED_KEYWORDS_MAX_LIMIT);
  * in live workspaces only DATAFORSEO_LIVE_RANKED_TARGETS may be fetched
    (empty = any); sandbox workspaces (free dummy data) are unrestricted;
  * the provider-reported cost of every fetch is stored on the run;
  * multi-page scopes ("relevant" / "all") are capped by keywords AND by
    projected cost per fetch, and stop as soon as a page adds nothing new.

Scopes:
  top      — top-N keywords by traffic (one call; the original behaviour)
  relevant — server-side filtered: keyword matches the relevance regex,
             position <= N, volume >= M; paged up to the caps
  all      — every keyword the domain ranks for, paged up to the caps
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.domain import Domain
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.services.pagination import PageQuery, paginate
from app.services import data_env, dataforseo_client, domain_analytics_service, domain_settings
from app.services.dataforseo_client import DataForSeoError


class RankedFetchNotAllowed(Exception):
    """Live fetch refused for a domain outside the budget allowlist."""


def norm_host(host: str) -> str:
    h = re.sub(r"^https?://", "", (host or "").strip().lower()).split("/")[0]
    return h[4:] if h.startswith("www.") else h


def clamp_limit(limit: int | None) -> int:
    limit = limit or settings.DATAFORSEO_RANKED_KEYWORDS_LIMIT
    return max(1, min(int(limit), settings.DATAFORSEO_RANKED_KEYWORDS_MAX_LIMIT))


def estimated_cost(limit: int, env: str) -> float:
    """DataForSEO Labs pricing: $0.01 per task + $0.0001 per returned keyword."""
    return 0.0 if env == data_env.SANDBOX else round(0.01 + 0.0001 * limit, 4)


SCOPES = ("top", "relevant", "all")
PAGE_SIZE = 1000  # DataForSEO Labs max per request


def scope_filters(scope: str, topic_regex: str | None = None) -> list | None:
    """DataForSEO Labs filter expression for a scope (None = no filter).

    `topic_regex` is the domain's on-topic rule (domain_settings). When it's
    None (no niche set) the keyword-regex clause is omitted, so "relevant" means
    just top-positions + minimum volume rather than forcing a fixed niche."""
    if scope != "relevant":
        return None
    parts: list = []
    if topic_regex:
        parts += [["keyword_data.keyword", "regex", topic_regex], "and"]
    parts += [
        ["ranked_serp_element.serp_item.rank_absolute", "<=", settings.DATAFORSEO_RELEVANT_MAX_POSITION],
        "and",
        ["keyword_data.keyword_info.search_volume", ">=", settings.DATAFORSEO_RELEVANT_MIN_VOLUME],
    ]
    return parts


def scope_max_keywords(scope: str) -> int:
    """Hard cap on keywords per fetch for a multi-page scope. 'relevant' has a
    tighter cap so a single opportunity fetch stays cheap; 'all' keeps the
    larger shared cap (still reachable from scripts / sandbox, not the UI)."""
    if scope == "relevant":
        return settings.DATAFORSEO_RELEVANT_MAX_KEYWORDS
    return settings.DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH


def pages_cost(n_keywords: int, env: str) -> tuple[int, float]:
    """(requests, USD) to fetch n keywords in 1000-keyword pages."""
    if n_keywords <= 0:
        return 0, 0.0
    pages = -(-n_keywords // PAGE_SIZE)
    return pages, 0.0 if env == data_env.SANDBOX else round(pages * 0.01 + n_keywords * 0.0001, 4)


def affordable_keywords(budget: float, env: str) -> int:
    """Most keywords whose paged fetch stays within `budget` USD."""
    if env == data_env.SANDBOX:
        return settings.DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH
    n = 0
    while n < settings.DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH:
        step = min(PAGE_SIZE, settings.DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH - n)
        if pages_cost(n + step, env)[1] > budget:
            # Partial last page: remaining budget after full pages' fixed fees.
            pages = -(-(n + 1) // PAGE_SIZE)
            extra = int((budget - pages * 0.01 - n * 0.0001) / 0.0001)
            return max(n, min(n + max(extra, 0), n + step))
        n += step
    return n


def allowed_for_domain(db: Session, domain: Domain) -> set[str]:
    """Domains this workspace may fetch live besides the global allowlist:
    its own site and every competitor on its competitor list."""
    hosts = {norm_host(domain.domain)}
    hosts |= {norm_host(c) for c in db.execute(
        text("SELECT competitor FROM tracked_competitors WHERE domain_id = :d"), {"d": str(domain.id)}).scalars()}
    return hosts


def can_fetch(target: str, env: str, extra_allowed: set[str] | None = None) -> bool:
    """Sandbox: always. Live: only when enabled, and the target is on the global
    allowlist (DATAFORSEO_LIVE_RANKED_TARGETS; empty = any) or in
    `extra_allowed` (the domain's own site + its competitors)."""
    if env == data_env.SANDBOX:
        return True
    if not settings.DATAFORSEO_LIVE_ENABLED:
        return False
    allowed = settings.live_ranked_targets
    host = norm_host(target)
    return not allowed or host in allowed or host in (extra_allowed or set())


def latest_run(
    db: Session, target: str, *, location_code: int, language: str, env: str,
    scope: str | None = None,
) -> DomainRankedKeywordRun | None:
    """Newest run for the target (optionally of one scope; runs stored before
    scopes existed count as 'top')."""
    runs = db.execute(
        select(DomainRankedKeywordRun)
        .where(
            DomainRankedKeywordRun.environment == env,
            DomainRankedKeywordRun.target == norm_host(target),
            DomainRankedKeywordRun.location_code == location_code,
            DomainRankedKeywordRun.language_code == language,
        )
        .order_by(DomainRankedKeywordRun.fetched_at.desc())
    ).scalars()
    for run in runs:
        if scope is None or run_scope(run) == scope:
            return run
    return None


def top_pages(rows: list[dict], limit: int = 25) -> list[dict]:
    """Competitor pages (ranking URLs) grouped from keyword rows: how many
    keywords each page ranks for, best position, and traffic."""
    pages: dict[str, dict] = {}
    for r in rows:
        if not r["url"]:
            continue
        p = pages.setdefault(r["url"], {"url": r["url"], "title": r["title"], "keywords": 0,
                                         "best_position": None, "etv": 0.0,
                                         "top_keyword": None, "_top_etv": -1.0})
        p["keywords"] += 1
        if r["position"] is not None and (p["best_position"] is None or r["position"] < p["best_position"]):
            p["best_position"] = r["position"]
        p["etv"] += r["etv"] or 0
        if (r["etv"] or 0) > p["_top_etv"]:
            p["_top_etv"], p["top_keyword"] = r["etv"] or 0, r["keyword"]
    out = sorted(pages.values(), key=lambda p: (-p["etv"], -p["keywords"]))[:limit]
    for p in out:
        p.pop("_top_etv")
        p["etv"] = round(p["etv"], 1)
    return out


def run_rows(db: Session, run: DomainRankedKeywordRun) -> list[DomainRankedKeyword]:
    return list(db.execute(
        select(DomainRankedKeyword)
        .where(DomainRankedKeyword.run_id == run.id)
        .order_by(DomainRankedKeyword.etv.desc().nulls_last(),
                  DomainRankedKeyword.rank_absolute.asc().nulls_last())
    ).scalars())


def _check_fetch_allowed(target: str, env: str, extra_allowed: set[str] | None = None) -> str:
    target = norm_host(target)
    if not target or "." not in target:
        raise ValueError("Enter a valid domain, e.g. example.com")
    if not dataforseo_client.is_configured():
        raise DataForSeoError("DataForSEO credentials are not configured.")
    if env == data_env.LIVE and not settings.DATAFORSEO_LIVE_ENABLED:
        raise RankedFetchNotAllowed(
            "Live DataForSEO calls are disabled (DATAFORSEO_LIVE_ENABLED=false). "
            "Stored results are still shown; use a sandbox workspace to test."
        )
    if not can_fetch(target, env, extra_allowed):
        raise RankedFetchNotAllowed(
            f"Add {target} to your competitors to fetch its keywords."
        )
    return target


def run_scope(run: DomainRankedKeywordRun | None) -> str | None:
    return (run.meta or {}).get("scope", "top") if run else None


def estimate(
    target: str, *, location_code: int, language: str, env: str, scope: str = "relevant",
    extra_allowed: set[str] | None = None, topic_regex: str | None = None,
) -> dict:
    """How many keywords a scope matches and what fetching them costs.

    One 1-keyword request (~$0.0101 live, free in sandbox) — DataForSEO returns
    the matching total. Nothing is stored."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    target = _check_fetch_allowed(target, env, extra_allowed)
    data = dataforseo_client.ranked_keywords(
        target, location_code=location_code, language_code=language, limit=1, env=env,
        filters=scope_filters(scope, topic_regex))
    matching = int(data["total_count"] or 0)
    if scope == "top":
        to_fetch = min(matching, clamp_limit(None))
    else:
        to_fetch = min(matching, scope_max_keywords(scope),
                       affordable_keywords(settings.DATAFORSEO_RANKED_MAX_COST_PER_FETCH, env))
    requests, cost = pages_cost(to_fetch, env)
    full_requests, full_cost = pages_cost(matching, env)
    return {
        "target": target,
        "scope": scope,
        "environment": env,
        "matching_count": matching,
        "to_fetch": to_fetch,
        "requests": requests,
        "estimated_cost": cost,
        "full_cost": full_cost,             # what ALL matches would cost (uncapped)
        "capped": to_fetch < matching,
        "max_keywords": settings.DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH,
        "max_cost": settings.DATAFORSEO_RANKED_MAX_COST_PER_FETCH,
        "check_cost": data["cost"] or 0.0,  # what this estimate itself cost
        "filters": scope_filters(scope, topic_regex),
    }


def fetch(
    db: Session, target: str, *, location_code: int, language: str, env: str,
    limit: int | None = None, force: bool = False, scope: str = "top",
    max_keywords: int | None = None, extra_allowed: set[str] | None = None,
    topic_regex: str | None = None,
) -> tuple[DomainRankedKeywordRun, bool]:
    """Return (run, fetched_now). Reuses a fresh stored run of the same scope
    unless `force`. `top` = one call of `limit` keywords (original behaviour);
    `relevant` / `all` page through up to the keyword + cost caps."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    target = _check_fetch_allowed(target, env, extra_allowed)

    existing = latest_run(db, target, location_code=location_code, language=language,
                          env=env, scope=scope)
    ttl = timedelta(days=settings.PROVIDER_CACHE_TTL_DAYS)
    if existing is not None and not force and existing.fetched_at > datetime.now(timezone.utc) - ttl:
        return existing, False

    filters = scope_filters(scope, topic_regex)
    if scope == "top":
        cap = clamp_limit(limit)
    else:
        scope_cap = scope_max_keywords(scope)
        cap = min(max_keywords or scope_cap, scope_cap)
    budget = settings.DATAFORSEO_RANKED_MAX_COST_PER_FETCH
    cap = min(cap, affordable_keywords(budget, env)) if scope != "top" else cap

    items: list[dict] = []
    seen: set[str] = set()
    total_cost = 0.0
    first_meta: dict = {}
    total_count = None
    pages = 0
    stop_reason = "complete"
    offset = 0
    while len(items) < cap:
        page_limit = min(PAGE_SIZE, cap - len(items))
        data = dataforseo_client.ranked_keywords(
            target, location_code=location_code, language_code=language,
            limit=page_limit, env=env, offset=offset, filters=filters)
        pages += 1
        total_cost += data["cost"] or 0.0
        if pages == 1:
            first_meta = data["meta"] or {}
            total_count = data["total_count"]
        new = 0
        for it in data["items"]:
            key = it["keyword"].lower()
            if key not in seen:
                seen.add(key)
                items.append(it)
                new += 1
        offset += len(data["items"])
        if new == 0:
            stop_reason = "no_new_keywords"   # sandbox repeats; live list exhausted
            break
        if len(data["items"]) < page_limit or (total_count is not None and offset >= total_count):
            stop_reason = "complete"
            break
        if scope == "top":
            break
    else:
        stop_reason = "cap_reached"

    run = DomainRankedKeywordRun(
        environment=env, target=target, location_code=location_code,
        language_code=language,
        source="dataforseo_sandbox" if env == data_env.SANDBOX else "dataforseo_labs",
        requested_limit=cap, total_count=total_count, items_count=len(items),
        cost=round(total_cost, 5),
        meta={**first_meta, "scope": scope, "filters": filters, "pages": pages,
              "stop_reason": stop_reason},
    )
    db.add(run)
    db.flush()

    # Live rows link to the canonical keyword table; sandbox dummy keywords don't.
    from app.services import keyword_analysis_service as kas  # local: avoid import cycle

    for it in items:
        keyword_id = kas._upsert_keyword(db, it["keyword"]).id if env == data_env.LIVE else None
        db.add(DomainRankedKeyword(run_id=run.id, keyword_id=keyword_id, **it))
    db.commit()
    db.refresh(run)
    return run, True


def _our_positions(db: Session, domain: Domain, our_run: DomainRankedKeywordRun | None
                   ) -> dict[str, dict]:
    """keyword(lower) -> {position, url, source} for OUR domain: our own ranked
    run first, else the GSC position stored on opportunity_keywords."""
    out: dict[str, dict] = {}
    for kw, pos in db.execute(text(
        "SELECT keyword, current_position FROM opportunity_keywords "
        "WHERE domain_id = :d AND current_position IS NOT NULL"
    ), {"d": str(domain.id)}).all():
        out[kw.lower()] = {"position": int(pos), "url": None, "source": "gsc"}
    if our_run is not None:
        for r in run_rows(db, our_run):
            if r.rank_absolute is not None:
                out[r.keyword.lower()] = {"position": r.rank_absolute, "url": r.url,
                                          "source": "ranked"}
    return out


# Position buckets the table filters by (rank_absolute).
BUCKETS = {
    "all": lambda p: True,
    "top3": lambda p: p is not None and p <= 3,
    "top10": lambda p: p is not None and 3 < p <= 10,
    "top20": lambda p: p is not None and 10 < p <= 20,
    "rest": lambda p: p is not None and p > 20,
}
SORTS = {"etv", "position", "search_volume", "keyword_difficulty", "keyword"}


def _filter_rows(rows: list[dict], *, q: str, bucket: str, show: str, url: str | None) -> list[dict]:
    test = BUCKETS.get(bucket, BUCKETS["all"])
    out = []
    for r in rows:
        if not test(r["position"]):
            continue
        if show == "gap" and not r["is_gap"]:
            continue
        if show == "common" and r["our_position"] is None:
            continue
        if url and r["url"] != url:
            continue
        if q and q not in r["keyword"].lower():
            continue
        out.append(r)
    return out


def payload(
    db: Session, domain: Domain, competitor: str, *,
    country: str | None = None, language: str | None = None,
    refresh: dict | None = None, scope: str | None = None,
    listing: PageQuery | None = None, bucket: str = "all", show: str = "all", url: str | None = None,
) -> dict:
    """Stored ranked keywords for `competitor` (DB only), compared with ours.
    `scope` picks the newest run of that scope (default: newest of any).

    Only one page of rows is returned (`listing` + bucket/show/url filters,
    server-side); `summary` and `pages` still cover every stored row."""
    s = domain_settings.resolve(domain, getattr(domain, "workspace", None))
    country = country or s.country
    language = language or s.language
    env = data_env.env_for_domain(db, domain)
    loc = domain_analytics_service.location_code_for(country)
    host = norm_host(domain.domain)
    target = norm_host(competitor)
    is_you = target == host

    run = latest_run(db, target, location_code=loc, language=language, env=env, scope=scope)
    our_run = None if is_you else latest_run(db, host, location_code=loc, language=language, env=env)
    ours = {} if is_you else _our_positions(db, domain, our_run)
    tracked = {k.lower() for (k,) in db.execute(text(
        "SELECT keyword FROM opportunity_keywords WHERE domain_id = :d"), {"d": str(domain.id)}).all()}

    rows = []
    for r in (run_rows(db, run) if run else []):
        mine = ours.get(r.keyword.lower())
        change = (r.prev_rank_absolute - r.rank_absolute
                  if r.prev_rank_absolute is not None and r.rank_absolute is not None else None)
        rows.append({
            "keyword": r.keyword,
            "position": r.rank_absolute,
            "prev_position": r.prev_rank_absolute,
            "change": change,               # + = moved up
            "is_new": r.is_new,
            "url": r.url,
            "title": r.title,
            "etv": round(r.etv, 1) if r.etv is not None else None,
            "search_volume": r.search_volume,
            "cpc": float(r.cpc) if r.cpc is not None else None,
            "paid_difficulty": round(r.competition * 100) if r.competition is not None else None,
            "keyword_difficulty": r.keyword_difficulty,
            "intent": r.intent,
            "serp_features": r.serp_features or [],
            "our_position": mine["position"] if mine else None,
            "our_url": mine["url"] if mine else None,
            "our_position_source": mine["source"] if mine else None,
            "is_gap": (not is_you) and mine is None,
            "tracked": r.keyword.lower() in tracked,
        })

    listing = listing or PageQuery(page=1, page_size=25, sort=None, descending=True, q=None)
    sort = listing.sort if listing.sort in SORTS else "etv"
    filtered = _filter_rows(rows, q=listing.search(), bucket=bucket, show=show, url=url)
    page_rows, page_info = paginate(
        filtered, page=listing.page, page_size=listing.page_size,
        sort_key=(lambda r: r["keyword"].lower()) if sort == "keyword" else sort,
        descending=listing.descending)

    limit = clamp_limit(None)
    positions = [r["position"] for r in rows if r["position"] is not None]
    return {
        "domain": domain.domain,
        "target": target,
        "is_you": is_you,
        "environment": env,
        "configured": dataforseo_client.is_configured(),
        "can_fetch": can_fetch(target, env, allowed_for_domain(db, domain)),
        "live_allowlist": sorted(settings.live_ranked_targets),
        "live_enabled": settings.DATAFORSEO_LIVE_ENABLED,
        "default_limit": limit,
        "max_limit": settings.DATAFORSEO_RANKED_KEYWORDS_MAX_LIMIT,
        "estimated_cost": estimated_cost(limit, env),
        "fetched_at": run.fetched_at if run else None,
        "total_count": run.total_count if run else None,
        "items_count": run.items_count if run else 0,
        "cost": float(run.cost) if run and run.cost is not None else None,
        "has_our_run": our_run is not None,
        "scope": run_scope(run),
        "stored_scopes": stored_scopes(db, target, location_code=loc, language=language, env=env),
        "pages_fetched": (run.meta or {}).get("pages") if run else None,
        "stop_reason": (run.meta or {}).get("stop_reason") if run else None,
        "relevant_rule": {
            # The domain's own topic terms (empty when no niche is set).
            "regex": s.topic_regex or "",
            "max_position": settings.DATAFORSEO_RELEVANT_MAX_POSITION,
            "min_volume": settings.DATAFORSEO_RELEVANT_MIN_VOLUME,
        },
        "max_keywords": settings.DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH,
        "max_cost": settings.DATAFORSEO_RANKED_MAX_COST_PER_FETCH,
        "pages": top_pages(rows),
        "summary": {
            "top3": sum(1 for p in positions if p <= 3),
            "top10": sum(1 for p in positions if p <= 10),
            "etv": round(sum(r["etv"] or 0 for r in rows)),
            "common": sum(1 for r in rows if r["our_position"] is not None),
            "gap": sum(1 for r in rows if r["is_gap"]),
            "tracked": sum(1 for r in rows if r["tracked"]),
        },
        "rows": page_rows,
        "pagination": page_info,
        "refresh": refresh,
    }


def stored_scopes(db: Session, target: str, *, location_code: int, language: str,
                  env: str) -> list[dict]:
    """Newest stored run per scope for a target (to switch views for free)."""
    out: dict[str, dict] = {}
    for run in db.execute(
        select(DomainRankedKeywordRun)
        .where(DomainRankedKeywordRun.environment == env,
               DomainRankedKeywordRun.target == norm_host(target),
               DomainRankedKeywordRun.location_code == location_code,
               DomainRankedKeywordRun.language_code == language)
        .order_by(DomainRankedKeywordRun.fetched_at.desc())
    ).scalars():
        sc = run_scope(run)
        if sc not in out:
            out[sc] = {"scope": sc, "fetched_at": run.fetched_at, "items_count": run.items_count,
                       "total_count": run.total_count,
                       "cost": float(run.cost) if run.cost is not None else None}
    return list(out.values())


def options(db: Session, domain: Domain, *,
            country: str | None = None, language: str | None = None,
            suggestions: list[str] | None = None) -> dict:
    """Domains to offer in the UI: this domain, its competitor list and
    footprint suggestions — with the ones that have stored runs (free to open).

    Runs are global (public rankings, shared by every workspace), but only
    domains relevant to THIS domain are offered, so one tenant never sees
    another tenant's sites or competitors as suggestions. Any other domain can
    still be typed in and opened."""
    s = domain_settings.resolve(domain, getattr(domain, "workspace", None))
    country = country or s.country
    language = language or s.language
    env = data_env.env_for_domain(db, domain)
    loc = domain_analytics_service.location_code_for(country)
    mine = {norm_host(domain.domain), *(norm_host(s) for s in (suggestions or []))}
    mine |= {norm_host(c) for c in db.execute(
        text("SELECT competitor FROM tracked_competitors WHERE domain_id = :d"), {"d": str(domain.id)}).scalars()}
    stored = db.execute(text(
        """
        SELECT DISTINCT ON (target) target, fetched_at, items_count, total_count, cost
        FROM domain_ranked_keyword_runs
        WHERE environment = :env AND location_code = :loc AND language_code = :lang
          AND target = ANY(:mine)
        ORDER BY target, fetched_at DESC
        """
    ), {"env": env, "loc": loc, "lang": language, "mine": sorted(mine)}).all()
    return {
        "environment": env,
        "your_domain": norm_host(domain.domain),
        "live_allowlist": sorted(settings.live_ranked_targets),
        "live_enabled": settings.DATAFORSEO_LIVE_ENABLED,
        "stored": [
            {"target": s.target, "fetched_at": s.fetched_at, "items_count": s.items_count,
             "total_count": s.total_count, "cost": float(s.cost) if s.cost is not None else None}
            for s in sorted(stored, key=lambda s: s.fetched_at, reverse=True)
        ],
        "suggestions": suggestions or [],
    }
