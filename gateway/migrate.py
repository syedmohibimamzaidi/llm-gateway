"""Single-runner transactional SQL migrations; never emit connection credentials."""

import os
from pathlib import Path
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict


MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 67617465776179


def migrate(dsn: str, directory: Path = MIGRATIONS) -> None:
    files = sorted(directory.glob("*.sql"))
    if not dsn or not files:
        raise ValueError("Migration database or SQL files are missing")
    hosts = conninfo_to_dict(dsn).get("host", "").split(",")
    if any(host.endswith(".neon.tech") and "-pooler" in host.split(".")[0] for host in hosts):
        raise ValueError("Migrations require a direct Neon endpoint for session locking")
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
        connection.execute("SET statement_timeout = '30s'")
        connection.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            for path in files:
                connection.execute(path.read_text(encoding="utf-8"))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def main() -> int:
    try:
        migrate(os.getenv("DATABASE_URL", ""))
    except Exception as exc:
        print(f"Migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Database migrations applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
