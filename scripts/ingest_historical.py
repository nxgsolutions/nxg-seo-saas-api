"""Ingest REAL historical data from DataForSEO — Phase 1/2 of the roadmap.

See docs/HISTORICAL_DATA_ROADMAP.md. LOCAL first, then promoted to the server.

What it writes (idempotent):
- Google Ads search_volume (live) -> 12-month monthly volume + cpc + competition
  -> one `keyword_metrics` row per keyword per month (upsert on metric_date).
- --serp: live SERP per keyword -> `serp_snapshots` (+ items) + SERP difficulty
  -> updates `opportunity_keywords` position / competitors / ranking_url / KD.
- Updates `opportunity_keywords` search_volume + cpc (latest month) and
  recomputes opportunity_score, so the Opportunity Research page goes real.

Usage:
  python -m scripts.ingest_historical --domain iaspirants.com --location India --lang en --serp
"""
from __future__ import annotations

import argparse
from datetime import date

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword
from app.models.keyword_analysis import KeywordMetric, SerpSnapshot, SerpSnapshotItem
from app.services import dataforseo_client as dfs
from app.services import keyword_planner_service as kp
from app.services import keyword_scoring as ks

# Google Ads returns month NAMES (e.g. "SEPTEMBER"); map to numbers.
_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


def _month_first(year: int, month: int) -> date:
    return date(year, month, 1)


def _serp_difficulty(keyword: str, snap: dict) -> tuple[int | None, int | None, str | None, list]:
    """Return (difficulty, my_position, my_url, competitors) from a SERP snapshot dict."""
    items = snap.get("items") or []
    kw_low = keyword.lower()
    distinct_domains = len({i["domain"] for i in items if i.get("domain")})
    exact_titles = sum(1 for i in items if kw_low in (i.get("title") or "").lower())
    difficulty = ks.estimate_difficulty(
        results_count=snap.get("se_results_count"),
        exact_title_matches=exact_titles,
        distinct_domains=distinct_domains,
    )
    return difficulty, distinct_domains, exact_titles, items


