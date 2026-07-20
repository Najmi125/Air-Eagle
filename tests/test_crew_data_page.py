"""
tests/test_crew_data_page.py

Uses Streamlit's AppTest framework to actually drive
pages/2_Crew_Data.py — fill in the form, submit it, check the result
— rather than only testing the service layer underneath it. This is
the one place in the test suite that exercises the UI layer itself,
not just what it calls.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from streamlit.testing.v1 import AppTest

from db.db import get_engine


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    """
    AppTest runs the real page file, which imports db.db.get_engine()
    on its own — it doesn't go through the migrated_db fixture's
    engine object directly. Point DATABASE_URL at the same test DB
    migrated_db just set up, and clear get_engine's lru_cache so it
    picks up the change rather than reusing a stale cached engine
    from a previous test.
    """
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = AppTest.from_file("pages/2_Crew_Data.py")
    yield at

    get_engine.cache_clear()


def test_page_loads_without_exception(page_app):
    at = page_app.run()
    assert not at.exception


def test_empty_crew_list_shows_info_message(page_app):
    at = page_app.run()
    assert any("No crew records yet" in info.value for info in at.info)


def test_add_crew_via_form_succeeds_and_shows_in_table(page_app):
    at = page_app.run()

    at.text_input[0].input("Test Captain")  # Name *
    at.selectbox[0].select("CPT")            # Role *
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("Added Test Captain as CPT-01" in s.value for s in at.success)

    at = at.run()  # rerun triggered by st.rerun() inside the callback
    df = at.dataframe[0].value
    assert "CPT-01" in list(df["crew_id"])
    assert "Test Captain" in list(df["name"])


def test_add_crew_missing_name_shows_error(page_app):
    at = page_app.run()

    # Name left blank, only role selected
    at.selectbox[0].select("FO")
    at.button[0].click()
    at = at.run()

    assert any("Missing required crew field" in e.value for e in at.error)
