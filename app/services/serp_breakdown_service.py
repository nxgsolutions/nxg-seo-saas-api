"""Keyword Breakdown — the stored Google SERP for a keyword, enriched per result.

DB only (zero cost): reads the latest `serp_snapshots` row for the keyword and
joins what we already store about each ranking domain:
  - domain metrics (`domain_analytics_snapshots`): authority (DataForSEO
    Backlinks domain rank, stored on the 0-100 scale), backlinks, referring
    domains, site traffic;
  - visits for THIS keyword: the DataForSEO Labs estimate when that domain's
    ranked keywords are stored (`domain_ranked_keywords.etv`), otherwise our
    own estimate = search volume x CTR(position) (same CTR curve as the action
    plan). Each value says which source it came from.
Nothing here calls a paid API; refreshing the SERP is the separate
POST /serp/analyze?refresh=true.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.workspace import Workspace
from app.services import data_env, domain_analytics_service, ranked_keywords_service, serp_scrape_service
from app.services.opportunity_service import _ctr_from_position


def _metrics(db: Session, keyword: str, country: str) -> dict:
    row = db.execute(text("""
        SELECT m.search_volume, m.cpc, m.difficulty, m.competition, m.intent, m.metric_date
        FROM keywords k JOIN keyword_metrics m ON m.keyword_id = k.id
        WHERE lower(k.keyword) = lower(:kw) AND m.country_code = :c
        ORDER BY (m.search_volume IS NULL), m.metric_date DESC
        LIMIT 1
    """), {"kw": keyword, "c": country}).mappings().first()
    if not row:
        return {"search_volume": None, "cpc": None, "difficulty": None, "competition": None,
                "intent": None, "metric_date": None}
    return {
        "search_volume": row["search_volume"],
        "cpc": float(row["cpc"]) if row["cpc"] is not None else None,
        "difficulty": float(row["difficulty"]) if row["difficulty"] is not None else None,
        "competition": float(row["competition"]) if row["competition"] is not None else None,
        "intent": row["intent"],
        "metric_date": row["metric_date"].isoformat() if row["metric_date"] else None,
    }


def _ranked_etv(db: Session, keyword: str, hosts: list[str], loc: int, env: str) -> dict[str, float]:
    """Labs' per-keyword visit estimate for each domain, from its latest stored run."""
    if not hosts:
        return {}
    rows = db.execute(text("""
        SELECT DISTINCT ON (r.target) r.target, k.etv
        FROM domain_ranked_keywords k
        JOIN domain_ranked_keyword_runs r ON r.id = k.run_id
        WHERE r.environment = :env AND r.location_code = :loc
          AND lower(k.keyword) = lower(:kw) AND r.target = ANY(:hosts) AND k.etv IS NOT NULL
        ORDER BY r.target, r.fetched_at DESC
    """), {"env": env, "loc": loc, "kw": keyword, "hosts": hosts}).all()
    return {r.target: float(r.etv) for r in rows}


def build(db: Session, workspace: Workspace, keyword: str, *, country: str = "in",
          language: str = "en") -> dict:
    env = data_env.env_for_workspace(workspace)
    loc = domain_analytics_service.location_code_for(country)
    keyword = " ".join(keyword.split())
    metrics = _metrics(db, keyword, country)
    snap = serp_scrape_service.latest_snapshot(db, keyword, country=country, language=language, env=env)

    ours = {ranked_keywords_service.norm_host(d) for d in db.execute(
        text("SELECT domain FROM domains WHERE workspace_id = :w"), {"w": str(workspace.id)}).scalars()}

    raw_rows = snap["rows"] if snap else []
    hosts = sorted({ranked_keywords_service.norm_host(r["domain"]) for r in raw_rows if r.get("domain")})
    analytics = domain_analytics_service.latest(db, hosts, location_code=loc, language=language, env=env)
    etv = _ranked_etv(db, keyword, hosts, loc, env)
    volume = metrics["search_volume"]

    rows = []
    for r in raw_rows:
        host = ranked_keywords_service.norm_host(r["domain"])
        a = analytics.get(host)
        if host in etv:
            visits, visits_source = round(etv[host]), "dataforseo"
        elif volume and r.get("type", "organic") == "organic":
            visits, visits_source = round(volume * _ctr_from_position(r["position"]) / 100), "estimate"
        else:
            visits, visits_source = None, None
        rows.append({
            **r,
            "host": host,
            "is_you": host in ours,
            "visits": visits,
            "visits_source": visits_source,
            "authority": a.domain_rank if a else None,
            "backlinks": a.backlinks if a else None,
            "referring_domains": a.referring_domains if a else None,
            "site_traffic": round(a.organic_etv) if a else None,
            "site_keywords": a.organic_count if a else None,
        })

    top10 = [r for r in rows if r["position"] <= 10]
    auth = [r["authority"] for r in top10 if r["authority"] is not None]
    links = [r["backlinks"] for r in top10 if r["backlinks"] is not None]
    return {
        "keyword": keyword,
        "country": country,
        "language": language,
        "environment": env,
        "metrics": metrics,
        "captured_at": snap["captured_at"] if snap else None,
        "source": snap["source"] if snap else None,
        "se_results_count": snap["se_results_count"] if snap else None,
        "check_url": snap["check_url"] if snap else None,
        "summary": {
            "top10_with_metrics": len(auth),
            "avg_authority": round(sum(auth) / len(auth)) if auth else None,
            "avg_backlinks": round(sum(links) / len(links)) if links else None,
            "your_position": min((r["position"] for r in rows if r["is_you"]), default=None),
        },
        "rows": rows,
    }
