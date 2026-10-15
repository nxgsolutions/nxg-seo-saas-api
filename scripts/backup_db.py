"""Back up the PostgreSQL database to a timestamped SQL dump.

Reads DATABASE_URL and DB_BACKUP_DIR from the environment / backend .env and runs
pg_dump. The backup directory is git-ignored (dumps contain user emails +
password hashes — never commit them).

Usage:
    .venv/Scripts/python -m scripts.backup_db
    DB_BACKUP_DIR=/path/to/dir .venv/Scripts/python -m scripts.backup_db

Requires `pg_dump` on PATH (set PG_DUMP to an explicit path if it isn't).
Restore a dump with:  psql -d <db> -f <file.sql>
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.core.config import settings


def main() -> None:
    url = settings.DATABASE_URL
    if not url:
        raise SystemExit("DATABASE_URL is not set.")
    # Normalize SQLAlchemy driver URL (postgresql+psycopg://) to a plain one.
    parsed = urlparse(url.replace("+psycopg", "").replace("+asyncpg", ""))
    host = parsed.hostname or "localhost"
    port = str(parsed.port or 5432)
    user = unquote(parsed.username or "postgres")
    password = unquote(parsed.password or "")
    db = (parsed.path or "/").lstrip("/") or "postgres"

    backup_dir = Path(os.environ.get("DB_BACKUP_DIR")
                      or getattr(settings, "DB_BACKUP_DIR", None)
                      or "db_backup")
    # Resolve relative to the repo root (parent of backend/).
    if not backup_dir.is_absolute():
        backup_dir = Path(__file__).resolve().parents[2] / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = backup_dir / f"{db}_{stamp}.sql"

    pg_dump = os.environ.get("PG_DUMP", "pg_dump")
    env = {**os.environ, "PGPASSWORD": password}
    cmd = [pg_dump, "-h", host, "-p", port, "-U", user, "-d", db, "-F", "p", "-f", str(out)]
    print(f"Backing up {db} -> {out}")
    try:
        subprocess.run(cmd, env=env, check=True)
    except FileNotFoundError:
        raise SystemExit(
            "pg_dump not found. Install PostgreSQL client tools or set PG_DUMP to "
            "the full path (e.g. 'C:\\Program Files\\PostgreSQL\\18\\bin\\pg_dump.exe')."
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"pg_dump failed (exit {exc.returncode}).")

    size_kb = out.stat().st_size / 1024
    print(f"OK — {out}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
