"""The Roster page's "Current assignments" table: one row per flight,
two seats, names a controller can read.

It showed one row per crew member per sector with `crew_id`, `role`,
`duty_id`, `flight_id` and a serial column — internal identifiers on a
screen nobody debugs from.

The part worth guarding is not the column list. It is that a NULL
`operating_position` means two OPPOSITE things depending on grade, and
the two must not be conflated (operator decision, 2026-09-03):

  * on a CPT or FO it is an ANOMALY — someone holds a flight-deck seat
    the data failed to record, and dropping them hides a real
    assignment.
  * on an LM or ENGR it is NORMAL — they are outside the flight-deck
    model by design, so there is nothing to omit, and a flight
    carrying only them has no flight-deck assignment to show.

Handling the second like the first fills the table with cargo flights;
handling the first like the second swallows the anomaly. DB-free, so
these run where the page's own tests skip.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from services import assignment_service, crew_service, flight_service
from tests.conftest import authed_app_test

D = dt.datetime

# CPT-01, CPT-06 and FO-01 are all in CREW_DISPLAY_NAMES as of
# 2026-09-06, so they now render as the operator names them and the
# mechanical rule never runs for them. CPT-99 exists here for the
# tests that measure the RULE: it carries the same awkward stored name
# CPT-06 does and is deliberately NOT in the lookup, so the title
# stripping stays covered instead of quietly becoming untested the
# moment a real pilot was added to the table.
CREW = [
    {"crew_id": "CPT-01", "role": "CPT", "name": "MUHAMMAD WAQAR"},
    {"crew_id": "CPT-06", "role": "CPT", "name": "CAPT MUHAMMAD ASAD ALI "},
    {"crew_id": "CPT-99", "role": "CPT", "name": "CAPT MUHAMMAD ASAD ALI "},
    {"crew_id": "FO-01", "role": "FO", "name": "IBTISAM MUZZAFAR "},
    {"crew_id": "LM-01", "role": "LM", "name": "ABDULGHANI KHAN"},
]

FLIGHT_COLUMNS = ["flight_id", "flight_no", "origin", "destination",
                  "dep_time_planned", "arr_time_planned", "status", "domestic",
                  "aircraft", "other_occupants_operating",
                  "other_occupants_non_operating", "remarks", "rotation_instance_id"]


def _flight(flight_id, flight_no="EPE 786"):
    dep = D(2026, 9, 15, 19, 0) + dt.timedelta(hours=flight_id)
    return {"flight_id": flight_id, "flight_no": flight_no, "origin": "KHI",
            "destination": "LHE", "dep_time_planned": dep,
            "arr_time_planned": dep + dt.timedelta(hours=2), "status": "PLANNED",
            "domestic": True, "aircraft": "AP-BNW",
            "other_occupants_operating": "", "other_occupants_non_operating": "",
            "remarks": "", "rotation_instance_id": flight_id}


def _assignment(crew_id, role, position, duty_id="DUTY-1"):
    return {"crew_id": crew_id, "role_assigned": role,
            "operating_position": position, "duty_id": duty_id,
            "fdp_hours": 5.75, "status": "PLANNED"}


@pytest.fixture
def roster_page(monkeypatch):
    """Renders the REAL page over fake reads. `per_flight` maps
    flight_id -> list of assignment rows."""
    def render(per_flight, flight_nos=None):
        flight_nos = flight_nos or {}
        flights = pd.DataFrame(
            [_flight(fid, flight_nos.get(fid, "EPE 786")) for fid in sorted(per_flight)],
            columns=FLIGHT_COLUMNS)
        monkeypatch.setattr(flight_service, "get_all_flights",
                            lambda **k: flights.copy())
        monkeypatch.setattr(
            assignment_service, "get_roster_for_flight",
            lambda fid, **k: pd.DataFrame(
                per_flight[fid],
                columns=["crew_id", "role_assigned", "operating_position",
                         "duty_id", "fdp_hours", "status"]))
        monkeypatch.setattr(crew_service, "get_all_crew",
                            lambda **k: pd.DataFrame(CREW))
        # The flagged-for-review section (2026-09-05) reads on every
        # render. Unfaked it reaches for a real database and the
        # AppTest times out, which reads like a hang rather than a
        # missing patch — and against this machine's .env it would be
        # a live query.
        monkeypatch.setattr(
            assignment_service, "duties_needing_review",
            lambda **k: pd.DataFrame(columns=[
                "duty_id", "crew_id", "duty_date", "report_time", "debrief_time",
                "role_assigned", "operating_position", "flight_id",
                "flight_no", "origin", "destination"]))
        return authed_app_test("pages/4_Roster.py").run()
    return render


def _table(at):
    assert not at.exception, at.exception
    assert at.dataframe, "no table rendered"
    return at.dataframe[0].value


# ------------------------------------------------------------------
# Shape
# ------------------------------------------------------------------

def test_one_row_per_flight_not_one_per_crew_member(roster_page):
    at = roster_page({1: [_assignment("CPT-01", "CPT", "COMMANDER"),
                          _assignment("FO-01", "FO", "SECOND_PILOT")]})
    table = _table(at)

    assert len(table) == 1, "two crew on one flight must collapse to one row"
    row = table.iloc[0]
    assert row["Commander"] == "CPT Waqar"
    assert row["Second Pilot"] == "FO Ibtisam"


def test_internal_identifiers_are_gone(roster_page):
    """crew_id, role, duty_id and flight_id were on a screen nobody
    debugs from."""
    table = _table(roster_page({1: [_assignment("CPT-01", "CPT", "COMMANDER")]}))

    for gone in ("crew_id", "role", "duty_id", "flight_id", "fdp_hours"):
        assert gone not in table.columns, f"{gone} is still shown"


def test_the_flight_is_named_by_its_number(roster_page):
    table = _table(roster_page({1: [_assignment("CPT-01", "CPT", "COMMANDER")]},
                               flight_nos={1: "EPE 787"}))
    assert "EPE 787" in table.iloc[0]["Flight"]


def test_the_columns_say_commander_and_second_pilot(roster_page):
    """Not PIC/SIC. Both are the operator's vocabulary and
    migrations/016 records them as equivalent, but roster_coverage was
    standardised on these words on 2026-08-28 and one concept must not
    have two names across two screens."""
    table = _table(roster_page({1: [_assignment("CPT-01", "CPT", "COMMANDER")]}))

    assert "Commander" in table.columns and "Second Pilot" in table.columns
    assert "PIC" not in table.columns and "SIC" not in table.columns


# ------------------------------------------------------------------
# Names
# ------------------------------------------------------------------

def test_a_title_stored_inside_the_name_is_not_taken_for_a_given_name(roster_page):
    """CPT-06 is stored as "CAPT MUHAMMAD ASAD ALI" in production, so
    the naive rule initials a pilot from a rank: "CPT C Ali".

    Measured on CPT-99, which carries that same stored name and is NOT
    in CREW_DISPLAY_NAMES. CPT-06 himself is in the lookup now and
    renders "CPT Asad" without the rule running at all — pointing this
    test at him would have left the stripping untested while still
    passing."""
    table = _table(roster_page({1: [_assignment("CPT-99", "CPT", "COMMANDER")]}))
    assert table.iloc[0]["Commander"] == "CPT M Ali"


def test_a_pilot_in_the_lookup_is_named_the_way_the_operator_names_him(roster_page):
    """The other half of the pair above, on the same stored name: the
    rule gives "CPT M Ali", the operator says "Asad"."""
    table = _table(roster_page({1: [_assignment("CPT-06", "CPT", "COMMANDER")]}))
    assert table.iloc[0]["Commander"] == "CPT Asad"


# ------------------------------------------------------------------
# The two meanings of a missing operating_position
# ------------------------------------------------------------------

def test_an_empty_seat_reads_uncovered(roster_page):
    table = _table(roster_page({1: [_assignment("CPT-01", "CPT", "COMMANDER")]}))
    assert table.iloc[0]["Second Pilot"] == "UNCOVERED"


def test_a_cockpit_crew_member_with_no_recorded_seat_is_surfaced(roster_page):
    """ANOMALY. An FO holding no seat is a real assignment the data
    failed to place, and must not vanish."""
    at = roster_page({1: [_assignment("CPT-01", "CPT", "COMMANDER"),
                          _assignment("FO-01", "FO", None)]})
    table = _table(at)

    assert "Seat not recorded" in table.columns
    assert "FO Ibtisam" in table.iloc[0]["Seat not recorded"]
    # And the seat they did not fill is still uncovered, not quietly
    # treated as covered by them.
    assert table.iloc[0]["Second Pilot"] == "UNCOVERED"


def test_an_lm_with_no_recorded_seat_is_normal_and_not_flagged(roster_page):
    """NOT an anomaly. LM and ENGR have no operating position by
    design. Flagging them would fill the column with noise and train a
    controller to ignore it — which is how the real anomaly above gets
    swallowed."""
    table = _table(roster_page({1: [_assignment("CPT-01", "CPT", "COMMANDER"),
                                    _assignment("LM-01", "LM", None)]}))

    assert "Seat not recorded" not in table.columns
    assert "LM-01" not in str(table.iloc[0].to_dict())


def test_a_flight_carrying_only_ground_crew_does_not_appear(roster_page):
    """"Current assignments" is the flight-deck pair. A flight with
    both seats empty because somebody loaded cargo is noise — the same
    reason a wholly uncrewed flight stays out, and Roster Generation's
    uncovered panel is where those live."""
    at = roster_page({1: [_assignment("LM-01", "LM", None)]})

    assert not at.exception, at.exception
    assert not at.dataframe, "a cargo-only flight has no flight-deck assignment"
    assert any("No crew assigned yet" in i.value for i in at.info)


def test_a_crewed_flight_still_appears_beside_a_cargo_only_one(roster_page):
    """The exclusion must be per flight, not a filter that swallows the
    whole table."""
    table = _table(roster_page({
        1: [_assignment("LM-01", "LM", None)],
        2: [_assignment("CPT-01", "CPT", "COMMANDER")],
    }))

    assert len(table) == 1
    assert table.iloc[0]["Commander"] == "CPT Waqar"
