"""Weekly SERP snapshot job — grows the competitor SERP history (DataForSEO).

For each owned domain, fetch the organic SERP for its highest-priority keywords
and store a `serp_snapshots` + `serp_snapshot_items` row. This is what powers
Competitive Strategy (who ranks for our keywords) and the competitor-keyword
gap views over time.

Cost control (DataForSEO SERP is paid per keyword):
  * PRIORITY — tracked keywords first, then by search volume, then opportunity
    score. We spend on the keywords that matter most.
  * TTL DEDUP — a keyword with a snapshot newer than SERP_TTL_DAYS is skipped, so
    the same keyword is never re-fetched within the window (no double spend).
  * HARD CAP — at most SERP_MAX_PER_RUN keywords per run, across all domains.

Launched by the external scheduler, weekly:

    python -m app.jobs serp_sync --triggered-by scheduler
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.domain import Domain
from app.models.keyword_analysis import SerpSnapshot, SerpSnapshotItem
from app.models.workspace import Workspace
from app.services import data_env, dataforseo_client
from app.services.dataforseo_client import DataForSeoError

logger = logging.getLogger(__name__)

# --- Cost knobs -------------------------------------------------------------
SERP_TTL_DAYS = 7        # don't re-fetch a keyword whose SERP is younger than this
SERP_MAX_PER_RUN = 50    # hard cap on paid SERP calls per run (across all domains)
SERP_DEPTH = 10          # organic results depth per SERP (lower = cheaper)

# country_code -> DataForSEO location_name (SERP is location-scoped).
_LOCATION_NAMES = {
    "in": "India", "us": "United States", "gb": "United Kingdom",
    "ca": "Canada", "au": "Australia",
}


def _norm_host(host: str) -> str:
    h = re.sub(r"^https?://", "", (host or "").strip().lower()).split("/")[0]
    return h[4:] if h.startswith("www.") else h


def _target_domains(db: Session) -> list[Domain]:
    """Owned domains that have an opportunity keyword set to snapshot.

    Sandbox workspaces are skipped (they are for manual testing and must not
    eat the per-run cap) unless DATAFORSEO_FORCE_SANDBOX makes the whole run
    sandbox."""
    ids = db.execute(text("SELECT DISTINCT domain_id FROM opportunity_keywords")).scalars().all()
    if not ids:
        return []
    stmt = select(Domain).where(Domain.id.in_(ids)).order_by(Domain.created_at)
    if not settings.DATAFORSEO_FORCE_SANDBOX:
        stmt = stmt.join(Workspace, Workspace.id == Domain.workspace_id).where(
            Workspace.is_sandbox.is_(False))
    return list(db.execute(stmt).scalars())


def _priority_candidates(db: Session, domain: Domain, *, limit: int, env: str) -> list:
    """The domain's keywords ranked by priority, EXCLUDING any with a fresh SERP.

    Priority: tracked keywords first, then keywords we already rank for (GSC
    position — their SERP shows who we share the page with), then higher search
    volume, then higher opportunity score. The TTL filter is what prevents re-fetching the same
    keyword within SERP_TTL_DAYS."""
    if limit <= 0:
        return []
    return db.execute(text(
        """
        SELECT ok.keyword_id, ok.keyword, ok.search_volume, ok.opportunity_score,
               EXISTS (
                 SELECT 1 FROM tracked_keywords tk
                 WHERE tk.domain_id = ok.domain_id AND lower(tk.term) = lower(ok.keyword)
               ) AS is_tracked
        FROM opportunity_keywords ok
        WHERE ok.domain_id = :did
          AND ok.keyword_id IS NOT NULL
          AND NOT EXISTS (
                SELECT 1 FROM serp_snapshots s
                WHERE s.keyword_id = ok.keyword_id
                  AND s.source = 'dataforseo'
                  AND s.environment = :env
                  AND s.captured_at > now() - make_interval(days => :ttl)
          )
        ORDER BY is_tracked DESC,
                 (ok.current_position IS NOT NULL) DESC,
                 ok.search_volume DESC NULLS LAST,
                 ok.opportunity_score DESC NULLS LAST
        LIMIT :lim
        """
    ), {"did": str(domain.id), "ttl": SERP_TTL_DAYS, "lim": limit, "env": env}).all()


def _store_snapshot(db: Session, domain: Domain, keyword_id, keyword: str,
                    country: str, language: str, env: str) -> int | None:
    """Fetch + store one keyword's SERP. Returns our own rank (or None). Raises
    DataForSeoError on a provider failure (the caller decides whether to abort)."""
    location_name = _LOCATION_NAMES.get(country, "United States")
    serp = dataforseo_client.serp_organic(
        keyword, location_name=location_name, language_code=language, depth=SERP_DEPTH,
        env=env,
    )
    if not serp:
        return None
    snap = SerpSnapshot(
        keyword_id=keyword_id, search_engine="google", source="dataforseo",
        environment=env, device="desktop", country_code=country, language_code=language,
        items_count=len(serp), status="ok",
    )
    snap.items = [
        SerpSnapshotItem(
            type="organic", rank_absolute=h["position"], rank_group=h["position"],
            url=h["url"], domain=h["domain"] or "", title=h.get("title"),
        )
        for h in serp
    ]
    db.add(snap)

    our_host = _norm_host(domain.domain)
    mine = [h["position"] for h in serp if _norm_host(h["domain"]) == our_host]
    our_pos = min(mine) if mine else None
    if our_pos is not None:
        # Keep our own rank fresh on the opportunity row (bonus, cheap).
        db.execute(text(
            "UPDATE opportunity_keywords SET current_position = :p "
            "WHERE domain_id = :d AND keyword_id = :k"
        ), {"p": our_pos, "d": str(domain.id), "k": str(keyword_id)})
    return our_pos


def run(db: Session, *, days: int = 28, country: str = "in", language: str = "en") -> dict:
    """Snapshot the top-priority, stale-or-missing keywords across owned domains,
    capped at SERP_MAX_PER_RUN. `days` is accepted (job CLI contract) but unused."""
    if not settings.SERP_SYNC_ENABLED:
        return {"skipped": "Paid SERP sync is off (SERP_SYNC_ENABLED=false)", "failed": 0,
                "fetched": 0, "domains_total": 0}
    if not dataforseo_client.is_configured():
        return {"skipped": "DataForSEO not configured", "failed": 0,
                "fetched": 0, "domains_total": 0}

    domains = _target_domains(db)
    remaining = SERP_MAX_PER_RUN
    fetched = 0
    failed = 0
    results: list[dict] = []
    aborted: str | None = None

    for domain in domains:
        if remaining <= 0:
            break
        env = data_env.env_for_domain(db, domain)
        cands = _priority_candidates(db, domain, limit=remaining, env=env)
        dom_fetched = 0
        for c in cands:
            if remaining <= 0:
                break
            try:
                _store_snapshot(db, domain, c.keyword_id, c.keyword, country, language, env)
                db.commit()
                fetched += 1
                dom_fetched += 1
                remaining -= 1
            except DataForSeoError as exc:
                db.rollback()
                failed += 1
                # An auth / verification / balance error hits every call — stop the
                # whole run rather than burn attempts (and money) on each keyword.
                aborted = f"{type(exc).__name__}: {exc}"
                logger.warning("serp_sync aborting after provider error: %s", exc)
                break
            except Exception:  # noqa: BLE001 - one bad keyword must not stop the job
                db.rollback()
                failed += 1
                logger.exception("serp_sync failed for keyword %r", c.keyword)
        results.append({"domain": domain.domain, "fetched": dom_fetched,
                        "candidates": len(cands)})
        if aborted:
            break

    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "country": country,
        "language": language,
        "ttl_days": SERP_TTL_DAYS,
        "cap_per_run": SERP_MAX_PER_RUN,
        "domains_total": len(domains),
        "fetched": fetched,
        "failed": failed,
        "aborted": aborted,
        "domains": results,
    }
