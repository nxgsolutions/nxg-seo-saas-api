"""Remove irrelevant keywords from a domain's tracked (opportunity) keyword list.

Irrelevant = competitor brand / navigational terms or local "near me" intent
(see app/services/keyword_relevance.py). Keywords we actually rank for in GSC
(current_position set) are always kept.

Only `opportunity_keywords` rows are removed. Global keywords, keyword metrics,
SERP snapshots and ranked-keyword data are untouched. Every removed row is
written to a JSON backup first, and --restore puts them back.

Usage (from backend/):
    .venv/Scripts/python -m scripts.clean_tracked_keywords --domain iaspirants.com            # dry run
    .venv/Scripts/python -m scripts.clean_tracked_keywords --domain iaspirants.com --apply
    .venv/Scripts/python -m scripts.clean_tracked_keywords --restore backups/<file>.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword
from app.services import keyword_relevance, opportunity_service

BACKUP_DIR = Path(__file__).resolve().parents[1] / "backups"
_COLUMNS = [c.name for c in OpportunityKeyword.__table__.columns]


def _competitor_hosts(db, domain: Domain) -> list[str]:
    hosts = {c["domain"] for c in opportunity_service.strategy(db, domain)["competitors"]}
    hosts |= set(db.execute(text(
        "SELECT competitor FROM tracked_competitors WHERE domain_id = :d"), {"d": str(domain.id)}).scalars())
    hosts |= set(db.execute(text("SELECT DISTINCT target FROM domain_ranked_keyword_runs")).scalars())
    return sorted(hosts)


def clean(domain_host: str | None, apply: bool, domain_id: str | None = None) -> int:
    db = SessionLocal()
    try:
        # --domain-id targets one exact domain row; --domain matches the host in
        # every workspace (sandbox copies included).
        stmt = select(Domain).where(Domain.id == domain_id) if domain_id else             select(Domain).where(Domain.domain.ilike(domain_host))
        domains = db.execute(stmt).scalars().all()
        if not domains:
            print(f"No domain {domain_host}")
            return 1
        for domain in domains:
            comps = [h for h in _competitor_hosts(db, domain)
                     if keyword_relevance._label(h) != keyword_relevance._label(domain.domain)]
            rows = db.execute(select(OpportunityKeyword).where(
                OpportunityKeyword.domain_id == domain.id)).scalars().all()
            drop = []
            for r in rows:
                if r.current_position is not None:
                    continue  # we genuinely rank for it (GSC) — keep
                reason = keyword_relevance.irrelevance_reason(
                    r.keyword, own_host=domain.domain, competitor_hosts=comps)
                if reason:
                    drop.append((r, reason))

            print(f"\n{domain.domain} ({domain.id}): {len(rows)} tracked, {len(drop)} to remove")
            for r, reason in sorted(drop, key=lambda x: -x[0].search_volume):
                print(f"  - {r.keyword:45} vol {r.search_volume:>7}  [{reason}]")
            if not apply or not drop:
                continue

            BACKUP_DIR.mkdir(exist_ok=True)
            path = BACKUP_DIR / f"opportunity_keywords_{domain.domain}_{domain.id}_{datetime.now():%Y%m%d_%H%M%S}.json"
            path.write_text(json.dumps(
                [{c: getattr(r, c) for c in _COLUMNS} | {"_reason": reason} for r, reason in drop],
                default=str, indent=1), encoding="utf-8")
            for r, _ in drop:
                db.delete(r)
            db.commit()
            print(f"  removed {len(drop)}; backup: {path}")
        if not apply:
            print("\nDry run — nothing changed. Re-run with --apply to remove.")
        return 0
    finally:
        db.close()


def restore(path: str) -> int:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    db = SessionLocal()
    try:
        restored = 0
        for row in rows:
            row.pop("_reason", None)
            if db.get(OpportunityKeyword, row["id"]) is None:
                db.add(OpportunityKeyword(**{k: v for k, v in row.items() if v is not None}))
                restored += 1
        db.commit()
        print(f"Restored {restored} of {len(rows)} rows from {path}")
        return 0
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.clean_tracked_keywords")
    ap.add_argument("--domain", help="host, e.g. iaspirants.com (all workspaces)")
    ap.add_argument("--domain-id", help="one exact domain row id (safer)")
    ap.add_argument("--apply", action="store_true", help="actually remove (default: dry run)")
    ap.add_argument("--restore", help="restore rows from a backup JSON")
    args = ap.parse_args()
    if args.restore:
        return restore(args.restore)
    if not args.domain and not args.domain_id:
        ap.error("--domain or --domain-id is required")
    return clean(args.domain, args.apply, args.domain_id)


if __name__ == "__main__":
    raise SystemExit(main())