def run(domain_host: str, location: str, lang: str, do_serp: bool) -> None:
    db = SessionLocal()
    country = "in" if location.lower() == "india" else "us"
    try:
        domains = db.execute(
            select(Domain).where(Domain.domain.ilike(domain_host))
        ).scalars().all()
        if not domains:
            print(f"no domain matches {domain_host!r}")
            return

        # Drive off the domain's existing opportunity keywords (the real set).
        okws = db.execute(
            select(OpportunityKeyword).where(
                OpportunityKeyword.domain_id.in_([d.id for d in domains])
            )
        ).scalars().all()
        # keyword string -> a representative keyword_id (global keyword row)
        kw_to_id: dict[str, object] = {}
        for o in okws:
            if o.keyword and o.keyword.lower() not in kw_to_id and o.keyword_id:
                kw_to_id[o.keyword.lower()] = o.keyword_id
        keywords = sorted(kw_to_id.keys())
        if not keywords:
            print("no opportunity keywords with keyword_id found for this domain")
            return
        print(f"{len(keywords)} keywords, {len(domains)} domain rows, location={location}")

        # ---- 1) Historical monthly volume — DIRECT Google Ads (free), not DataForSEO ----
        ga_rows = kp.historical_metrics(keywords, country=country, language=lang)
        latest: dict[str, dict] = {}  # keyword -> {volume, cpc, comp_idx}
        metric_rows = 0
        for item in ga_rows:
            kw = (item.get("keyword") or "").strip().lower()
            kid = kw_to_id.get(kw)
            if not kid:
                continue
            cpc = item.get("cpc_high") or item.get("cpc_low")
            comp_idx = item.get("competition_index")
            months = item.get("monthly") or []
            latest[kw] = {"volume": item.get("avg_monthly_searches"), "cpc": cpc, "comp": comp_idx}
            for m in months:
                if not m.get("year") or not m.get("month"):
                    continue
                md = _month_first(int(m["year"]), _MONTHS[m["month"]])
                existing = db.execute(
                    select(KeywordMetric).where(
                        KeywordMetric.keyword_id == kid,
                        KeywordMetric.country_code == country,
                        KeywordMetric.language_code == lang,
                        KeywordMetric.search_engine == "google",
                        KeywordMetric.device == "desktop",
                        KeywordMetric.metric_date == md,
                    )
                ).scalar_one_or_none()
                row = existing or KeywordMetric(
                    keyword_id=kid, country_code=country, language_code=lang,
                    search_engine="google", device="desktop", metric_date=md,
                )
                row.search_volume = m.get("searches")
                row.cpc = cpc
                row.competition = comp_idx
                if existing is None:
                    db.add(row)
                metric_rows += 1
        db.flush()
        print(f"  keyword_metrics upserted: {metric_rows} monthly rows")

        # ---- 2) Optional live SERP -> difficulty, rank, competitors ----
        serp_by_kw: dict[str, dict] = {}
        if do_serp:
            for kw in keywords:
                try:
                    snap = dfs.serp_organic_snapshot(
                        kw, location_name=location, language_code=lang, depth=20
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  SERP fail {kw!r}: {str(e)[:80]}")
                    continue
                serp_by_kw[kw] = snap
                # persist the snapshot (fresh history point)
                kid = kw_to_id.get(kw)
                ss = SerpSnapshot(
                    keyword_id=kid, search_engine="google", source="dataforseo",
                    device="desktop", country_code=country, language_code=lang,
                    location_code=snap.get("location_code"), depth=20,
                    items_count=snap.get("items_count"),
                    se_results_count=snap.get("se_results_count"),
                    check_url=snap.get("check_url"), status="ok", raw=snap.get("raw"),
                )
                db.add(ss)
                db.flush()
                for it in snap.get("items", []):
                    db.add(SerpSnapshotItem(
                        snapshot_id=ss.id, rank_absolute=it["rank_absolute"],
                        rank_group=it.get("rank_group"), type=it.get("type"),
                        domain=it.get("domain"), url=it.get("url"),
                        title=it.get("title"), description=it.get("description"),
                        breadcrumb=it.get("breadcrumb"), raw=it.get("raw"),
                    ))
            db.flush()
            print(f"  SERP snapshots stored: {len(serp_by_kw)}")

        # ---- 3) Update opportunity_keywords (real volume / cpc / KD / rank) ----
        updated = 0
        for o in okws:
            kw = (o.keyword or "").lower()
            lat = latest.get(kw)
            if lat:
                if lat["volume"] is not None:
                    o.search_volume = int(lat["volume"])
                if lat["cpc"] is not None:
                    o.cpc = float(lat["cpc"])
            snap = serp_by_kw.get(kw)
            position = o.current_position
            if snap:
                difficulty, _, _, items = _serp_difficulty(o.keyword, snap)
                if difficulty is not None:
                    o.keyword_difficulty = int(difficulty)
                host = o.ranking_url  # keep existing if not found below
                mine = next((i for i in items if o.keyword and
                             (i.get("domain") or "").endswith(domain_host)), None)
                if mine:
                    o.current_position = mine["rank_absolute"]
                    position = mine["rank_absolute"]
                    o.ranking_url = mine["url"]
                    o.competitors = [
                        {"domain": i["domain"], "position": i["rank_absolute"]}
                        for i in items if i.get("domain") and not (i.get("domain") or "").endswith(domain_host)
                    ][:8]
                else:
                    o.current_position = None  # domain not in top results now
            # recompute opportunity score with real demand (volume) + position + KD
            o.opportunity_score = int(round(ks.opportunity_score(
                impressions=o.search_volume, position=o.current_position,
                difficulty=o.keyword_difficulty or None,
            )))
            updated += 1
        db.commit()
        print(f"  opportunity_keywords updated: {updated}")
        print("DONE")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--location", default="India")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--serp", action="store_true", help="also pull live SERP (costs ~$0.002/kw)")
    a = ap.parse_args()
    run(a.domain, a.location, a.lang, a.serp)
