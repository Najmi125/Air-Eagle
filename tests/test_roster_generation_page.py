"""
tests/test_roster_generation_page.py

AppTest coverage for pages/6_Roster_Generation.py, same fixture
pattern as tests/test_roster_page.py / test_control_room_page.py /
test_assistant_page.py. First page test to exercise Phase 7's whole
chain end to end: create_template() -> expand_and_persist() ->
approve_instance() -> the page's own Generate/Publish, not synthetic
shortcuts — same discipline tests/test_roster_generator_service.py
already established for the service layer this page presents.

Dates are computed relative to dt.date.today() (never hardcoded) so
this file stays valid indefinitely, same discipline as every other
page test file this session.
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

    at = AppTest.from_file("pages/6_Roster_Generation.py")
    yield at

    get_engine.cache_clear()


# ------------------------------------------------------------------
# Grounding data — same real EPE 786/787 (domestic) and EPE 802/804/805
# (international) rotations as tests/test_roster_generator_service.py.
# ------------------------------------------------------------------
DOMESTIC_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
     "flight_no": "EPE 786", "domestic": True},
    {"leg_order": 2, "origin": "LHE", "destination": "KHI",
     "dep_time": dt.time(22, 0), "arr_time": dt.time(23, 45),
     "flight_no": "EPE 787", "domestic": True},
]
DOMESTIC_DAYS = [1, 2, 3, 4, 5]  # ISO weekday, Mon-Fri

INTERNATIONAL_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(1, 45), "arr_time": dt.time(3, 30),
     "flight_no": "EPE 802", "domestic": False},
    {"leg_order": 2, "origin": "LHE", "destination": "DWC",
     "dep_time": dt.time(4, 30), "arr_time": dt.time(8, 0),
     "flight_no": "EPE 804", "domestic": False},
    {"leg_order": 3, "origin": "DWC", "destination": "KHI",
     "dep_time": dt.time(9, 0), "arr_time": dt.time(11, 0),
     "flight_no": "EPE 805", "domestic": False},
]
INTERNATIONAL_DAYS = [2, 4, 5, 6]  # ISO weekday, Tue/Thu/Fri/Sat

_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY, "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY, "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY, "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY, "dg_expiry": _FAR_FUTURE_EXPIRY,
    # Missing date_of_birth triggers AE-CREW-PAIR-AGE-001_DOB_MISSING ->
    # NEEDS_MANUAL_REVIEW for every pairing, so no seat ever fills (found
    # 2026-08-09 in real-Postgres verification). A fixed, clearly-under-65
    # DOB keeps these tests exercising rest/FDP/coverage mechanics, not
    # the age-pairing gate.
    "date_of_birth": dt.date(1980, 1, 1),
}


def _next_weekday(start: dt.date, iso_weekday: int) -> dt.date:
    """Next date >= start whose isoweekday() == iso_weekday."""
    days_ahead = (iso_weekday - start.isoweekday()) % 7
    return start + dt.timedelta(days=days_ahead)


def _add_crew(role, **overrides):
    from services import crew_service
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _make_domestic_instance(date):
    from services import rotation_template_service as rts
    rts.create_template(
        rotation_code="EPE-786-787", days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2020, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-KHI domestic",
    )
    created = rts.expand_and_persist("EPE-786-787", date, date)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created[0]


def _make_international_instances(date_from, date_to):
    from services import rotation_template_service as rts
    rts.create_template(
        rotation_code="EPE-802-805", days_of_week=INTERNATIONAL_DAYS, legs=INTERNATIONAL_LEGS,
        effective_from=dt.date(2020, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-DWC-KHI international",
    )
    created = rts.expand_and_persist("EPE-802-805", date_from, date_to)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created


def _set_window(at, date_from, date_to):
    at.date_input[0].set_value(date_from)
    at.date_input[1].set_value(date_to)
    return at.run()


def _click(at, label):
    matches = [b for b in at.button if b.label == label]
    assert len(matches) == 1, f"expected exactly one {label!r} button, found {len(matches)}"
    matches[0].click()
    return at.run()


# ------------------------------------------------------------------
# Basics
# ------------------------------------------------------------------

def test_page_loads_without_exception(page_app):
    at = page_app.run()
    assert not at.exception


def test_no_approved_rotations_shows_templates_page_pointer(page_app):
    at = page_app.run()
    assert any("not built yet" in i.value for i in at.info)
    assert not any(b.label == "Generate" for b in at.button)


# ------------------------------------------------------------------
# Generate — happy path
# ------------------------------------------------------------------

def test_generate_fills_seats_and_shows_fairness_counts(page_app):
    date = _next_weekday(dt.date.today(), 1)  # next Monday
    _make_domestic_instance(date)
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")

    assert not at.exception
    assert any("All seats filled" in s.value for s in at.success)

    # No uncovered dataframe renders when uncovered is empty, so the
    # fairness tables are dataframe[0] (CPT) and dataframe[1] (FO).
    cpt_df = at.dataframe[0].value
    fo_df = at.dataframe[1].value
    assert cpt_df["Crew"].iloc[0].startswith(cpt_id)
    assert int(cpt_df["Duties filled"].iloc[0]) == 1
    assert fo_df["Crew"].iloc[0].startswith(fo_id)
    assert int(fo_df["Duties filled"].iloc[0]) == 1


# ------------------------------------------------------------------
# uncovered — shown first, both the structural (no-candidates) and the
# real rule-derived-reason case.
# ------------------------------------------------------------------

def test_uncovered_no_candidates_shown_before_fairness(page_app):
    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("FO")  # no CPT at all -- guaranteed, clean uncovered

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")

    assert not at.exception
    error_idx = next(i for i, el in enumerate(at.main) if type(el).__name__ == "Error")
    fairness_idx = next(
        i for i, el in enumerate(at.main)
        if type(el).__name__ == "Markdown" and "fairness check" in getattr(el, "value", "")
    )
    assert error_idx < fairness_idx

    uncovered_df = at.dataframe[0].value
    assert "No candidates in pool" in uncovered_df["Reason"].iloc[0]


def test_uncovered_real_rejection_reason_reaches_page(page_app):
    """Distinct from the no-candidates case above: this proves the
    actual legality-gate rejection string a controller would act on
    reaches the page unaltered, not just that the section renders in
    the right place. Back-to-back international duty for the sole CPT
    candidate is a real, already-established rest-math rejection
    (tests/test_roster_generator_service.py's own grounding case)."""
    thu = _next_weekday(dt.date.today(), 4)
    fri = thu + dt.timedelta(days=1)
    _make_international_instances(thu, fri)
    _add_crew("CPT")  # sole candidate -- no fairness escape route
    _add_crew("FO")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, thu, fri)
    at = _click(at, "Generate")

    assert not at.exception
    uncovered_df = at.dataframe[0].value
    fri_cpt = uncovered_df[(uncovered_df["Date"] == fri) & (uncovered_df["Role"] == "CPT")]
    assert len(fri_cpt) == 1
    reason = fri_cpt["Reason"].iloc[0]
    assert "REJECTED" in reason
    assert "No candidates in pool" not in reason


