"""
tests/test_roster_page.py
"""
import os
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from streamlit.testing.v1 import AppTest

from db.db import get_engine


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = AppTest.from_file("pages/4_Roster.py")
    yield at

    get_engine.cache_clear()


def _seed_crew_and_flight():
    from services import crew_service, flight_service
    # All qualification expiry fields set far in the future so these
    # page tests exercise page/FDP/rest mechanics, not the
    # qualification gate (2026-07-31) — that gate has its own
    # dedicated tests in test_assignment_service.py.
    far_future = dt.date(2099, 1, 1)
    crew_id = crew_service.add_crew({
        "name": "Test Captain", "role": "CPT", "base": "KHI",
        "license_expiry": far_future, "medical_expiry": far_future,
        "sim_expiry": far_future, "route_check_expiry": far_future,
        "ir_expiry": far_future, "sep_expiry": far_future,
        "crm_expiry": far_future, "dg_expiry": far_future,
    })
    flight_id = flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 45),
        "arr_time_planned": dt.datetime(2026, 7, 20, 7, 45),
        "domestic": True,
    })
    return crew_id, flight_id


def test_page_loads_without_exception(page_app):
    _seed_crew_and_flight()
    at = page_app.run()
    assert not at.exception


def test_no_flights_shows_info_message(page_app):
    at = page_app.run()
    assert any("No flights in Flight Log" in i.value for i in at.info)


def test_legal_assignment_via_form_succeeds(page_app):
    crew_id, flight_id = _seed_crew_and_flight()
    at = page_app.run()

    at.multiselect[0].select(flight_id)
    at.selectbox[0].select(crew_id)   # Crew member
    at.selectbox[1].select("CPT")     # Role
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("ALLOWED" in s.value for s in at.success)

    from services import assignment_service
    assert len(assignment_service.get_roster_for_crew(crew_id)) == 1


def test_unassign_section_appears_after_assignment_and_works(page_app):
    crew_id, flight_id = _seed_crew_and_flight()

    from services import assignment_service
    assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    at = page_app.run()
    assert not at.exception

    # With an active assignment present, the unassign form's crew
    # selectbox and button should now be available.
    unassign_button = [b for b in at.button if b.label == "Unassign"]
    assert len(unassign_button) == 1

    unassign_button[0].click()
    at = at.run()

    assert any("Unassigned" in s.value for s in at.success)
    remaining = assignment_service.get_roster_for_crew(crew_id)
    assert len(remaining) == 0
