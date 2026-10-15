#!/usr/bin/env python3
"""Import a DataForSEO-shaped SERP JSON into serp_snapshots + serp_snapshot_items.

Accepts the JSON produced by scraping/serp_snapshot.py (or a real DataForSEO
Google Organic response — same shape) and writes ONE serp_snapshots fetch-event
row plus its serp_snapshot_items rows. The keyword is upserted into the global
`keywords` table.

Usage (from backend/, with the venv):
    .venv/Scripts/python.exe scripts/import_serp_snapshot.py path/to/serp_*.json [more.json ...]
    .venv/Scripts/python.exe scripts/import_serp_snapshot.py --source dataforseo dfs_result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the backend package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.models.keyword_analysis import SerpSnapshot, SerpSnapshotItem
from app.services import keyword_analysis_service as kas

# location_code -> ISO-2 country (inverse of the scraper's LOCATION_CODES).
_CC_BY_LOCATION = {
    2356: "in", 2840: "us", 2826: "gb", 2124: "ca", 2036: "au", 2276: "de",
    2250: "fr", 2724: "es", 2380: "it", 2528: "nl", 2076: "br", 2702: "sg",
    2784: "ae", 2586: "pk", 2050: "bd", 2710: "za", 2392: "jp", 2360: "id",
    2608: "ph", 2458: "my", 2566: "ng", 2484: "mx",
}


def _parse_dt(value: str | None) -> datetime:
    if value:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def import_file(db, path: Path, source_override: str | None) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    task = payload["tasks"][0]
    data = task.get("data", {})
    result = task["result"][0]

    keyword = result.get("keyword") or data.get("keyword")
    if not keyword:
        raise ValueError(f"{path.name}: no keyword in JSON")

    engine = (data.get("se") or "google").lower()
    src = payload.get("_source", "")
    source = source_override or ("scrape" if src.startswith("scrape") else "dataforseo")
    loc = result.get("location_code") or data.get("location_code")
    country = _CC_BY_LOCATION.get(loc, "in")

    kw = kas._upsert_keyword(db, keyword)
    db.flush()

    snapshot = SerpSnapshot(
        keyword_id=kw.id,
        search_engine=engine,
        source=source,
        device="desktop",
        country_code=country,
        language_code=result.get("language_code") or "en",
        location_code=loc,
        depth=data.get("depth") or result.get("items_count"),
        items_count=result.get("items_count") or len(result.get("items", [])),
        se_results_count=result.get("se_results_count"),
        check_url=result.get("check_url"),
        status="ok",
        raw={"data": data, "result_meta": {k: v for k, v in result.items() if k != "items"}},
        captured_at=_parse_dt(result.get("datetime")),
    )
    snapshot.items = [
        SerpSnapshotItem(
            type=it.get("type", "organic"),
            rank_absolute=it.get("rank_absolute") or (i + 1),
            rank_group=it.get("rank_group"),
            domain=it.get("domain") or "",
            url=it.get("url") or "",
            title=it.get("title"),
            description=it.get("description"),
            breadcrumb=it.get("breadcrumb"),
            raw=it,
        )
        for i, it in enumerate(result.get("items", []))
    ]
    db.add(snapshot)
    db.flush()
    return (f"{path.name}: keyword={keyword!r} engine={engine} source={source} "
            f"country={country} items={len(snapshot.items)} snapshot_id={snapshot.id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import SERP JSON into serp_snapshots.")
    ap.add_argument("files", nargs="+", help="serp_snapshot.py / DataForSEO JSON files")
    ap.add_argument("--source", choices=["scrape", "dataforseo", "import"],
                    help="override the source label (else inferred from _source)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        for f in args.files:
            print(import_file(db, Path(f), args.source))
        db.commit()
        print("committed.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