# ------------------------------------------------------------------
# Idempotency
# ------------------------------------------------------------------

def test_generate_twice_is_idempotent(page_app):
    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("CPT")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")
    assert any("All seats filled" in s.value for s in at.success)

    at = _click(at, "Generate")
    assert not at.exception
    assert any("2 seat(s) were already covered" in c.value for c in at.caption)


# ------------------------------------------------------------------
# Publish
# ------------------------------------------------------------------

def test_publish_shows_correct_count_and_flips_to_planned(page_app):
    from services import assignment_service

    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("CPT")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")

    # search_roster() is sector-level: the domestic rotation has 2 legs
    # x 2 crew (CPT+FO) = 4 rows, not 2 duties -- same row-vs-duty unit
    # publish_window() itself returns (confirmed in
    # tests/test_roster_generator_service.py's own publish test).
    assert any("4" in w.value and "PROPOSED roster row" in w.value for w in at.markdown)

    at = _click(at, "Publish")
    assert not at.exception
    assert any("Published 4 roster row" in s.value for s in at.success)

    roster = assignment_service.search_roster(date_from=date, date_to=date, include_proposed=True)
    assert set(roster["status"]) == {"PLANNED"}


def test_reject_then_publish_skips_cancelled_row(page_app):
    from services import assignment_service

    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    cpt_id = _add_crew("CPT")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")

    # search_roster() is sector-level (one row per leg) -- the domestic
    # rotation has 2 legs, so the CPT's duty spans 2 rows. Cancelling
    # only one leg would leave the other ACTIVE, which is not what
    # "unassign it on the Roster page" means operationally.
    proposed = assignment_service.search_roster(date_from=date, date_to=date, include_proposed=True)
    for _, cpt_row in proposed[proposed["crew_id"] == cpt_id].iterrows():
        assignment_service.remove_assignment(cpt_id, int(cpt_row["flight_id"]), "CPT", reason="test reject")

    at = at.run()  # rerun to pick up the cancellation before publishing (Publish is DB-fresh, not session-cached)
    at = _click(at, "Publish")
    assert not at.exception

    after = assignment_service.search_roster(
        date_from=date, date_to=date, include_proposed=True, include_cancelled=True)
    cpt_after = after[after["crew_id"] == cpt_id]
    fo_after = after[after["crew_id"] != cpt_id]
    assert set(cpt_after["status"]) == {"CANCELLED"}
    assert set(fo_after["status"]) == {"PLANNED"}


# ------------------------------------------------------------------
# Window-size warning
# ------------------------------------------------------------------

def test_window_warning_for_wide_window_not_for_default(page_app):
    today = dt.date.today()

    at = page_app.run()
    assert not any("spans" in w.value for w in at.warning)

    at = _set_window(at, today, today + dt.timedelta(days=40))
    assert any("spans" in w.value for w in at.warning)
