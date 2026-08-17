"""
tests/test_control_room_page.py

Rebuilt for the flight-deck crew package (2026-08-13): the page now
has a "Crew type" radio (defaults to the flight-deck pair path) that
determines which fields render — Commander/Second Pilot selectboxes
live OUTSIDE the form (read before submission, same reasoning as
every other live-updating control in this codebase), the single
crew_id/role selectboxes for LM/ENGR/Other live INSIDE the form and
only render when "Single crew" is selected.
"""
import os
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from db.db import get_engine
from tests.conftest import authed_app_test


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = authed_app_test("pages/1_Control_Room.py")
    yield at

    get_engine.cache_clear()


_FAR_FUTURE = dt.date(2099, 1, 1)
_QUAL_DEFAULTS = {
    "license_expiry": _FAR_FUTURE, "medical_expiry": _FAR_FUTURE,
    "sim_expiry": _FAR_FUTURE, "route_check_expiry": _FAR_FUTURE,
    "ir_expiry": _FAR_FUTURE, "sep_expiry": _FAR_FUTURE,
    "crm_expiry": _FAR_FUTURE, "dg_expiry": _FAR_FUTURE,
}


def _seed_crew(role="LM", **overrides):
    from services import crew_service
    # All qualification expiry fields set far in the future so these
    # page tests exercise page/FDP/rest mechanics, not the
    # qualification gate (2026-07-31) — that gate has its own
    # dedicated tests in test_assignment_service.py.
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUAL_DEFAULTS)
    crew_data.update(overrides)
    from services import crew_service as cs
    return cs.add_crew(crew_data)


def _seed_pair(commander_dob=dt.date(1980, 1, 1), second_pilot_dob=dt.date(1985, 1, 1)):
    cpt = _seed_crew("CPT", date_of_birth=commander_dob)
    fo = _seed_crew("FO", date_of_birth=second_pilot_dob)
    return cpt, fo


def test_page_loads_without_exception(page_app):
    _seed_pair()
    at = page_app.run()
    assert not at.exception


def test_no_active_crew_shows_warning(page_app):
    at = page_app.run()
    assert any("No active crew" in w.value for w in at.warning)


def test_no_eligible_pair_shows_warning(page_app):
    """Only an LM on file — no CPT/FO at all — the pair path (the
    default crew type) must warn rather than error, since there's no
    eligible Commander or Second Pilot pool to build a form from."""
    _seed_crew("LM")
    at = page_app.run()
    assert not at.exception
    assert any("active CPT" in w.value for w in at.warning)


def test_legal_pair_assignment_saves_flight_and_shows_success(page_app):
    cpt, fo = _seed_pair()
    at = page_app.run()

    # Pair is the default crew type — no radio interaction needed.
    at.selectbox[0].select(cpt)  # Commander
    at.selectbox[1].select(fo)   # Second Pilot
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
    assert len(assignment_service.get_roster_for_crew(cpt)) == 1
    assert len(assignment_service.get_roster_for_crew(fo)) == 1


def test_illegal_pair_assignment_shows_rejection_and_saves_nothing(page_app):
    cpt, fo = _seed_pair()

    # Seed a prior 8h-FDP duty for the Commander directly — an 8h duty
    # now correctly triggers NEEDS_MANUAL_REVIEW through the real
    # assign_pair_to_new_flights() call (no meal/snack data
    # populated, tested separately), so it's seeded here as GIVEN
    # history instead, same pattern as test_assignment_service.py's
    # own _seed_duty helper.
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
            "crew_id": cpt, "flight_id": prior_flight,
            "duty_date": dt.date(2026, 7, 20),
            "report_time": dt.datetime(2026, 7, 20, 4, 15),
            "debrief_time": dt.datetime(2026, 7, 20, 12, 15),
            "fdp_hours": 8.0, "role_assigned": "CPT",
        })

    at = page_app.run()
    at.selectbox[0].select(cpt)
    at.selectbox[1].select(fo)
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
    # ad-hoc attempt must not have saved a second one, for either seat.
    assert len(flight_service.get_all_flights()) == 1


def test_needs_review_pair_assignment_shows_warning_not_success(page_app):
    """Regression guard for a real bug already fixed once on this
    page (pre-pair-model): a held assignment must never fall into the
    ALLOWED branch, which references flight_ids[0] — genuinely empty
    for a held assignment.

    Trigger: a missing qualification-expiry field on the Commander
    (AE-CREW-QUAL-001_LICENSE_EXPIRY_MISSING)."""
    from services import crew_service
    cpt, fo = _seed_pair()
    crew_service.update_crew(cpt, {"license_expiry": None})

    at = page_app.run()
    at.selectbox[0].select(cpt)
    at.selectbox[1].select(fo)
    at.text_input[0].input("KHI")
    at.text_input[1].input("LHE")
    at.date_input[0].set_value(dt.date(2026, 7, 20))
    at.time_input[0].set_value(dt.time(5, 0))
    at.date_input[1].set_value(dt.date(2026, 7, 20))
    at.time_input[1].set_value(dt.time(7, 0))
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("HELD FOR MANUAL REVIEW" in w.value for w in at.warning)
    assert not any("ALLOWED" in s.value for s in at.success)

    from services import flight_service as fs
    assert len(fs.get_all_flights()) == 0  # nothing saved


def test_single_crew_path_still_works_for_lm(page_app):
    """The unaffected LM/ENGR/Other path — switching Crew type off
    the default pair option renders the single crew_id/role form
    instead."""
    lm = _seed_crew("LM")
    at = page_app.run()

    at.radio[0].set_value("Single crew (LM / ENGR / Other)")
    at = at.run()

    at.text_input[0].input("KHI")
    at.text_input[1].input("LHE")
    at.date_input[0].set_value(dt.date(2026, 7, 20))
    at.time_input[0].set_value(dt.time(5, 45))
    at.date_input[1].set_value(dt.date(2026, 7, 20))
    at.time_input[1].set_value(dt.time(7, 45))
    at.selectbox[0].select(lm)     # Crew member
    at.selectbox[1].select("LM")   # Role
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("ALLOWED" in s.value for s in at.success)

    from services import assignment_service
    assert len(assignment_service.get_roster_for_crew(lm)) == 1
