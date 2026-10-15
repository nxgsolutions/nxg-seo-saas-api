"""CLI entry point for scheduled jobs.

    python -m app.jobs gsc_sync                       # run once, by hand
    python -m app.jobs gsc_sync --triggered-by scheduler --days 28 --country in
    python -m app.jobs --list

Exit code: 0 = success, 1 = the job raised, 2 = partial (some domains failed).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.db.session import SessionLocal
from app.jobs import JOBS
from app.jobs.runner import run_job


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m app.jobs")
    ap.add_argument("job", nargs="?", choices=sorted(JOBS))
    ap.add_argument("--list", action="store_true", help="list available jobs")
    ap.add_argument("--triggered-by", default="manual", choices=["manual", "scheduler"])
    ap.add_argument("--days", type=int, default=28, help="GSC lookback window")
    ap.add_argument("--country", default="in", help="ISO-2 locale for keyword_metrics rows")
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    if args.list or not args.job:
        print("\n".join(sorted(JOBS)))
        return 0

    db = SessionLocal()
    try:
        run = run_job(
            db, args.job, JOBS[args.job],
            triggered_by=args.triggered_by,
            days=args.days, country=args.country, language=args.language,
        )
    except Exception:  # noqa: BLE001 - already logged + recorded by run_job
        return 1
    finally:
        db.close()

    print(json.dumps({"job_run_id": str(run.id), "status": run.status, **run.stats}, indent=2, default=str))
    return 2 if run.status == "partial" else 0


if __name__ == "__main__":
    sys.exit(main())
