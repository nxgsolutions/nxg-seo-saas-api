"""Budget-capped bulk historical pull from DataForSEO — spends up to --budget.

Order of value:
  1) Google Ads search_volume for ALL keywords (12-month history) — cheap, one call.
  2) live SERP (depth 20) for the domain's opportunity keywords, highest volume
     first, until the running cost would exceed --budget. Fills rank + competitors
     + real difficulty.

Tracks DataForSEO's actual `cost` per response and STOPS before the cap, leaving a
buffer so no call fails on insufficient funds. Commits progress as it goes.

Usage:
  python -m scripts.pull_all_historical --domain iaspirants.com --location India --lang en \
         --budget 0.60 --kw-like upsc,ias,civil
"""
from __future__ import annotations

import argparse
from datetime import date

import httpx
from sqlalchemy import select, text

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword
from app.models.keyword_analysis import Keyword, KeywordMetric, SerpSnapshot, SerpSnapshotItem
from app.services import keyword_relevance
from app.services import keyword_scoring as ks

AUTH = (settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD)
BASE = "https://api.dataforseo.com"


def _post(path: str, task: dict) -> dict:
    r = httpx.post(f"{BASE}{path}", json=[task], auth=AUTH, timeout=120)
    r.raise_for_status()
    return (r.json().get("tasks") or [{}])[0]


def _mfirst(y: int, m: int) -> date:
    return date(y, m, 1)


