"""Background data-collection jobs that grow the DataCube on a schedule.

Jobs are plain functions `fn(db, **options) -> stats dict`, launched by an
external scheduler (PM2 cron on the server, cron/launchd locally) through the
CLI in `app/jobs/__main__.py`:

    python -m app.jobs gsc_sync --triggered-by scheduler

Every run is logged in the `job_runs` table.
"""

from app.jobs import gsc_sync, serp_sync

JOBS = {
    "gsc_sync": gsc_sync.run,
    "serp_sync": serp_sync.run,
}
