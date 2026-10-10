"""Apply db/migrations/*.sql in filename order, once each.

    uv run python -m scripts.migrate            # migrations only
    uv run python -m scripts.migrate --seed     # then db/seed.sql (test designers)

Uses DATABASE_URL_UNPOOLED (Neon's non-pooler host; `neon link` writes it to
.env.local). Migrations need a direct connection: the pooler runs PgBouncer in
transaction mode, which does not support session state or advisory locks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from app.config import ROOT, get_settings

MIGRATIONS = ROOT / "db" / "migrations"
SEED = ROOT / "db" / "seed.sql"


def apply(conninfo: str, seed: bool = False, migrations_dir: Path = MIGRATIONS) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=True, connect_timeout=10) as conn:
        conn.execute(
            "create table if not exists schema_migrations ("
            " filename text primary key, applied_at timestamptz not null default now())"
        )
        conn.execute("select pg_advisory_lock(727372)")  # one migrator at a time
        try:
            done = {r[0] for r in conn.execute("select filename from schema_migrations")}
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("insert into schema_migrations (filename) values (%s)", [path.name])
                applied.append(path.name)
            if seed:
                with conn.transaction():
                    conn.execute(SEED.read_text(encoding="utf-8"))
                applied.append(SEED.name)
        finally:
            conn.execute("select pg_advisory_unlock(727372)")
    return applied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    s = get_settings()
    url = s.database_url_unpooled or s.database_url
    if not url:
        print("Set DATABASE_URL_UNPOOLED (Neon direct connection string)", file=sys.stderr)
        return 1
    if "-pooler." in url:
        print("Refusing to migrate through Neon's pooler; use the direct host.", file=sys.stderr)
        return 1
    applied = apply(url, a.seed)
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
