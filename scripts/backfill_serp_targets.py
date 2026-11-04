#!/usr/bin/env python3
"""Backfill real destination url/domain for Google-redirect SERP items.

Older serp_snapshot_items rows stored DataForSEO's Google redirect wrapper
verbatim (domain = "google.com", url = https://google.com/goto?url=<token>).
This re-resolves each such row from its stored `raw` JSON via the same helper
the live parsers now use (dataforseo_client._resolve_serp_target) and UPDATEs the
`url` / `domain` columns in place. The `raw` column is left untouched.

Idempotent: a row already resolved (or with no usable breadcrumb) is a no-op.

Usage (from backend/, with the venv):
    .venv/Scripts/python.exe scripts/backfill_serp_targets.py --dry-run
    .venv/Scripts/python.exe scripts/backfill_serp_targets.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the backend package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.keyword_analysis import SerpSnapshotItem
from app.services.dataforseo_client import _resolve_serp_target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing.")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        # Candidates: the google.com wrapper domain OR a goto/url redirect link.
        rows = db.execute(
            select(SerpSnapshotItem).where(
                (SerpSnapshotItem.domain == "google.com")
                | (SerpSnapshotItem.url.like("%/goto?url=%"))
                | (SerpSnapshotItem.url.like("%google.com/url?%"))
            )
        ).scalars().all()

        changed = resolved = skipped_no_raw = still_wrapped = 0
        for it in rows:
            if not it.raw:
                skipped_no_raw += 1
                continue
            new_url, new_domain = _resolve_serp_target(it.raw)
            if new_domain == "google.com":
                still_wrapped += 1
                continue
            if new_url == it.url and new_domain == it.domain:
                continue  # already resolved — no-op
            print(f"  [{it.rank_absolute:>3}] {it.domain} -> {new_domain}  {new_url[:70]}")
            if not args.dry_run:
                it.url, it.domain = new_url, new_domain
            changed += 1
            resolved += 1

        if args.dry_run:
            print(f"\nDRY RUN: {len(rows)} candidate rows; would resolve {resolved}, "
                  f"leave {still_wrapped} wrapped, skip {skipped_no_raw} with no raw.")
            db.rollback()
        else:
            db.commit()
            print(f"\nDONE: resolved {resolved} of {len(rows)} candidate rows; "
                  f"{still_wrapped} left wrapped, {skipped_no_raw} skipped (no raw).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
