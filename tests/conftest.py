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
import pytest
from sqlalchemy import create_engine, text


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
