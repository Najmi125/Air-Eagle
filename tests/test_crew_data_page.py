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
from db.db import get_engine
from tests.conftest import authed_app_test


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

    at = authed_app_test("pages/2_Crew_Data.py")
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


def test_role_dropdown_excludes_lm_and_engr(page_app):
    """Air Eagle's crew records are CPT/FO only (2026-08-02 operator
    decision, same reasoning as scripts/import_crew_from_xlsx.py's
    EXCLUDED_ROLES) — the manual-add form must not offer LM or ENGR as
    selectable roles either, closing the inconsistency where imports
    excluded them but a controller could still hand-create one here.
    'Other' stays, as the escape hatch for a genuinely unanticipated
    role, not for LM/AME specifically."""
    at = page_app.run()
    options = at.selectbox[0].options
    assert options == ["CPT", "FO", "Other"]
    assert "LM" not in options
    assert "ENGR" not in options


def test_add_crew_missing_name_shows_error(page_app):
    at = page_app.run()

    # Name left blank, only role selected
    at.selectbox[0].select("FO")
    at.button[0].click()
    at = at.run()

    assert any("Missing required crew field" in e.value for e in at.error)
