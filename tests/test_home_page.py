"""
tests/test_home_page.py

AppTest coverage for home.py (the Home page's actual content) and a
light check on app.py (the st.navigation() router that hands off to
it and the other seven pages). Same page_app fixture pattern as every
other page test.

st.success()'s leading-emoji behavior, confirmed directly (2026-08-11):
a leading emoji in body is extracted into a separate, dedicated icon
slot and REMOVED from the body text (Streamlit's own
extract_leading_icon, displayed "slightly enlarged" next to the
alert) -- st.success("🟢 Database connected") does NOT leave the
emoji in .value; it shows up in .icon (backed by .proto.icon) instead.
Asserted against .icon here, not .value, since that's what the code
actually produces and what a naive .value check would have silently
gotten wrong.

Page-link buttons (added 2026-08-12, removed same day per operator
request -- genuinely redundant with the sidebar's own automatic page
list from st.navigation()) are no longer part of this page; no test
coverage for them here.
"""
import datetime as dt
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
from db.db import get_engine
from tests.conftest import authed_app_test


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = authed_app_test("home.py")
    yield at

    get_engine.cache_clear()


def test_page_loads_without_exception_and_shows_db_connected(page_app):
    at = page_app.run()
    assert not at.exception
    assert any("Database connected" in s.value for s in at.success)
    # The leading emoji lands in .icon, not .value -- see module docstring.
    assert any(s.icon == "🟢" for s in at.success)
    assert any("Operations Control Centre" in m.value for m in at.markdown)


def test_utc_clock_is_inline_with_db_status(page_app):
    """UTC lives in the DB-status success message itself (2026-08-12,
    moved there per operator feedback), not a separate markdown line."""
    at = page_app.run()
    assert any("UTC" in s.value for s in at.success)
    # dd-mm-yyyy, dashed -- computed relative to "today", never hardcoded.
    today_dashed = dt.datetime.now(dt.timezone.utc).strftime("%d-%m-%Y")
    assert any(today_dashed in s.value for s in at.success)


def test_nav_text_points_to_sidebar_only(page_app):
    """No page-link buttons on this page (removed 2026-08-12) -- the
    sidebar's own automatic page list is the only navigation surface,
    per the operator's own call."""
    at = page_app.run()
    assert any(w.value == "Use the sidebar to navigate." for w in at.markdown)
    assert not any(type(el).__name__ == "UnknownElement" for el in at.main)


def test_page_loads_without_exception_when_db_unreachable():
    """DB-down path exercised via a mocked test_connection() (not the
    real migrated_db fixture) -- confirms the error branch renders
    cleanly (no traceback from the logo/background/panel additions)
    and the DB-status message is still present and readable."""
    with patch("db.db.test_connection", return_value="connection refused"):
        at = authed_app_test("home.py")
        at.run()
        assert not at.exception
        assert any("connection refused" in e.value for e in at.error)


def test_router_loads_the_default_home_page_without_exception():
    """app.py itself -- confirms every st.Page() path resolves (no
    typo'd filename) and st.navigation()/pg.run() correctly renders
    the default=True page (home.py) with no exception."""
    with patch("db.db.test_connection", return_value=True):
        at = authed_app_test("app.py")
        at.run()
        assert not at.exception
        assert any("Database connected" in s.value for s in at.success)


# ------------------------------------------------------------------
# Ops status banner (2026-08-20) — DB-free
# ------------------------------------------------------------------

def test_ops_banner_does_not_query_a_database_known_to_be_unreachable():
    """Regression: the banner must be SKIPPED, not merely wrapped, when
    the connection check has already failed.

    try/except catches a failing query but not a hanging one. With the
    queries unconditional, an unreachable database left them sitting in
    connection retries and the home page took over three seconds to
    render — it was still correct, it just stopped being usable at the
    moment an operator most needs to see something. Asserted by making
    either query an outright failure: if the page reaches them at all,
    this test fails."""
    called = []

    def must_not_run(*args, **kwargs):
        called.append(args)
        raise AssertionError("ops banner queried an unreachable database")

    with patch("db.db.test_connection", return_value="connection refused"), \
         patch("services.roster_generator_service.get_open_uncovered_seats", must_not_run), \
         patch("services.assignment_service.qualification_expiry_counts", must_not_run):
        at = authed_app_test("home.py")
        at.run()

    assert not at.exception
    assert called == [], "no ops query may run when the DB is unreachable"
    assert any("Ops status unavailable" in c.value for c in at.caption)


def test_ops_banner_renders_counts_when_the_database_is_up():
    """The healthy path — and that the two document counts stay
    SEPARATE. A document expiring today is already expired under the
    legality gate's boundary, so folding them into one number would let
    a controller read a blocking document as one they still have time to
    renew."""
    with patch("db.db.test_connection", return_value=True), \
         patch("services.roster_generator_service.get_open_uncovered_seats",
               return_value=pd.DataFrame([{"x": 1}, {"x": 2}])), \
         patch("services.assignment_service.qualification_expiry_counts",
               return_value={"expired": 3, "expiring": 1, "horizon_days": 7}):
        at = authed_app_test("home.py")
        at.run()

    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Uncovered rotation seats today"] == "2"
    assert metrics["Crew with expired documents"] == "3"
    assert metrics["Crew with documents expiring in 7 days"] == "1"


def test_ops_banner_survives_one_query_failing_on_its_own():
    """Connection up, one query broken — a missing table, a migration
    not yet applied. The other half must still render."""
    with patch("db.db.test_connection", return_value=True), \
         patch("services.roster_generator_service.get_open_uncovered_seats",
               side_effect=RuntimeError("relation uncovered_seats does not exist")), \
         patch("services.assignment_service.qualification_expiry_counts",
               return_value={"expired": 2, "expiring": 0, "horizon_days": 7}):
        at = authed_app_test("home.py")
        at.run()

    assert not at.exception
    assert any("Uncovered seats unavailable" in c.value for c in at.caption)
    assert {m.label: m.value for m in at.metric}["Crew with expired documents"] == "2"
