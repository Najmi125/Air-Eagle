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
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Username the page tests run as. Any non-empty string satisfies the
# gate — require_login() returns session_state["app_user"] as-is when
# it's already set, without touching the users table — so page tests
# need no seeded user and no DB for authentication itself.
TEST_APP_USER = "occ1"

ROOT = Path(__file__).resolve().parent.parent


def page_path(path: str) -> Path:
    """Resolve a repo-relative page path ("pages/4_Roster.py") to an
    absolute one, for AppTest.from_file().

    NEVER hand AppTest.from_file() a relative path. Its resolution is
    two-stage and both stages are traps (Streamlit 1.60,
    AppTest.from_file, whose own comment reads "TODO: Make this not
    super fragile"):

        script_path = Path(script_path)
        if script_path.is_file():        # relative to the CWD
            path = script_path
        else:                            # relative to the CALLER's file
            filepath = Path(stack[1].filename)
            path = filepath.parent / script_path

    is_file() is evaluated against the current working directory, so a
    relative path silently works when pytest is run from the repo root
    and silently breaks when it is run from anywhere else — at which
    point it falls back to resolving against the CALLING file's
    directory, i.e. tests/pages/4_Roster.py, which does not exist.

    That CWD dependence is what made this bug invisible locally
    (2026-08-18): the DB-backed page tests all skip without Postgres,
    and the DB-free ones that did run happened to be run from the repo
    root, so the first branch matched and every path resolved. Against
    real Postgres the DB-backed tests actually executed and produced 57
    errors. An absolute path takes the first branch unconditionally, so
    resolution no longer depends on the CWD or on which file calls it.
    """
    return ROOT / path


def authed_app_test(path: str, app_user: str = TEST_APP_USER) -> AppTest:
    """Build an AppTest for a real page file with the login gate
    already satisfied.

    Every page calls auth_service.require_login() near its top, which
    st.stop()s unless session_state["app_user"] is set. Without this,
    every page test asserts against a login form instead of the page,
    and the failure is confusing rather than obvious: the page renders
    "successfully" with no exception, just none of its own content.

    This lives here rather than in each test file for the same reason
    _patch_all_service_engines does — nine construction sites across
    eight files is nine chances to forget, and the gate is now a
    precondition for testing ANY page, not a per-file concern.

    Tests that deliberately exercise the UNAUTHENTICATED path build
    their own AppTest directly instead of calling this (see
    test_auth_coverage.py and test_auth_service.py) — they must resolve
    the path the same way, via page_path().

    Always pass an ABSOLUTE path (see page_path) — a relative one makes
    the resolution depend on the current working directory.
    """
    at = AppTest.from_file(str(page_path(path)))
    at.session_state["app_user"] = app_user
    return at


@pytest.fixture(scope="function")
def db_engine():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        # REQUIRE_DB=1 turns these skips into failures. Without it a
        # full run reports "176 passed" while ~334 DB-dependent tests
        # silently didn't execute, which reads as a green suite and is
        # how a 57-error path bug reached a real-Postgres verification
        # round (2026-08-18). Set REQUIRE_DB=1 for any run whose result
        # is going to be quoted as evidence the branch works.
        if os.environ.get("REQUIRE_DB"):
            pytest.fail(
                "REQUIRE_DB is set but TEST_DATABASE_URL is not — this run "
                "cannot verify anything DB-dependent. Set TEST_DATABASE_URL "
                "to a THROWAWAY database: this fixture opens with "
                "DROP SCHEMA public CASCADE and will destroy whatever it "
                "points at. Never point it at DATABASE_URL."
            )
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