def run(domain_host: str, location: str, lang: str, budget: float, kw_like: list[str]) -> None:
    db = SessionLocal()
    country = "in" if location.lower() == "india" else "us"
    spent = 0.0
    try:
        domains = db.execute(select(Domain).where(Domain.domain.ilike(domain_host))).scalars().all()
        if not domains:
            print(f"no domain {domain_host!r}"); return
        dom_ids = [d.id for d in domains]

        # ---- 1) VOLUME for all keywords (one call, up to 700) ----
        all_kw = db.execute(select(Keyword.id, Keyword.keyword)).all()
        id_by_kw = {k.lower(): kid for kid, k in all_kw}
        kw_list = [k for _, k in all_kw][:700]
        t = _post("/v3/keywords_data/google_ads/search_volume/live",
                  {"keywords": kw_list, "location_name": location, "language_code": lang})
        spent += float(t.get("cost") or 0)
        latest_vol: dict[str, int] = {}
        rows = 0
        for it in (t.get("result") or []):
            kw = (it.get("keyword") or "").strip().lower()
            kid = id_by_kw.get(kw)
            if not kid:
                continue
            if it.get("search_volume") is not None:
                latest_vol[kw] = int(it["search_volume"])
            cpc = it.get("cpc"); comp = it.get("competition_index")
            for m in (it.get("monthly_searches") or []):
                md = _mfirst(int(m["year"]), int(m["month"]))
                existing = db.execute(select(KeywordMetric).where(
                    KeywordMetric.keyword_id == kid, KeywordMetric.country_code == country,
                    KeywordMetric.language_code == lang, KeywordMetric.search_engine == "google",
                    KeywordMetric.device == "desktop", KeywordMetric.metric_date == md,
                )).scalar_one_or_none()
                row = existing or KeywordMetric(
                    keyword_id=kid, country_code=country, language_code=lang,
                    search_engine="google", device="desktop", metric_date=md)
                row.search_volume = m.get("search_volume"); row.cpc = cpc; row.competition = comp
                if existing is None:
                    db.add(row)
                rows += 1
        db.commit()
        print(f"[1] volume: {len(latest_vol)} keywords, {rows} monthly rows, cost=${t.get('cost')} (spent ${spent:.4f})")

        # ---- ensure opportunity_keywords exist for the domain's relevant keywords ----
        like_sql = " OR ".join([f"keyword ILIKE :p{i}" for i in range(len(kw_like))]) or "TRUE"
        params = {f"p{i}": f"%{p}%" for i, p in enumerate(kw_like)}
        # Competitor brand ("drishti ias") and "near me" keywords are skipped: the
        # global keyword table also holds competitors' domain-search keywords, and
        # a pattern like '%ias%' would otherwise pull them into OUR list.
        comp_hosts = list(db.execute(text(
            "SELECT DISTINCT target FROM domain_ranked_keyword_runs")).scalars())
        for did in dom_ids:
            candidates = db.execute(text(f"""
                SELECT k.id, k.keyword FROM keywords k
                WHERE ({like_sql})
                  AND NOT EXISTS (SELECT 1 FROM opportunity_keywords o WHERE o.domain_id=:did AND o.keyword_id=k.id)
            """), {**params, "did": str(did)}).all()
            skipped = 0
            for kid, kw in candidates:
                if keyword_relevance.irrelevance_reason(kw, own_host=domain_host,
                                                        competitor_hosts=comp_hosts):
                    skipped += 1
                    continue
                db.execute(text("""
                    INSERT INTO opportunity_keywords
                      (id, domain_id, keyword_id, keyword, search_volume, yoy_change,
                       keyword_difficulty, current_position, intent, opportunity_score,
                       serp_features, competitors, country_code, created_at)
                    VALUES (gen_random_uuid(), :did, :kid, :kw, 0,0,0,NULL,'informational',0,
                            '[]'::jsonb,'[]'::jsonb,'in',now())
                """), {"did": str(did), "kid": str(kid), "kw": kw})
            print(f"  opportunity_keywords: +{len(candidates) - skipped}, skipped {skipped} irrelevant")
        db.commit()

        # set opportunity_keywords volume/cpc from latest month
        okws = db.execute(select(OpportunityKeyword).where(OpportunityKeyword.domain_id.in_(dom_ids))).scalars().all()
        for o in okws:
            v = latest_vol.get((o.keyword or "").lower())
            if v is not None:
                o.search_volume = v
        db.commit()

        # ---- 2) SERP depth-20, highest volume first, until budget ----
        # distinct keywords for the domain, by volume desc
        seen: dict[str, object] = {}
        for o in sorted(okws, key=lambda x: x.search_volume or 0, reverse=True):
            kwl = (o.keyword or "").lower()
            if kwl and kwl not in seen and o.keyword_id:
                seen[kwl] = o.keyword_id
        serp_done = 0
        SERP_EST = 0.004
        for kwl, kid in seen.items():
            if spent + SERP_EST > budget:
                print(f"[2] budget cap reached at ${spent:.4f}; stopping SERP")
                break
            try:
                t = _post("/v3/serp/google/organic/live/advanced",
                          {"keyword": kwl, "location_name": location, "language_code": lang, "depth": 20})
            except Exception as e:  # noqa: BLE001
                print(f"    SERP fail {kwl!r}: {str(e)[:60]}"); continue
            spent += float(t.get("cost") or 0)
            res = (t.get("result") or [{}])[0]
            raw_items = res.get("items") or []
            items = [i for i in raw_items if i.get("type") == "organic" and i.get("url")]
            # store snapshot
            ss = SerpSnapshot(keyword_id=kid, search_engine="google", source="dataforseo",
                              device="desktop", country_code=country, language_code=lang,
                              location_code=res.get("location_code"), depth=20,
                              items_count=res.get("items_count") or len(items),
                              se_results_count=res.get("se_results_count"),
                              check_url=res.get("check_url"), status="ok",
                              raw={k: v for k, v in res.items() if k != "items"})
            db.add(ss); db.flush()
            distinct_domains = len({i.get("domain") for i in items if i.get("domain")})
            exact = sum(1 for i in items if kwl in (i.get("title") or "").lower())
            for i in items:
                db.add(SerpSnapshotItem(
                    snapshot_id=ss.id, rank_absolute=int(i.get("rank_absolute") or i.get("rank_group") or 0),
                    rank_group=i.get("rank_group"), type="organic", domain=i.get("domain") or "",
                    url=i.get("url"), title=i.get("title"), description=i.get("description"),
                    breadcrumb=i.get("breadcrumb"), raw=i))
            difficulty = ks.estimate_difficulty(res.get("se_results_count"), exact, distinct_domains)
            mine = next((i for i in items if (i.get("domain") or "").endswith(domain_host)), None)
            comps = [{"domain": i.get("domain"), "position": int(i.get("rank_absolute") or 0)}
                     for i in items if i.get("domain") and not (i.get("domain") or "").endswith(domain_host)][:8]
            for o in okws:
                if (o.keyword or "").lower() != kwl:
                    continue
                if difficulty is not None:
                    o.keyword_difficulty = int(difficulty)
                o.current_position = int(mine["rank_absolute"]) if mine else None
                o.ranking_url = mine["url"] if mine else None
                o.competitors = comps
            serp_done += 1
            if serp_done % 20 == 0:
                db.commit()
                print(f"    ...{serp_done} SERPs, spent ${spent:.4f}")
        db.commit()
        print(f"[2] SERP snapshots: {serp_done} (spent ${spent:.4f})")

        # ---- recompute opportunity scores ----
        for o in okws:
            o.opportunity_score = int(round(ks.opportunity_score(
                impressions=o.search_volume, position=o.current_position,
                difficulty=o.keyword_difficulty or None)))
        db.commit()
        print(f"DONE. total spent ≈ ${spent:.4f}")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--location", default="India")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--budget", type=float, default=0.60)
    ap.add_argument("--kw-like", default="upsc,ias,civil service")
    a = ap.parse_args()
    run(a.domain, a.location, a.lang, a.budget, [s.strip() for s in a.kw_like.split(",") if s.strip()])
