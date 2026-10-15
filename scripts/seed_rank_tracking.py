"""Seed realistic Rank Tracking demo data (position history).

For every tracked keyword in the targeted domain(s), this replaces the keyword's
rank_entries with N weekly positions ending today, following a believable path
(a noisy, gently improving trend, occasionally "not ranking"). This gives the
Rank Tracking page real-looking trend charts, best/latest/avg stats and the
top-10 summary — without waiting for live SERP syncs.

Idempotent: re-running wipes and reinserts entries for the same keywords only.
It never adds or removes tracked keywords, and touches no other tables.

Run (from backend/, with the venv):
    .venv/bin/python -m scripts.seed_rank_tracking                 # all domains with tracked keywords
    .venv/bin/python -m scripts.seed_rank_tracking --domain work   # only domains whose name/url contains "work"
    .venv/bin/python -m scripts.seed_rank_tracking --weeks 16       # 16 weeks of history (default 12)
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.ranking import RankEntry, TrackedKeyword

random.seed(42)


def _trend(weeks: int) -> list[int | None]:
    """A believable weekly position path: starts mid-pack, drifts upward (better),
    with weekly noise and an occasional miss (None = not in the top 100)."""
    start = random.randint(18, 70)
    target = random.randint(1, max(2, start - random.randint(8, 40)))
    positions: list[int | None] = []
    current = float(start)
    for i in range(weeks):
        # Ease toward the target, then add weekly jitter.
        current += (target - current) * random.uniform(0.12, 0.3)
        jittered = round(current + random.uniform(-3, 3))
        value = max(1, min(100, jittered))
        # Early weeks sometimes aren't ranking yet.
        if i < 2 and random.random() < 0.3:
            positions.append(None)
        elif value >= 100 and random.random() < 0.5:
            positions.append(None)
        else:
            positions.append(value)
    return positions


def seed(domain_filter: str | None, weeks: int) -> None:
    db = SessionLocal()
    try:
        domains = db.scalars(select(Domain)).all()
        if domain_filter:
            needle = domain_filter.lower()
            domains = [d for d in domains if needle in (d.name or "").lower() or needle in (d.domain or "").lower()]

        if not domains:
            print("No matching domains.")
            return

        today = date.today()
        total_kw = 0
        total_entries = 0

        for domain in domains:
            tracked = db.scalars(
                select(TrackedKeyword).where(TrackedKeyword.domain_id == domain.id)
            ).all()
            if not tracked:
                continue

            print(f"• {domain.name} ({domain.domain}) — {len(tracked)} tracked keyword(s)")
            for kw in tracked:
                # Replace any existing history for this keyword (idempotent).
                db.execute(delete(RankEntry).where(RankEntry.keyword_id == kw.id))
                positions = _trend(weeks)
                for i, position in enumerate(positions):
                    recorded = today - timedelta(weeks=(weeks - 1 - i))
                    db.add(RankEntry(keyword_id=kw.id, position=position, recorded_at=recorded))
                    total_entries += 1
                total_kw += 1

        db.commit()
        print(f"\nDone. Seeded {total_entries} entries across {total_kw} tracked keyword(s).")

        grand = db.scalar(select(func.count()).select_from(RankEntry))
        print(f"rank_entries now holds {grand} rows.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed Rank Tracking position history.")
    parser.add_argument("--domain", default=None, help="Only seed domains whose name or URL contains this text.")
    parser.add_argument("--weeks", type=int, default=12, help="Weeks of weekly history per keyword (default 12).")
    args = parser.parse_args()
    seed(args.domain, args.weeks)
