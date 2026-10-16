"""One-off: measure the REAL cost of the backlinks warm-up for iaspirants.com.

Checks the live DataForSEO balance, warms backlinks for our domain + its
competitors (live), then checks the balance again and prints the delta.

Usage:
  python -m scripts.warm_backlinks_test --domain iaspirants.com --country in --force
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.services import data_env, dataforseo_client, domain_analytics_service, opportunity_service


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="iaspirants.com")
    ap.add_argument("--country", default="in")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        domain = db.execute(
            select(Domain).where(Domain.domain.ilike(f"%{args.domain}%"))
        ).scalars().first()
        if not domain:
            raise SystemExit(f"Domain matching {args.domain!r} not found.")

        host = opportunity_service._norm_host(domain.domain)
        competitors = [c["domain"] for c in opportunity_service.strategy(db, domain)["competitors"]]
        targets = [host, *competitors]
        loc = domain_analytics_service.location_code_for(args.country)

        print(f"Domain:      {domain.domain}  (workspace {domain.workspace_id})")
        print(f"Location:    {args.country} -> {loc}")
        print(f"Targets ({len(targets)}): {host}  + competitors: {competitors}")

        bal_before = dataforseo_client.account_balance(env=data_env.LIVE)
        print(f"\nBalance BEFORE: ${bal_before}")

        result = domain_analytics_service.warm_backlinks(
            db, targets, location_code=loc, language="en",
            force=args.force, env=data_env.LIVE,
        )
        print(f"Warm result:   {result}")

        bal_after = dataforseo_client.account_balance(env=data_env.LIVE)
        print(f"Balance AFTER:  ${bal_after}")
        if bal_before is not None and bal_after is not None:
            spent = round(bal_before - bal_after, 6)
            n = max(result["fetched"], 1)
            print(f"\n>>> ACTUAL COST: ${spent}  for {result['fetched']} domains "
                  f"(~${round(spent / n, 6)} per domain)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
