"""Apply the rank-tracking schedule schema directly via idempotent SQL.

The dev DB's alembic_version is stamped ahead of the files on disk (see the
alembic-version-divergence note), so `alembic upgrade head` fails. Until that is
reconciled, this script is the source of truth for the schema change behind
automated rank tracking. It is idempotent (ADD COLUMN IF NOT EXISTS) and does
NOT touch alembic_version.

    backend\\.venv\\Scripts\\python.exe -m app.scripts.apply_rank_tracking_schedule

Mirrors the corresponding migration 0058_rank_tracking_schedule.py.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db.session import engine

_STATEMENTS = [
    # tracked_keywords — scheduling + status
    "ALTER TABLE tracked_keywords ADD COLUMN IF NOT EXISTS tracking_enabled BOOLEAN NOT NULL DEFAULT true",
    "ALTER TABLE tracked_keywords ADD COLUMN IF NOT EXISTS tracking_frequency_days SMALLINT NOT NULL DEFAULT 15",
    "ALTER TABLE tracked_keywords ADD COLUMN IF NOT EXISTS tracking_status VARCHAR(16) NOT NULL DEFAULT 'idle'",
    "ALTER TABLE tracked_keywords ADD COLUMN IF NOT EXISTS last_fetched_at TIMESTAMPTZ",
    "ALTER TABLE tracked_keywords ADD COLUMN IF NOT EXISTS next_fetch_at TIMESTAMPTZ",
    "ALTER TABLE tracked_keywords ADD COLUMN IF NOT EXISTS last_error TEXT",
    "CREATE INDEX IF NOT EXISTS ix_tracked_keywords_due "
    "ON tracked_keywords (tracking_enabled, next_fetch_at)",
    # Existing rows should be due now so the scheduler/first fetch picks them up.
    "UPDATE tracked_keywords SET next_fetch_at = now() WHERE next_fetch_at IS NULL",
    # rank_entries — ranking URL + source
    "ALTER TABLE rank_entries ADD COLUMN IF NOT EXISTS url TEXT",
    "ALTER TABLE rank_entries ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'serp'",
]


def main() -> int:
    with engine.begin() as conn:
        for stmt in _STATEMENTS:
            conn.execute(text(stmt))
    print(f"Applied {len(_STATEMENTS)} rank-tracking schema statements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
