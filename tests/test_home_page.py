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

st.page_link() renders as a generic UnknownElement in AppTest -- no
dedicated element type, no .click(). These tests can only confirm each
page-link's label is present in the element tree, not that clicking
one actually navigates -- said plainly, not left implied.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from streamlit.testing.v1 import AppTest

from db.db import get_engine

PAGE_LINK_LABELS = [
    "Control Room", "Crew Data", "Flight Log", "Roster",
    "OCC Assistant", "Roster Generation", "Schedule Templates",
]


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = AppTest.from_file("home.py")
    yield at

    get_engine.cache_clear()


def _page_link_labels(at):
    return [
        el.label for el in at.main
        if type(el).__name__ == "UnknownElement" and el.label
    ]


def test_page_loads_without_exception_and_shows_db_connected(page_app):
    at = page_app.run()
    assert not at.exception
    assert any("Database connected" in s.value for s in at.success)
    # The leading emoji lands in .icon, not .value -- see module docstring.
    assert any(s.icon == "🟢" for s in at.success)
    assert any("Operations Control Centre" in m.value for m in at.markdown)


def test_utc_clock_is_inline_with_db_status_and_page_links_are_all_present(page_app):
    """UTC lives in the DB-status success message itself (2026-08-12,
    moved there per operator feedback), not a separate markdown line."""
    at = page_app.run()
    assert any("UTC" in s.value for s in at.success)
    labels = _page_link_labels(at)
    for expected in PAGE_LINK_LABELS:
        assert expected in labels


def test_page_loads_without_exception_when_db_unreachable():
    """DB-down path exercised via a mocked test_connection() (not the
    real migrated_db fixture) -- confirms the error branch renders
    cleanly (no traceback from the logo/background/panel additions)
    and the DB-status message is still present and readable."""
    with patch("db.db.test_connection", return_value="connection refused"):
        at = AppTest.from_file("home.py")
        at.run()
        assert not at.exception
        assert any("connection refused" in e.value for e in at.error)


def test_router_loads_the_default_home_page_without_exception():
    """app.py itself -- confirms every st.Page() path resolves (no
    typo'd filename) and st.navigation()/pg.run() correctly renders
    the default=True page (home.py) with no exception."""
    with patch("db.db.test_connection", return_value=True):
        at = AppTest.from_file("app.py")
        at.run()
        assert not at.exception
        assert any("Database connected" in s.value for s in at.success)
