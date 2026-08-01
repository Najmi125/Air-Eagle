"""
scripts/run_migrations.py

Applies every numbered .sql file in migrations/ that hasn't been
applied yet, in filename order, and records each one in
schema_migrations. This is the ONLY sanctioned way schema changes
reach a database — see Ownership Table: "Crew qualifications /
schema -> migrations/ -> Forbidden: UI-level table creation."

Usage:
    python scripts/run_migrations.py                 # apply pending
    python scripts/run_migrations.py --dry-run        # show what would run
    python scripts/run_migrations.py --status          # show applied vs pending

Requires DATABASE_URL — loaded from .env if present (see .env.example),
with override=True so this project's own .env always wins over any
environment variable that happens to already be set in the shell.
"""
import os
import sys
import hashlib
import argparse
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# override=True: without this, a stale DATABASE_URL already present
# in the shell (e.g. left over from an unrelated earlier project)
# would silently win over .env, with no error of any kind. This was
# a real, confirmed bug — this script previously didn't call
# load_dotenv() at all, so it only ever read a genuine shell
# environment variable, completely ignoring .env's contents. A user
# hit this directly: their shell had a leftover Neon DATABASE_URL,
# and every run of this script silently connected there instead of
# the Supabase database they'd just configured.
load_dotenv(override=True)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def get_engine():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set in environment.")
        sys.exit(1)
    return create_engine(db_url)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migrations():
    """Numbered .sql files, sorted. 000_ (tracking table itself) always first."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"No migration files found in {MIGRATIONS_DIR}")
    return files


def ensure_tracking_table(engine):
    tracking_file = MIGRATIONS_DIR / "000_migration_tracking.sql"
    if not tracking_file.exists():
        print("ERROR: 000_migration_tracking.sql is missing. Cannot proceed.")
        sys.exit(1)
    with engine.begin() as conn:
        conn.execute(text(tracking_file.read_text(encoding="utf-8")))


def get_applied(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT migration_id, checksum FROM schema_migrations"
        )).fetchall()
    return {row[0]: row[1] for row in rows}


def run(dry_run=False, status_only=False, engine=None):
    if engine is None:
        engine = get_engine()
    ensure_tracking_table(engine)

    applied = get_applied(engine)
    files = discover_migrations()

    pending = []
    for f in files:
        name = f.name
        current_checksum = checksum(f)

        if name in applied:
            if applied[name] != current_checksum:
                print(
                    f"WARNING: {name} has already been applied, but its content "
                    f"has changed since then (checksum mismatch). This migration "
                    f"was edited after being applied somewhere — that's exactly "
                    f"what the numbering rule exists to prevent. Investigate "
                    f"before doing anything else; do not just re-run it."
                )
            continue
        pending.append((f, current_checksum))

    if status_only:
        print(f"Applied: {len(applied)}  Pending: {len(pending)}")
        for f, _ in pending:
            print(f"  PENDING: {f.name}")
        return

    if not pending:
        print("Database is up to date. Nothing to apply.")
        return

    print(f"{len(pending)} migration(s) pending:")
    for f, _ in pending:
        print(f"  {f.name}")

    if dry_run:
        print("\n--dry-run: nothing was executed.")
        return

    for f, csum in pending:
        # 000_migration_tracking.sql is applied via ensure_tracking_table()
        # above (it has to exist before we can even query schema_migrations),
        # so skip re-running it here, but still record it if not recorded.
        if f.name != "000_migration_tracking.sql":
            print(f"Applying {f.name} ...")
            with engine.begin() as conn:
                # Explicit UTF-8: every migration file's comments use
                # em dashes, and Path.read_text() with no encoding
                # defaults to the OS locale codec (cp1252 on Windows),
                # which silently mis-decodes them into mojibake rather
                # than raising — harmless here since Postgres line
                # comments ignore their own content, but the same bug
                # class as scripts/check_reachability.py's, where the
                # equivalent silent corruption broke actual logic, not
                # just comment text. checksum() is unaffected — it
                # hashes read_bytes(), not read_text().
                conn.execute(text(f.read_text(encoding="utf-8")))
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO schema_migrations (migration_id, checksum, applied_by) "
                "VALUES (:mid, :cs, :by) ON CONFLICT (migration_id) DO NOTHING"
            ), {"mid": f.name, "cs": csum, "by": os.environ.get("USER", "unknown")})
        print(f"  -> recorded as applied.")

    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run, status_only=args.status)
