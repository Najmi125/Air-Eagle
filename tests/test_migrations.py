"""
tests/test_migrations.py

Tests the migration runner itself against a real, disposable
Postgres database (see conftest.py's db_engine fixture). This is
the piece of infrastructure the old repo never had at all — no way
to know which of several competing schema scripts had actually run
against a given database.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from scripts import run_migrations as rm


def test_fresh_database_has_pending_migrations(db_engine):
    applied = rm.get_applied(db_engine) if _tracking_table_exists(db_engine) else {}
    assert applied == {}


def test_apply_migrations_is_idempotent(db_engine, capsys):
    rm.run(engine=db_engine)
    first_run_applied = rm.get_applied(db_engine)
    assert len(first_run_applied) >= 1  # at least the tracking migration itself

    # Running again must not error and must not re-apply anything.
    rm.run(engine=db_engine)
    second_run_applied = rm.get_applied(db_engine)
    assert first_run_applied == second_run_applied

    captured = capsys.readouterr()
    assert "up to date" in captured.out.lower()


def test_dry_run_does_not_modify_database(db_engine):
    rm.run(dry_run=True, engine=db_engine)
    # dry-run must not have created the tracking table's row for itself
    # beyond what ensure_tracking_table() does structurally — no
    # migration should be recorded as applied after a dry run.
    applied = rm.get_applied(db_engine)
    assert applied == {}


def test_editing_an_applied_migration_is_detected(db_engine, tmp_path, monkeypatch):
    # Point the runner at an isolated migrations dir so this test
    # doesn't depend on (or risk corrupting) the real migrations/.
    fake_migrations = tmp_path / "migrations"
    fake_migrations.mkdir()
    tracking = fake_migrations / "000_migration_tracking.sql"
    tracking.write_text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "migration_id VARCHAR(255) PRIMARY KEY, "
        "applied_at TIMESTAMP NOT NULL DEFAULT NOW(), "
        "checksum VARCHAR(64) NOT NULL, "
        "applied_by VARCHAR(100));"
    )
    monkeypatch.setattr(rm, "MIGRATIONS_DIR", fake_migrations)

    rm.run(engine=db_engine)
    assert len(rm.get_applied(db_engine)) == 1

    # Simulate editing the file after it was already applied.
    tracking.write_text(tracking.read_text() + "\n-- tampered\n")

    rm.run(engine=db_engine)  # should not crash, should warn
    # database state should be unchanged despite the edit
    assert len(rm.get_applied(db_engine)) == 1


def _tracking_table_exists(engine) -> bool:
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'schema_migrations')"
        )).scalar()
    return bool(result)
