"""
tests/conftest.py

Fixtures shared across the test suite.

Most of the tests that matter most (duty dedup, FDP calculation,
legality rules) are pure functions with no DB dependency — they
don't need anything from this file. The db_engine fixture below is
only for tests that genuinely exercise the database: the migration
runner, and later, service-layer integration tests.

Requires TEST_DATABASE_URL in the environment, separate from
DATABASE_URL, so tests can never accidentally run against a real
deployment's data. If TEST_DATABASE_URL isn't set, DB-dependent
tests are skipped (not silently passed) — you'll see exactly which
ones didn't run and why.
"""
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="function")
def db_engine():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL not set — skipping DB-dependent test")

    engine = create_engine(test_url)

    # Nuke and pave: every test that uses this fixture gets a
    # genuinely empty schema, not whatever state a previous test
    # run left behind.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    yield engine

    engine.dispose()


@pytest.fixture
def migrated_db(db_engine):
    """db_engine already gives a wiped-clean schema. Apply all
    migrations on top of it — for any test that needs the real
    crew/flights/roster/audit_log tables, not just an empty DB.
    Used by test_schema.py, test_crew_service.py, and onward."""
    from scripts import run_migrations as rm
    rm.run(engine=db_engine)
    return db_engine


@pytest.fixture
def _patch_all_service_engines(migrated_db, monkeypatch):
    """
    Patches get_engine() on every service module that has one, so any
    test needing the real migrated test DB can request this single
    fixture instead of a per-file, easy-to-under-scope copy.

    This exists because omitting a module here is a real, already-
    happened failure mode, not a hypothetical: test_rotation_template_
    service.py's own local _patch_engine fixture patched rts/
    flight_service/assignment_service/crew_service but omitted
    audit_service — invisible in every test that reached log_audit()
    through a conn= parameter (joining an already-patched connection),
    and only surfaced as a real failure (RuntimeError: DATABASE_URL not
    set) in the one test that called crew_service.add_crew(), whose
    own log_audit() call has no conn and fell through to the
    unpatched get_engine() (2026-08-04). A single shared fixture here
    makes that whole class of gap structurally impossible instead of
    something each new test file has to remember to get right.

    roster_generator_service added 2026-08-04 (Phase 7's roster
    generator) — new modules join this list rather than getting a new
    one-off local fixture, per the same discipline.

    Each test file keeps its own local `_patch_engine` name (a thin,
    3-line wrapper requesting this fixture) so no existing test
    function signature needs to change — only the actual patching
    logic moves to one place.
    """
    import services.assignment_service as assignment_service
    import services.audit_service as audit_service
    import services.crew_service as crew_service
    import services.flight_service as flight_service
    import services.roster_generator_service as roster_generator_service
    import services.rotation_template_service as rotation_template_service

    for mod in (assignment_service, audit_service, crew_service,
                flight_service, rotation_template_service, roster_generator_service):
        monkeypatch.setattr(mod, "get_engine", lambda: migrated_db)
    return migrated_db
