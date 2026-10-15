"""Whole-site domain analytics — fetch from DataForSEO, store, read back.

Each refresh calls DataForSEO Labs `domain_rank_overview` (one paid call per
domain; the live endpoint takes a single task) plus one Backlinks `bulk_ranks`
call for all domains, then upserts one `domain_analytics_snapshots` row per
(domain, location, language, day). Reads always come from Postgres, so page
loads never hit the paid API; a refresh skips domains fetched within the cache
TTL unless forced.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.domain_analytics import DomainAnalyticsSnapshot
from app.services import data_env, dataforseo_client
from app.services.dataforseo_client import DataForSeoError

logger = logging.getLogger(__name__)

# Single source of truth for markets: country_code -> (DataForSEO location_code,
# DataForSEO location_name). Both the Labs/analytics (code) and SERP (name) paths
# resolve through this table so they can never drift. Add markets here only.
GEO_TARGETS: dict[str, tuple[int, str]] = {
    "us": (2840, "United States"),
    "in": (2356, "India"),
    "gb": (2826, "United Kingdom"),
    "ca": (2124, "Canada"),
    "au": (2036, "Australia"),
    "de": (2276, "Germany"),
    "fr": (2250, "France"),
    "ae": (2784, "United Arab Emirates"),
    "sg": (2702, "Singapore"),
    "es": (2724, "Spain"),
    "it": (2380, "Italy"),
    "nl": (2528, "Netherlands"),
    "br": (2076, "Brazil"),
    "jp": (2392, "Japan"),
    "nz": (2554, "New Zealand"),
    "za": (2710, "South Africa"),
    "ie": (2372, "Ireland"),
    "mx": (2484, "Mexico"),
}
# Unknown / off-list country -> a neutral global default (US), NOT the old
# India/UPSC default. A domain configured for India still resolves "in" here.
DEFAULT_COUNTRY = "us"

# Back-compat alias: country_code -> location_code only.
LOCATION_CODES = {cc: code for cc, (code, _name) in GEO_TARGETS.items()}

# Bounds the per-refresh spend (~$0.012 per domain) and concurrency.
MAX_TARGETS = 20
_WORKERS = 4


def location_code_for(country: str) -> int:
    code, _ = GEO_TARGETS.get((country or "").lower(), GEO_TARGETS[DEFAULT_COUNTRY])
    return code


def location_name_for(country: str) -> str:
    """DataForSEO location_name for a country code (SERP endpoints want a name)."""
    _, name = GEO_TARGETS.get((country or "").lower(), GEO_TARGETS[DEFAULT_COUNTRY])
    return name


def latest(
    db: Session, targets: list[str], *, location_code: int, language: str,
    env: str = data_env.LIVE,
) -> dict[str, DomainAnalyticsSnapshot]:
    """Newest stored snapshot per target in `env` (missing targets are absent)."""
    if not targets:
        return {}
    rows = db.execute(
        select(DomainAnalyticsSnapshot)
        .where(
            DomainAnalyticsSnapshot.environment == env,
            DomainAnalyticsSnapshot.target.in_(targets),
            DomainAnalyticsSnapshot.location_code == location_code,
            DomainAnalyticsSnapshot.language_code == language,
        )
        .order_by(DomainAnalyticsSnapshot.target, DomainAnalyticsSnapshot.fetched_at.desc())
        .distinct(DomainAnalyticsSnapshot.target)
    ).scalars().all()
    return {r.target: r for r in rows}


def _values(target: str, location_code: int, language: str,
            metrics: dict | None, rank: int | None, env: str,
            links: dict | None = None) -> dict:
    organic = (metrics or {}).get("organic") or {}
    paid = (metrics or {}).get("paid") or {}

    def i(d: dict, k: str) -> int:
        return int(d.get(k) or 0)

    return {
        "environment": env,
        "target": target,
        "location_code": location_code,
        "language_code": language,
        "fetched_on": date.today(),
        "source": "dataforseo_sandbox" if env == data_env.SANDBOX else "dataforseo_labs",
        "organic_etv": float(organic.get("etv") or 0),
        "organic_count": i(organic, "count"),
        "pos_1": i(organic, "pos_1"),
        "pos_2_3": i(organic, "pos_2_3"),
        "pos_4_10": i(organic, "pos_4_10"),
        "pos_11_20": i(organic, "pos_11_20"),
        "is_new": i(organic, "is_new"),
        "is_up": i(organic, "is_up"),
        "is_down": i(organic, "is_down"),
        "is_lost": i(organic, "is_lost"),
        "traffic_cost": float(organic.get("estimated_paid_traffic_cost") or 0),
        "paid_etv": float(paid.get("etv") or 0),
        "paid_count": i(paid, "count"),
        "domain_rank": rank,
        "backlinks": (links or {}).get("backlinks"),
        "referring_domains": (links or {}).get("referring_domains"),
        # Full provider payload so new fields can be read later for free.
        "raw": {"metrics": metrics, "domain_rank": rank, "backlinks": links},
        "fetched_at": datetime.now(timezone.utc),
    }


def _backfill_backlinks(db: Session, snaps: list[DomainAnalyticsSnapshot], env: str) -> None:
    """Fill backlinks / referring_domains on existing snapshots (best-effort)."""
    if not snaps:
        return
    try:
        links = dataforseo_client.bulk_backlinks([s.target for s in snaps], env=env)
    except DataForSeoError as exc:
        logger.warning("DataForSEO bulk_backlinks backfill failed: %s", exc)
        return
    for s in snaps:
        got = links.get(s.target)
        if got:
            s.backlinks = got.get("backlinks")
            s.referring_domains = got.get("referring_domains")
    db.commit()


def refresh(
    db: Session, targets: list[str], *, location_code: int, language: str,
    force: bool = False, env: str = data_env.LIVE,
) -> dict:
    """Fetch + store metrics for `targets` whose stored data is stale.

    Returns {fetched, cached, failed: [domain], error}. Raises DataForSeoError
    only when credentials are missing or EVERY fetch failed, so a partial
    outage still stores what it could.
    """
    if not dataforseo_client.is_configured():
        raise DataForSeoError("DataForSEO credentials are not configured.")

    targets = list(dict.fromkeys(t for t in targets if t))[:MAX_TARGETS]
    if force:
        stale = targets
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.PROVIDER_CACHE_TTL_DAYS)
        have = latest(db, targets, location_code=location_code, language=language, env=env)
        stale = [t for t in targets if t not in have or have[t].fetched_at < cutoff]
    cached = len(targets) - len(stale)
    if not force:
        # Fresh rows stored before backlinks existed: one cheap bulk call fills
        # them in, instead of re-buying the whole overview.
        _backfill_backlinks(
            db, [have[t] for t in targets if t not in stale and have[t].backlinks is None], env)
    if not stale:
        return {"fetched": 0, "cached": cached, "failed": [], "error": None}

    def fetch(target: str):
        try:
            return target, dataforseo_client.domain_rank_overview(
                target, location_code=location_code, language_code=language, env=env), None
        except DataForSeoError as exc:
            return target, None, str(exc)

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(fetch, stale))

    ok = [(t, m) for t, m, err in results if err is None]
    failed = [t for t, _m, err in results if err is not None]
    first_error = next((err for _t, _m, err in results if err), None)
    if not ok:
        raise DataForSeoError(first_error or "DataForSEO returned no data.")

    # Domain rank + backlinks are nice-to-haves (Backlinks API); never fail the
    # refresh on them.
    try:
        ranks = dataforseo_client.bulk_domain_ranks([t for t, _m in ok], env=env)
    except DataForSeoError as exc:
        logger.warning("DataForSEO bulk_ranks failed: %s", exc)
        ranks = {}
    try:
        links = dataforseo_client.bulk_backlinks([t for t, _m in ok], env=env)
    except DataForSeoError as exc:
        logger.warning("DataForSEO bulk_backlinks failed: %s", exc)
        links = {}

    for target, metrics in ok:
        values = _values(target, location_code, language, metrics, ranks.get(target), env,
                         links.get(target))
        stmt = insert(DomainAnalyticsSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_domain_analytics_day",
            set_={k: stmt.excluded[k] for k in values
                  if k not in ("environment", "target", "location_code",
                               "language_code", "fetched_on")},
        )
        db.execute(stmt)
    db.commit()

    if failed:
        logger.warning("DataForSEO domain overview failed for %s: %s", failed, first_error)
    return {"fetched": len(ok), "cached": cached, "failed": failed, "error": first_error}
