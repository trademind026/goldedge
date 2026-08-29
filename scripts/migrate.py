"""
migrate.py — Applies SQL migration files in order using SQLAlchemy.
Tracks which migrations have already been applied in a `_schema_migrations` table.
Never run raw SQL files manually again.

Usage:
    python scripts/migrate.py            # apply all pending migrations
    python scripts/migrate.py --status   # show which migrations are applied
"""
import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import Engine, text

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from goldedge.adapters.secondary.db import build_engine

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _ensure_migrations_table(engine: Engine):
    """Create the internal tracking table if it doesn't exist."""
    sql = text("""
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            filename   VARCHAR(255) PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    with engine.begin() as conn:
        conn.execute(sql)


def _get_applied(engine: Engine) -> set:
    sql = text("SELECT filename FROM _schema_migrations ORDER BY filename")
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return {r.filename for r in rows}


def _apply_migration(engine: Engine, filepath: Path):
    sql_content = filepath.read_text(encoding="utf-8")
    with engine.begin() as conn:
        # Execute the raw SQL migration
        conn.execute(text(sql_content))
        # Record it
        conn.execute(
            text("INSERT INTO _schema_migrations (filename) VALUES (:filename)"),
            {"filename": filepath.name}
        )
    print(f"  ✓  Applied: {filepath.name}")


def run_migrations(db_url: str):
    engine = build_engine(db_url, pool_size=1, max_overflow=1)
    _ensure_migrations_table(engine)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        print("No migration files found in ./migrations/")
        engine.dispose()
        return

    applied = _get_applied(engine)
    pending = [f for f in migration_files if f.name not in applied]

    if not pending:
        print(f"All {len(migration_files)} migrations already applied. Schema is up to date.")
        engine.dispose()
        return

    print(f"Applying {len(pending)} pending migration(s)...")
    for mfile in pending:
        _apply_migration(engine, mfile)

    engine.dispose()
    print("Migration complete.")


def show_status(db_url: str):
    engine = build_engine(db_url, pool_size=1, max_overflow=1)
    _ensure_migrations_table(engine)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied = _get_applied(engine)
    engine.dispose()

    print(f"\n{'='*55}")
    print(f"  Schema Migration Status")
    print(f"{'='*55}")
    for mfile in migration_files:
        status = "✓ applied" if mfile.name in applied else "✗ pending"
        print(f"  [{status}]  {mfile.name}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run GoldEdge schema migrations")
    parser.add_argument("--status", action="store_true", help="Show migration status only")
    args = parser.parse_args()

    db_url = os.environ.get("DB_URL", "postgresql://user:password@localhost:5432/goldedge")

    if args.status:
        show_status(db_url)
    else:
        run_migrations(db_url)
