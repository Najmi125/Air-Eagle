"""
tests/test_roster_page.py

Rebuilt for the flight-deck crew package (2026-08-13): the assignment
form is now a Commander + Second Pilot pair form (assign_pair_to_duty()),
plus the unaffected LM/ENGR/Other single-crew form, plus a duty-scoped
unassign (remove_assignment_from_duty(), one selectbox + one button,
not a per-row button list).
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


_FAR_FUTURE = dt.date(2099, 1, 1)
_QUAL_DEFAULTS = {
    "license_expiry": _FAR_FUTURE, "medical_expiry": _FAR_FUTURE,
    "sim_expiry": _FAR_FUTURE, "route_check_expiry": _FAR_FUTURE,
    "ir_expiry": _FAR_FUTURE, "sep_expiry": _FAR_FUTURE,
    "crm_expiry": _FAR_FUTURE, "dg_expiry": _FAR_FUTURE,
}


def _seed_crew(role, **overrides):
    from services import crew_service
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUAL_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _seed_flight():
    from services import flight_service
    return flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 45),
        "arr_time_planned": dt.datetime(2026, 7, 20, 7, 45),
        "domestic": True,
    })


def _seed_pair_and_flight(commander_dob=dt.date(1980, 1, 1), second_pilot_dob=dt.date(1985, 1, 1)):
    cpt = _seed_crew("CPT", date_of_birth=commander_dob)
    fo = _seed_crew("FO", date_of_birth=second_pilot_dob)
    flight_id = _seed_flight()
    return cpt, fo, flight_id


def test_page_loads_without_exception(page_app):
    _seed_pair_and_flight()
    at = page_app.run()
    assert not at.exception


def test_no_flights_shows_info_message(page_app):
    at = page_app.run()
    assert any("No flights in Flight Log" in i.value for i in at.info)


def test_legal_pair_assignment_via_form_succeeds(page_app):
    cpt, fo, flight_id = _seed_pair_and_flight()
    at = page_app.run()

    at.multiselect[0].select(flight_id)   # pair_flights
    at.selectbox[0].select(cpt)           # Commander
    at.selectbox[1].select(fo)            # Second Pilot
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("ALLOWED" in s.value for s in at.success)

    from services import assignment_service
    assert len(assignment_service.get_roster_for_crew(cpt)) == 1
    assert len(assignment_service.get_roster_for_crew(fo)) == 1


def test_illegal_pair_age_pairing_shows_rejection(page_app):
    """Both 65+, domestic — the age-pairing rule (AE-CREW-PAIR-AGE-001)
    rejects the pair, and the form must show REJECTED with neither
    seat written."""
    cpt, fo, flight_id = _seed_pair_and_flight(
        commander_dob=dt.date(1959, 1, 1), second_pilot_dob=dt.date(1958, 1, 1))
    at = page_app.run()

    at.multiselect[0].select(flight_id)
    at.selectbox[0].select(cpt)
    at.selectbox[1].select(fo)
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("REJECTED" in e.value for e in at.error)

    from services import assignment_service
    assert len(assignment_service.get_roster_for_crew(cpt)) == 0
    assert len(assignment_service.get_roster_for_crew(fo)) == 0


def test_legal_other_crew_assignment_via_form_succeeds(page_app):
    """The unaffected LM/ENGR/Other path — with no CPT/FO on file at
    all, the pair section shows its warning only (no widgets), so this
    form's own multiselect/selectboxes are the first on the page."""
    lm = _seed_crew("LM")
    flight_id = _seed_flight()
    at = page_app.run()

    assert any("active CPT" in w.value for w in at.warning)  # pair section's warning

    at.multiselect[0].select(flight_id)   # other_flights
    at.selectbox[0].select(lm)            # Crew member
    at.selectbox[1].select("LM")          # Role
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("ALLOWED" in s.value for s in at.success)

    from services import assignment_service
    assert len(assignment_service.get_roster_for_crew(lm)) == 1


def test_unassign_section_removes_one_pilots_duty_only(page_app):
    """The default selectbox selection (index 0) is the Commander's
    own duty — written first inside assign_pair_to_duty()'s single
    transaction, so it's the first row active_df picks up. Unassigning
    it must not touch the Second Pilot's own separate duty_id for the
    same flight."""
    from services import assignment_service
    cpt, fo, flight_id = _seed_pair_and_flight()
    result = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])
    assert result.status == "ALLOWED"

    at = page_app.run()
    assert not at.exception

    unassign_button = [b for b in at.button if b.label == "Unassign"]
    assert len(unassign_button) == 1

    unassign_button[0].click()
    at = at.run()

    assert any("Unassigned" in s.value for s in at.success)
    assert len(assignment_service.get_roster_for_crew(cpt)) == 0
    assert len(assignment_service.get_roster_for_crew(fo)) == 1  # untouched
