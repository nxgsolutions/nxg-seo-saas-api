"""Seed realistic rank-tracking history into a SANDBOX workspace for a visual demo.

The DataForSEO sandbox returns generic dummy SERPs (UK pizza sites) that ignore
the keyword and never contain the user's own domain, so it can't produce a
meaningful own-vs-competitor trend. This script instead writes synthetic but
realistic UPSC-niche history (our domain + real competitor hosts) across many
dates, all tagged environment='sandbox', so the /dashboard/rankings graph can be
exercised end to end without touching live data or spending anything.

    backend\\.venv\\Scripts\\python.exe -m app.scripts.seed_sandbox_rankings
    ...  -m app.scripts.seed_sandbox_rankings --workspace-id <uuid> --points 10 --step-days 15

Idempotent: re-running resets each seeded keyword's rank_entries and its sandbox
SERP snapshots, then rebuilds them. Only touches is_sandbox workspaces.
"""

from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.keyword_analysis import SerpSnapshot, SerpSnapshotItem
from app.models.ranking import RankEntry, TrackedKeyword
from app.models.workspace import Workspace
from app.services import data_env, domain_settings, rank_service
from app.services.rank_service import KeywordExistsError


def _get_or_add(db, domain, term: str) -> TrackedKeyword:
    """Reuse an existing tracked keyword (any state) or create it."""
    existing = db.execute(
        select(TrackedKeyword).where(
            TrackedKeyword.domain_id == domain.id, TrackedKeyword.term == term,
        ).execution_options(include_deleted=True)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.deleted_at is not None:
            existing.deleted_at = None
            db.commit()
        return existing
    try:
        return rank_service.add_keyword(db, domain, term)
    except KeywordExistsError:
        return db.execute(
            select(TrackedKeyword).where(
                TrackedKeyword.domain_id == domain.id, TrackedKeyword.term == term,
            )
        ).scalar_one()

# Keyword -> competitor hosts that rank for it + our own starting position and a
# per-step drift (negative = improving toward #1). Real UPSC-prep competitors.
_COMPETITORS = [
    "drishtiias.com", "visionias.in", "insightsonindia.com", "clearias.com",
    "forumias.com", "testbook.com", "byjus.com", "vajiramandravi.com",
    "nextias.com", "unacademy.com",
]

# (keyword, our_start, our_drift_per_step, frequency_days)
_KEYWORDS = [
    ("upsc syllabus", 18, -1.1, 15),
    ("ias preparation", 27, -1.6, 15),
    ("current affairs for upsc", 9, 0.6, 30),
    ("upsc previous year papers", 34, -2.0, 15),
    ("best books for upsc", 14, 0.9, 30),
    ("upsc eligibility criteria", 22, -0.8, 15),
]


def _walk(start: float, drift: float, steps: int, *, lo: int, hi: int, jitter: float, rng: random.Random) -> list[int | None]:
    """A clamped random walk of integer positions; values beyond `hi` become None
    (not ranking in the tracked range)."""
    out: list[int | None] = []
    v = float(start)
    for _ in range(steps):
        v += drift + rng.uniform(-jitter, jitter)
        p = round(v)
        out.append(None if p > hi else max(lo, p))
    return out


def _pick_sandbox_workspace(db, workspace_id: str | None) -> Workspace:
    if workspace_id:
        ws = db.get(Workspace, workspace_id)
        if ws is None:
            raise SystemExit(f"Workspace {workspace_id} not found")
    else:
        ws = db.execute(
            select(Workspace).where(Workspace.is_sandbox.is_(True)).order_by(Workspace.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        if ws is None:
            raise SystemExit("No sandbox workspace found. Pass --workspace-id.")
    if not ws.is_sandbox:
        raise SystemExit(f"Workspace {ws.id} ({ws.name!r}) is NOT a sandbox — refusing to seed.")
    return ws


def _reset_keyword(db, kw: TrackedKeyword) -> None:
    db.execute(text("DELETE FROM rank_entries WHERE keyword_id = :k"), {"k": str(kw.id)})
    if kw.keyword_id is not None:
        db.execute(text(
            "DELETE FROM serp_snapshot_items WHERE snapshot_id IN "
            "(SELECT id FROM serp_snapshots WHERE keyword_id = :k AND environment = 'sandbox')"
        ), {"k": str(kw.keyword_id)})
        db.execute(text(
            "DELETE FROM serp_snapshots WHERE keyword_id = :k AND environment = 'sandbox'"
        ), {"k": str(kw.keyword_id)})
    db.commit()


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.scripts.seed_sandbox_rankings")
    ap.add_argument("--workspace-id", default=None, help="sandbox workspace id (default: most recent sandbox)")
    ap.add_argument("--points", type=int, default=10, help="number of dated snapshots per keyword")
    ap.add_argument("--step-days", type=int, default=15, help="days between snapshots")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    db = SessionLocal()
    try:
        ws = _pick_sandbox_workspace(db, args.workspace_id)
        domain = db.execute(
            select(Domain).where(Domain.workspace_id == ws.id, Domain.deleted_at.is_(None)).order_by(Domain.created_at)
        ).scalars().first()
        if domain is None:
            raise SystemExit(f"Sandbox workspace {ws.name!r} has no domain.")
        conf = domain_settings.resolve(domain, ws)
        own_host = domain.domain.strip().lower().removeprefix("www.")
        env = data_env.env_for_workspace(ws)  # 'sandbox'

        # Date axis: oldest -> today, every step_days.
        today = datetime.now(timezone.utc).date()
        dates = [today - timedelta(days=args.step_days * (args.points - 1 - i)) for i in range(args.points)]

        print(f"Seeding {len(_KEYWORDS)} keywords x {args.points} dates into "
              f"{ws.name!r} ({str(ws.id)[:8]}) domain {domain.domain} env={env}")

        for term, our_start, our_drift, freq in _KEYWORDS:
            kw = _get_or_add(db, domain, term)  # reuses/reactivates
            _reset_keyword(db, kw)

            # Our own line (occasional not-ranking gaps for realism).
            own = _walk(our_start, our_drift, args.points, lo=1, hi=100, jitter=2.2, rng=rng)

            # 4-6 competitors for this keyword; a couple appear/disappear mid-series.
            comp_hosts = rng.sample(_COMPETITORS, rng.randint(4, 6))
            late = comp_hosts[0]   # a "new" competitor: only ranks from mid-series on
            early = comp_hosts[1]  # a "lost" competitor: drops off near the end
            comp_walks: dict[str, list[int | None]] = {}
            for h in comp_hosts:
                base = rng.randint(2, 20)
                drift = rng.uniform(-1.2, 1.2)
                walk = _walk(base, drift, args.points, lo=1, hi=60, jitter=2.0, rng=rng)
                comp_walks[h] = walk
            appear_at = args.points // 2
            drop_at = args.points - 2
            for i in range(args.points):
                if i < appear_at:
                    comp_walks[late][i] = None
                if i >= drop_at:
                    comp_walks[early][i] = None

            made_snaps = 0
            made_entries = 0
            for i, d in enumerate(dates):
                # Build the SERP for this date: our row (if ranking) + competitors.
                rows: list[tuple[str, int]] = []
                if own[i] is not None:
                    rows.append((own_host, own[i]))
                for h in comp_hosts:
                    p = comp_walks[h][i]
                    if p is not None:
                        rows.append((h, p))
                rows.sort(key=lambda r: r[1])
                snap = SerpSnapshot(
                    keyword_id=kw.keyword_id, search_engine="google", source="dataforseo",
                    environment=env, device="desktop",
                    country_code=conf.country, language_code=conf.language,
                    depth=100, items_count=len(rows), status="ok",
                    captured_at=datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc),
                )
                snap.items = [
                    SerpSnapshotItem(
                        type="organic", rank_absolute=pos, rank_group=pos,
                        domain=host, url=f"https://{host}/", title=None,
                    )
                    for host, pos in rows
                ]
                db.add(snap)
                made_snaps += 1
                # Our own rank entry for the day (null = not ranking).
                db.add(RankEntry(
                    keyword_id=kw.id, position=own[i], recorded_at=d,
                    url=f"https://{own_host}/" if own[i] is not None else None, source="serp",
                ))
                made_entries += 1

            # Schedule fields: last fetch = last date, next = +frequency.
            last_dt = datetime(dates[-1].year, dates[-1].month, dates[-1].day, 10, 0, tzinfo=timezone.utc)
            kw.tracking_frequency_days = freq
            kw.tracking_status = "idle"
            kw.tracking_enabled = True
            kw.last_fetched_at = last_dt
            kw.next_fetch_at = last_dt + timedelta(days=freq)
            db.commit()
            latest = next((p for p in reversed(own) if p is not None), None)
            print(f"  {term!r}: {made_snaps} snapshots, {made_entries} entries, "
                  f"{len(comp_hosts)} competitors, latest own #{latest}, freq {freq}d")

        print("Done. Open /dashboard/rankings in the sandbox workspace to view the graphs.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
