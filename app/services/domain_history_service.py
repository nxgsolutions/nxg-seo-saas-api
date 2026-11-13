"""Monthly whole-site history — fetch from DataForSEO Labs, store, read back.

A refresh makes ONE historical_bulk_traffic_estimation call (monthly organic ETV
for every target at once) plus ONE historical_rank_overview call per target
(monthly ranking-keyword count + position distribution), then upserts one
`domain_monthly_metrics` row per (target, location, language, year, month).

Reads come from Postgres, so page loads never hit the paid API; a refresh skips
targets whose monthly history is still fresh (within the cache TTL) unless
forced. This fills the Competitive Strategy performance graph with real 12-month
history, replacing the daily single-snapshot path for that chart.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.domain_monthly_metrics import DomainMonthlyMetric
from app.services import data_env, dataforseo_client
from app.services.dataforseo_client import DataForSeoError

logger = logging.getLogger(__name__)

# Months of history requested per refresh, and DataForSEO's hard floor.
MONTHS = 12
_HISTORY_FLOOR = date(2020, 10, 1)
# Bounds per-refresh spend (1 bulk call + 1 rank-overview call per domain) and
# concurrency — mirrors domain_analytics_service.
MAX_TARGETS = 20
_WORKERS = 4

_POS_KEYS = ("pos_1", "pos_2_3", "pos_4_10", "pos_11_20", "pos_21_30")


def date_from(months: int = MONTHS) -> str:
    """yyyy-mm-dd for the first day of the month `months` back (clamped to floor)."""
    today = date.today()
    total = today.year * 12 + (today.month - 1) - (months - 1)
    y, m = divmod(total, 12)
    start = date(y, m + 1, 1)
    return max(start, _HISTORY_FLOOR).isoformat()


def history(
    db: Session, targets: list[str], *, location_code: int, language: str,
    env: str = data_env.LIVE,
) -> dict[str, list[DomainMonthlyMetric]]:
    """Every stored monthly row per target in `env`, oldest-first (for charts)."""
    if not targets:
        return {}
    rows = db.execute(
        select(DomainMonthlyMetric)
        .where(
            DomainMonthlyMetric.environment == env,
            DomainMonthlyMetric.target.in_(targets),
            DomainMonthlyMetric.location_code == location_code,
            DomainMonthlyMetric.language_code == language,
        )
        .order_by(DomainMonthlyMetric.target,
                  DomainMonthlyMetric.year.asc(), DomainMonthlyMetric.month.asc())
    ).scalars().all()
    out: dict[str, list[DomainMonthlyMetric]] = {}
    for r in rows:
        out.setdefault(r.target, []).append(r)
    return out


def _last_fetched(
    db: Session, targets: list[str], *, location_code: int, language: str, env: str,
) -> dict[str, datetime]:
    """target -> most recent fetched_at (for TTL skip)."""
    rows = db.execute(
        select(DomainMonthlyMetric.target, func.max(DomainMonthlyMetric.fetched_at))
        .where(
            DomainMonthlyMetric.environment == env,
            DomainMonthlyMetric.target.in_(targets),
            DomainMonthlyMetric.location_code == location_code,
            DomainMonthlyMetric.language_code == language,
        )
        .group_by(DomainMonthlyMetric.target)
    ).all()
    return {target: fetched for target, fetched in rows}


def refresh_monthly(
    db: Session, targets: list[str], *, location_code: int, language: str,
    force: bool = False, env: str = data_env.LIVE, months: int = MONTHS,
) -> dict:
    """Fetch + store `months` of monthly history for stale/missing targets.

    Returns {fetched, cached, failed: [domain], error}. Raises DataForSeoError
    only when credentials are missing; a partial outage still stores what it got.
    """
    if not dataforseo_client.is_configured():
        raise DataForSeoError("DataForSEO credentials are not configured.")

    targets = list(dict.fromkeys(t for t in targets if t))[:MAX_TARGETS]
    if not targets:
        return {"fetched": 0, "cached": 0, "failed": [], "error": None}

    if force:
        stale = targets
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.PROVIDER_CACHE_TTL_DAYS)
        seen = _last_fetched(db, targets, location_code=location_code, language=language, env=env)
        stale = [t for t in targets if t not in seen or seen[t] < cutoff]
    cached = len(targets) - len(stale)
    if not stale:
        return {"fetched": 0, "cached": cached, "failed": [], "error": None}

    start = date_from(months)

    # 1) One bulk call → monthly organic ETV + count for every stale target.
    try:
        traffic = dataforseo_client.historical_bulk_traffic_estimation(
            stale, location_code=location_code, language_code=language,
            date_from=start, env=env)
    except DataForSeoError as exc:
        logger.warning("DataForSEO historical bulk traffic failed: %s", exc)
        traffic = {}

    # 2) One rank-overview call per target → ranking-keyword count + positions.
    def fetch_rank(target: str):
        try:
            return target, dataforseo_client.historical_rank_overview(
                target, location_code=location_code, language_code=language,
                date_from=start, env=env), None
        except DataForSeoError as exc:
            return target, None, str(exc)

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        rank_results = list(pool.map(fetch_rank, stale))
    rank_by_target = {t: (r or []) for t, r, _err in rank_results}
    first_error = next((err for _t, _r, err in rank_results if err), None)

    now = datetime.now(timezone.utc)
    source = "dataforseo_sandbox" if env == data_env.SANDBOX else "dfs_historical"
    stored: set[str] = set()
    for t in stale:
        # Merge the two sources per (year, month). Rank overview is authoritative
        # for count + positions; bulk traffic is authoritative for etv and fills
        # any month the rank endpoint lacks.
        merged: dict[tuple[int, int], dict] = {}
        for m in traffic.get(t, []):
            merged[(m["year"], m["month"])] = {
                "organic_etv": m["etv"], "organic_count": m["count"]}
        for m in rank_by_target.get(t, []):
            row = merged.setdefault((m["year"], m["month"]), {})
            row["organic_count"] = m["count"]
            row.setdefault("organic_etv", m["etv"])
            row.update({k: m[k] for k in _POS_KEYS})
        if not merged:
            continue
        for (year, month), vals in merged.items():
            values = {
                "environment": env, "target": t,
                "location_code": location_code, "language_code": language,
                "year": year, "month": month, "source": source,
                "organic_etv": float(vals.get("organic_etv") or 0),
                "organic_count": int(vals.get("organic_count") or 0),
                "fetched_at": now,
                **{k: vals.get(k) for k in _POS_KEYS},
            }
            stmt = insert(DomainMonthlyMetric).values(**values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_domain_monthly_metric",
                set_={k: stmt.excluded[k] for k in values
                      if k not in ("environment", "target", "location_code",
                                   "language_code", "year", "month")},
            )
            db.execute(stmt)
        stored.add(t)
    db.commit()

    failed = [t for t in stale if t not in stored]
    if failed:
        logger.warning("DataForSEO monthly history: no data for %s (%s)", failed, first_error)
    return {"fetched": len(stored), "cached": cached, "failed": failed, "error": first_error}
