"""
tests/test_control_room_page.py
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

    at = AppTest.from_file("pages/1_Control_Room.py")
    yield at

    get_engine.cache_clear()


def _seed_crew():
    from services import crew_service
    return crew_service.add_crew({"name": "Test Captain", "role": "CPT", "base": "KHI"})


def test_page_loads_without_exception(page_app):
    _seed_crew()
    at = page_app.run()
    assert not at.exception


def test_no_active_crew_shows_warning(page_app):
    at = page_app.run()
    assert any("No active crew" in w.value for w in at.warning)


def test_legal_adhoc_assignment_saves_flight_and_shows_success(page_app):
    _seed_crew()
    at = page_app.run()

    at.text_input[0].input("KHI")   # Origin
    at.text_input[1].input("LHE")   # Destination
    at.date_input[0].set_value(dt.date(2026, 7, 20))
    at.time_input[0].set_value(dt.time(5, 45))
    at.date_input[1].set_value(dt.date(2026, 7, 20))
    at.time_input[1].set_value(dt.time(7, 45))
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("ALLOWED" in s.value for s in at.success)

    from services import flight_service, assignment_service
    assert len(flight_service.get_all_flights()) == 1
    assert len(assignment_service.get_roster_for_crew("CPT-01")) == 1


def test_illegal_adhoc_assignment_shows_rejection_and_saves_nothing(page_app):
    crew_id = _seed_crew()

    # Seed a prior 8h-FDP duty directly — an 8h duty now correctly
    # triggers NEEDS_MANUAL_REVIEW through the real
    # assign_crew_to_duty() call (no meal/snack data populated,
    # tested separately), so it's seeded here as GIVEN history
    # instead, the same way test_assignment_service.py's _seed_duty
    # helper does it.
    from services import flight_service
    from sqlalchemy import text
    prior_flight = flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 0),
        "arr_time_planned": dt.datetime(2026, 7, 20, 12, 0),
        "domestic": True,
    })
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                report_time, debrief_time, fdp_hours, role_assigned)
            VALUES (:crew_id, :flight_id, 'SEEDED-PRIOR', :duty_date,
                :report_time, :debrief_time, :fdp_hours, :role_assigned)
        """), {
            "crew_id": crew_id, "flight_id": prior_flight,
            "duty_date": dt.date(2026, 7, 20),
            "report_time": dt.datetime(2026, 7, 20, 4, 15),
            "debrief_time": dt.datetime(2026, 7, 20, 12, 15),
            "fdp_hours": 8.0, "role_assigned": "CPT",
        })

    at = page_app.run()
    at.text_input[0].input("KHI")
    at.text_input[1].input("LHE")
    at.date_input[0].set_value(dt.date(2026, 7, 20))
    at.time_input[0].set_value(dt.time(17, 45))   # only 5h after prior debrief (12:15)
    at.date_input[1].set_value(dt.date(2026, 7, 20))
    at.time_input[1].set_value(dt.time(19, 45))
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("REJECTED" in e.value for e in at.error)

    # Only the ONE prior (legal) flight should exist — the illegal
    # ad-hoc attempt must not have saved a second one.
    assert len(flight_service.get_all_flights()) == 1


def test_needs_review_adhoc_assignment_shows_warning_not_success(page_app):
    """Regression guard for a real bug: before the NEEDS_REVIEW
    branch was added to this page, a held assignment would fall into
    the 'else' (ALLOWED) branch, which references flight_ids[0] —
    but flight_ids is genuinely empty for a held assignment, so this
    would have raised an IndexError, not just shown a misleading
    success message."""
    _seed_crew()

    at = page_app.run()
    at.text_input[0].input("KHI")
    at.text_input[1].input("LHE")
    at.date_input[0].set_value(dt.date(2026, 7, 20))
    at.time_input[0].set_value(dt.time(5, 0))
    at.date_input[1].set_value(dt.date(2026, 7, 20))
    at.time_input[1].set_value(dt.time(11, 0))   # 7h FDP -> D25 nutrition data missing
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("HELD FOR MANUAL REVIEW" in w.value for w in at.warning)
    assert not any("ALLOWED" in s.value for s in at.success)

    from services import flight_service as fs
    assert len(fs.get_all_flights()) == 0  # nothing saved
