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
    the default=True page (home.py) with no exception.

    Needed extra patches for one day (2026-08-20) while the ops banner
    lived on Home: patching test_connection alone told home.py the
    database was reachable, which is the branch that then ran the
    banner's real queries — against no database, so they sat in
    connection retries until AppTest timed out. The banner moved to
    Control Room on 2026-08-21 and Home queries nothing again, so
    test_connection is once more the only patch this needs.
    """
    with patch("db.db.test_connection", return_value=True):
        at = authed_app_test("app.py")
        at.run()
        assert not at.exception
        assert any("Database connected" in s.value for s in at.success)


# ------------------------------------------------------------------
# Ops banner MOVED to Control Room (2026-08-21) — DB-free
# ------------------------------------------------------------------


def test_home_makes_no_service_queries_at_all():
    """The ops banner moved to Control Room, where a controller ACTS.
    Home is back to the DB status line plus navigation.

    Pinned as an absence rather than just deleting the banner's tests:
    Home is the page every session starts on, and it should stay the
    cheapest one in the app. Both services are patched to blow up — if
    Home reaches either, this fails."""
    def must_not_run(*args, **kwargs):
        raise AssertionError("home.py queried a service; it should not")

    with patch("db.db.test_connection", return_value=True), \
         patch("services.roster_generator_service.get_open_uncovered_seats", must_not_run), \
         patch("services.assignment_service.qualification_expiry_counts", must_not_run):
        at = authed_app_test("home.py")
        at.run()

    assert not at.exception
    assert at.metric == [], "no ops metrics belong on Home any more"
    assert any("Database connected" in s.value for s in at.success)
