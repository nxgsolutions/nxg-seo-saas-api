"""Thin DataForSEO API v3 client (Labs keyword ideas + domain overview, SERP
organic, Google Ads volume, Backlinks domain rank).

Paid third-party provider. Auth is HTTP Basic with the account login + API
password (Settings.DATAFORSEO_LOGIN / _PASSWORD). Only the endpoints the app
uses are wrapped, and every response is normalized to flat dicts so callers
never touch DataForSEO's nested envelope.

Docs: https://docs.dataforseo.com/v3/
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.services import data_env

_BASE_URL = "https://api.dataforseo.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Live "advanced" SERP (depth 100) for a fresh keyword routinely takes longer
# than the default read timeout, so those calls get a generous one. The
# frontend's write timeout (120s) still bounds the whole request.
_SERP_TIMEOUT = httpx.Timeout(90.0, connect=10.0)


class DataForSeoError(RuntimeError):
    """Raised when DataForSEO is unreachable or returns a non-success task."""


def is_configured() -> bool:
    return settings.dataforseo_configured


def _base_url(env: str | None) -> str:
    """Live API or the free Sandbox (dummy data, same credentials and shape)."""
    env = env or data_env.default_env()
    if settings.DATAFORSEO_FORCE_SANDBOX or env == data_env.SANDBOX:
        if not settings.dataforseo_sandbox_allowed:
            raise DataForSeoError(
                "DataForSEO sandbox is disabled in this environment "
                "(APP_ENV=production; set DATAFORSEO_SANDBOX_ENABLED=true to allow)."
            )
        return settings.DATAFORSEO_SANDBOX_URL.rstrip("/")
    if not settings.DATAFORSEO_LIVE_ENABLED:
        raise DataForSeoError(
            "Live DataForSEO calls are disabled (DATAFORSEO_LIVE_ENABLED=false) — "
            "stored data is still shown; use a sandbox workspace to test."
        )
    return _BASE_URL


def _post(path: str, task: dict, env: str | None = None,
          timeout: httpx.Timeout | None = None) -> dict:
    """POST a single-task array and return that task's `result` envelope.

    `env` ("live" | "sandbox", default per data_env.default_env) picks the host.
    `timeout` overrides the default read timeout for slow endpoints (live SERP).

    DataForSEO wraps everything: {tasks: [{status_code, result: [...]}]}. A task
    status_code of 20000 is success; anything else is surfaced as an error.
    """
    if not settings.dataforseo_configured:
        raise DataForSeoError("DataForSEO credentials are not configured.")
    try:
        response = httpx.post(
            f"{_base_url(env)}{path}",
            json=[task],
            auth=(settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD),
            timeout=timeout or _TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        raise DataForSeoError(f"DataForSEO request failed: {exc}") from exc
    except ValueError as exc:  # invalid JSON
        raise DataForSeoError("DataForSEO returned an invalid response.") from exc

    tasks = body.get("tasks") or []
    if not tasks:
        raise DataForSeoError("DataForSEO returned no tasks.")
    task_obj = tasks[0]
    if task_obj.get("status_code") != 20000:
        raise DataForSeoError(
            f"DataForSEO task error {task_obj.get('status_code')}: "
            f"{task_obj.get('status_message')}"
        )
    return task_obj


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def keyword_ideas(
    seed: str, *, location_name: str, language_code: str, limit: int, env: str | None = None
) -> list[dict]:
    """Related keyword ideas for a seed, with volume / CPC / competition.

    Endpoint: dataforseo_labs/google/keyword_ideas/live. Returns flat dicts:
    {keyword, search_volume, cpc, competition, difficulty, intent, monthly_searches}.
    """
    task = {
        "keywords": [seed],
        "location_name": location_name,
        "language_code": language_code,
        "limit": max(1, min(limit, 1000)),
        "order_by": ["keyword_info.search_volume,desc"],
    }
    result = _post("/v3/dataforseo_labs/google/keyword_ideas/live", task, env)
    items = (result.get("result") or [{}])[0].get("items") or []

    out: list[dict] = []
    for item in items:
        info = item.get("keyword_info") or {}
        props = item.get("keyword_properties") or {}
        intent_info = item.get("search_intent_info") or {}
        out.append(
            {
                "keyword": item.get("keyword") or "",
                "search_volume": info.get("search_volume"),
                "cpc": _num(info.get("cpc")),
                "competition": _num(info.get("competition")),  # 0..1
                "difficulty": props.get("keyword_difficulty"),
                "intent": intent_info.get("main_intent"),
                "monthly_searches": info.get("monthly_searches") or [],
            }
        )
    return [row for row in out if row["keyword"]]


def serp_organic(
    keyword: str, *, location_name: str, language_code: str, depth: int = 20, env: str | None = None
) -> list[dict]:
    """Live Google organic SERP for a keyword.

    Endpoint: serp/google/organic/live/advanced. Returns flat dicts:
    {position, url, domain, title}.
    """
    task = {
        "keyword": keyword,
        "location_name": location_name,
        "language_code": language_code,
        "depth": max(1, min(depth, 100)),
    }
    result = _post("/v3/serp/google/organic/live/advanced", task, env, timeout=_SERP_TIMEOUT)
    items = (result.get("result") or [{}])[0].get("items") or []

    out: list[dict] = []
    for item in items:
        if item.get("type") != "organic":
            continue
        position = item.get("rank_absolute") or item.get("rank_group")
        url = item.get("url")
        if position is None or not url:
            continue
        out.append(
            {
                "position": int(position),
                "url": url,
                "domain": item.get("domain") or "",
                "title": item.get("title"),
            }
        )
    return out


def serp_organic_snapshot(
    keyword: str, *, location_name: str, language_code: str, depth: int = 20, env: str | None = None
) -> dict:
    """Live Google organic SERP with FULL metadata, shaped for serp_snapshots.

    Same endpoint as `serp_organic` (serp/google/organic/live/advanced) but keeps
    the SERP-level fields (location_code, se_results_count, check_url) and the
    richer per-item fields (rank_group, description, breadcrumb, raw) so a whole
    snapshot can be stored, diffed and re-parsed. Returns:

        {location_code, se_results_count, check_url, items_count, items[], raw}

    where each item is
        {type, rank_absolute, rank_group, domain, url, title, description,
         breadcrumb, raw}
    and `raw` is the SERP-level result payload WITHOUT items[] (items carry their
    own raw), so nothing is duplicated in storage.
    """
    task = {
        "keyword": keyword,
        "location_name": location_name,
        "language_code": language_code,
        "depth": max(1, min(depth, 100)),
    }
    result = _post("/v3/serp/google/organic/live/advanced", task, env, timeout=_SERP_TIMEOUT)
    res0 = (result.get("result") or [{}])[0]
    raw_items = res0.get("items") or []

    items: list[dict] = []
    for item in raw_items:
        if item.get("type") != "organic":
            continue
        position = item.get("rank_absolute") or item.get("rank_group")
        url = item.get("url")
        if position is None or not url:
            continue
        items.append(
            {
                "type": "organic",
                "rank_absolute": int(position),
                "rank_group": item.get("rank_group"),
                "domain": item.get("domain") or "",
                "url": url,
                "title": item.get("title"),
                "description": item.get("description"),
                "breadcrumb": item.get("breadcrumb"),
                "raw": item,
            }
        )

    return {
        "location_code": res0.get("location_code"),
        "se_results_count": res0.get("se_results_count"),
        "check_url": res0.get("check_url"),
        "items_count": res0.get("items_count") or len(items),
        "items": items,
        # SERP-level payload minus items[] (kept lean; items store their own raw).
        "raw": {k: v for k, v in res0.items() if k != "items"},
    }


def search_volume(
    keywords: list[str], *, location_name: str, language_code: str, env: str | None = None
) -> dict[str, int]:
    """Exact monthly search volume per keyword (Google Ads, bulk).

    Endpoint: keywords_data/google_ads/search_volume/live. Returns
    {keyword_lower: search_volume}. Missing/None volumes are omitted.
    """
    kws = [k.strip()[:80] for k in keywords if k.strip()][:700]
    if not kws:
        return {}
    task = {"keywords": kws, "location_name": location_name, "language_code": language_code}
    result = _post("/v3/keywords_data/google_ads/search_volume/live", task, env)
    items = result.get("result") or []
    out: dict[str, int] = {}
    for item in items:
        kw = (item.get("keyword") or "").strip().lower()
        vol = item.get("search_volume")
        if kw and vol is not None:
            out[kw] = int(vol)
    return out


def domain_rank_overview(
    target: str, *, location_code: int, language_code: str, env: str | None = None
) -> dict | None:
    """Whole-site organic/paid metrics for one domain (DataForSEO Labs).

    Endpoint: dataforseo_labs/google/domain_rank_overview/live — the live
    endpoint accepts ONE task per request. Returns the item's `metrics` dict
    ({organic: {etv, count, pos_1, pos_2_3, ...}, paid: {...}}), or None when
    Labs has no data for the domain in that location.
    """
    task = {"target": target, "location_code": location_code, "language_code": language_code}
    result = _post("/v3/dataforseo_labs/google/domain_rank_overview/live", task, env)
    items = (result.get("result") or [{}])[0].get("items") or []
    return (items[0].get("metrics") or None) if items else None


def account_balance(env: str | None = None) -> float | None:
    """Current account balance in USD (appendix/user_data). None if unavailable.

    Used only to measure real spend in tooling/tests — never surfaced in the UI.
    """
    if not settings.dataforseo_configured:
        raise DataForSeoError("DataForSEO credentials are not configured.")
    try:
        response = httpx.get(
            f"{_base_url(env)}/v3/appendix/user_data",
            auth=(settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DataForSeoError(f"DataForSEO user_data failed: {exc}") from exc
    task = (body.get("tasks") or [{}])[0]
    money = ((task.get("result") or [{}])[0] or {}).get("money") or {}
    bal = money.get("balance")
    return float(bal) if bal is not None else None


def bulk_domain_ranks(targets: list[str], *, env: str | None = None) -> dict[str, int]:
    """Domain rank (0-100 scale) for up to 1000 domains in one call.

    Endpoint: backlinks/bulk_ranks/live. Returns {target_lower: rank}; targets
    missing from the response are omitted.
    """
    targets = [t for t in targets if t][:1000]
    if not targets:
        return {}
    task = {"targets": targets, "rank_scale": "one_hundred"}
    result = _post("/v3/backlinks/bulk_ranks/live", task, env)
    items = (result.get("result") or [{}])[0].get("items") or []
    return {
        (i.get("target") or "").lower(): int(i["rank"])
        for i in items if i.get("target") and i.get("rank") is not None
    }


def bulk_backlinks(targets: list[str], *, env: str | None = None) -> dict[str, dict]:
    """Backlink + referring-domain counts for up to 1000 domains (2 calls total).

    Endpoints: backlinks/bulk_backlinks/live and bulk_referring_domains/live.
    Returns {target_lower: {backlinks, referring_domains}}; a target missing from
    a response simply lacks that key.
    """
    targets = [t for t in targets if t][:1000]
    out: dict[str, dict] = {}
    if not targets:
        return out
    for path, field in (("/v3/backlinks/bulk_backlinks/live", "backlinks"),
                        ("/v3/backlinks/bulk_referring_domains/live", "referring_domains")):
        result = _post(path, {"targets": targets}, env)
        for item in (result.get("result") or [{}])[0].get("items") or []:
            target = (item.get("target") or "").lower()
            if target and item.get(field) is not None:
                out.setdefault(target, {})[field] = int(item[field])
    return out


def ranked_keywords(
    target: str, *, location_code: int, language_code: str, limit: int,
    env: str | None = None, offset: int = 0, filters: list | None = None,
) -> dict:
    """Keywords a domain actually ranks for (organic), top `limit` by traffic.

    Endpoint: dataforseo_labs/google/ranked_keywords/live (one task per call;
    billed ~$0.01 + $0.0001 per returned item). Returns
        {total_count, items_count, cost, meta, items[]}
    where each item is flat: {keyword, rank_absolute, rank_group,
    prev_rank_absolute, is_new, is_up, is_down, serp_item_type, url,
    relative_url, title, etv, search_volume, cpc, competition,
    keyword_difficulty, intent, serp_features, monthly, raw}.

    `offset` pages through the list (max 1000 per call). `filters` is a
    DataForSEO Labs filter expression (applied server-side, so only matching
    keywords are returned — and billed); `total_count` then counts matches.
    """
    task = {
        "target": target,
        "location_code": location_code,
        "language_code": language_code,
        "limit": max(1, min(int(limit), 1000)),
        "item_types": ["organic"],
        "load_rank_absolute": True,
        "order_by": ["ranked_serp_element.serp_item.etv,desc"],
    }
    if offset:
        task["offset"] = int(offset)
    if filters:
        task["filters"] = filters
    task_obj = _post("/v3/dataforseo_labs/google/ranked_keywords/live", task, env)
    res0 = (task_obj.get("result") or [{}])[0] or {}

    items: list[dict] = []
    for it in res0.get("items") or []:
        kd = it.get("keyword_data") or {}
        info = kd.get("keyword_info") or {}
        props = kd.get("keyword_properties") or {}
        intent = (kd.get("search_intent_info") or {}).get("main_intent")
        el = it.get("ranked_serp_element") or {}
        serp = el.get("serp_item") or {}
        changes = serp.get("rank_changes") or {}
        keyword = (kd.get("keyword") or "").strip()
        if not keyword:
            continue
        items.append({
            "keyword": keyword[:255],
            "rank_absolute": serp.get("rank_absolute"),
            "rank_group": serp.get("rank_group"),
            "prev_rank_absolute": changes.get("previous_rank_absolute"),
            "is_new": bool(changes.get("is_new")),
            "is_up": bool(changes.get("is_up")),
            "is_down": bool(changes.get("is_down")),
            "serp_item_type": serp.get("type"),
            "url": serp.get("url"),
            "relative_url": serp.get("relative_url"),
            "title": serp.get("title"),
            "etv": _num(serp.get("etv")),
            "search_volume": info.get("search_volume"),
            "cpc": _num(info.get("cpc")),
            "competition": _num(info.get("competition")),
            "keyword_difficulty": el.get("keyword_difficulty") or props.get("keyword_difficulty"),
            "intent": intent,
            "serp_features": el.get("serp_item_types") or [],
            "monthly": info.get("monthly_searches") or [],
            # Compact raw: the ranked element (keyword_data is large and mostly
            # captured in the columns above).
            "raw": {"ranked_serp_element": el},
        })
    return {
        "total_count": res0.get("total_count"),
        "items_count": res0.get("items_count") or len(items),
        "cost": _num(task_obj.get("cost")),
        "meta": {k: v for k, v in res0.items() if k != "items"},
        "items": items,
    }


# ── Backlinks API (profile + link gap) ──────────────────────────────────────
# $0.024 per request + $0.000036 per returned row (live); the sandbox is free.
# Every call returns {cost, total_count, items[]} with flat items.

def _bl(path: str, task: dict, env: str | None) -> tuple[dict, float]:
    t = _post(path, task, env)
    return (t.get("result") or [{}])[0] or {}, float(t.get("cost") or 0)


def backlinks_summary(target: str, *, env: str | None = None) -> dict:
    """Endpoint: backlinks/summary/live — totals for a domain (1 row)."""
    r, cost = _bl("/v3/backlinks/summary/live",
                  {"target": target, "include_subdomains": True, "rank_scale": "one_hundred"}, env)
    return {"cost": cost, "summary": r}


def backlinks_referring_domains(target: str, *, limit: int, env: str | None = None) -> dict:
    """Endpoint: backlinks/referring_domains/live — strongest linking domains first."""
    r, cost = _bl("/v3/backlinks/referring_domains/live",
                  {"target": target, "limit": limit, "order_by": ["rank,desc"],
                   "include_subdomains": True, "rank_scale": "one_hundred"}, env)
    items = [{
        "domain": i.get("domain"), "rank": i.get("rank"), "backlinks": i.get("backlinks"),
        "nofollow_backlinks": i.get("referring_pages_nofollow"),
        "spam_score": i.get("backlinks_spam_score"),
        "first_seen": i.get("first_seen"), "lost_date": i.get("lost_date"),
    } for i in r.get("items") or [] if i.get("domain")]
    return {"cost": cost, "total_count": r.get("total_count"), "items": items}


def backlinks_list(target: str, *, limit: int, env: str | None = None) -> dict:
    """Endpoint: backlinks/backlinks/live — one (strongest) link per referring domain."""
    r, cost = _bl("/v3/backlinks/backlinks/live",
                  {"target": target, "limit": limit, "mode": "one_per_domain",
                   "order_by": ["rank,desc"], "include_subdomains": True, "rank_scale": "one_hundred"}, env)
    items = [{
        "url_from": i.get("url_from"), "domain_from": i.get("domain_from"), "url_to": i.get("url_to"),
        "anchor": i.get("anchor"), "item_type": i.get("item_type"), "dofollow": i.get("dofollow"),
        "is_new": bool(i.get("is_new")), "is_lost": bool(i.get("is_lost")),
        "is_broken": bool(i.get("is_broken")),
        "domain_from_rank": i.get("domain_from_rank"), "page_from_rank": i.get("page_from_rank"),
        "page_from_title": i.get("page_from_title"), "spam_score": i.get("backlink_spam_score"),
        "first_seen": i.get("first_seen"), "last_seen": i.get("last_seen"),
    } for i in r.get("items") or [] if i.get("url_from")]
    return {"cost": cost, "total_count": r.get("total_count"), "items": items}


def backlinks_anchors(target: str, *, limit: int, env: str | None = None) -> dict:
    """Endpoint: backlinks/anchors/live — most-used anchor texts."""
    r, cost = _bl("/v3/backlinks/anchors/live",
                  {"target": target, "limit": limit, "order_by": ["backlinks,desc"],
                   "include_subdomains": True, "rank_scale": "one_hundred"}, env)
    items = [{
        "anchor": i.get("anchor") or "", "backlinks": i.get("backlinks"),
        "referring_domains": i.get("referring_domains"),
        "referring_domains_nofollow": i.get("referring_domains_nofollow"),
        "rank": i.get("rank"), "spam_score": i.get("backlinks_spam_score"),
        "first_seen": i.get("first_seen"),
    } for i in r.get("items") or [] if i.get("anchor") is not None]
    return {"cost": cost, "total_count": r.get("total_count"), "items": items}


def backlinks_gap(competitors: list[str], exclude: str, *, limit: int,
                  env: str | None = None) -> dict:
    """Endpoint: backlinks/domain_intersection/live — domains linking to at least
    one competitor but not to `exclude` (your domain). Each item:
    {domain, rank, competitors_linking, links: {competitor: backlinks}, spam_score}.
    In each intersection entry DataForSEO's `target` is the REFERRING domain."""
    targets = {str(n): c for n, c in enumerate(competitors[:20], start=1)}
    r, cost = _bl("/v3/backlinks/domain_intersection/live",
                  {"targets": targets, "exclude_targets": [exclude], "limit": limit,
                   "intersection_mode": "partial", "include_subdomains": True, "rank_scale": "one_hundred"}, env)
    items = []
    for it in r.get("items") or []:
        entries = {targets.get(k, k): v for k, v in (it.get("domain_intersection") or {}).items() if v}
        if not entries:
            continue
        first = next(iter(entries.values()))
        items.append({
            "domain": first.get("target"),
            "rank": max((e.get("rank") or 0) for e in entries.values()),
            "competitors_linking": len(entries),
            "links": {c: e.get("backlinks") for c, e in entries.items()},
            "spam_score": first.get("backlinks_spam_score"),
        })
    return {"cost": cost, "total_count": r.get("total_count"), "items": items}
