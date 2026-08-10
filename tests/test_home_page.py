"""
tests/test_home_page.py

AppTest coverage for app.py (2026-08-10 branding pass), same page_app
fixture pattern as every other page test file. Covers what pytest
actually can verify — the page loads without exception (with the real
assets/logo.png path resolved, and the real assets/AE-image.jpg
base64-encoded into the injected CSS) and the DB-status message stays
present in the element tree. Whether the background/panel/logo
actually LOOK right and stay legible is a visual judgment pytest
cannot make — verified separately via a manual `streamlit run app.py`,
not claimed here.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from streamlit.testing.v1 import AppTest

from db.db import get_engine


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = AppTest.from_file("app.py")
    yield at

    get_engine.cache_clear()


def test_page_loads_without_exception_and_shows_db_connected(page_app):
    at = page_app.run()
    assert not at.exception
    assert any("Database connected" in s.value for s in at.success)
    assert any("Air Eagle" in t.value for t in at.title)


def test_page_loads_without_exception_when_db_unreachable():
    """DB-down path exercised via a mocked test_connection() (not the
    real migrated_db fixture) -- confirms the error branch renders
    cleanly (no traceback from the background/logo additions) and the
    DB-status message is still present and readable in the element
    tree, exactly the message a controller needs at 0300."""
    with patch("db.db.test_connection", return_value="connection refused"):
        at = AppTest.from_file("app.py")
        at.run()
        assert not at.exception
        assert any("connection refused" in e.value for e in at.error)
