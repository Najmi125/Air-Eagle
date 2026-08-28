"""Seat (operating_position) versus grade (role_assigned), DB-free.

Under the flight-deck pair model a CPT may legitimately occupy the
Second Pilot seat, so the two are not interchangeable and never have
been. This has now been the same defect three times — the Control Room
status board, `reports.roster_coverage()`, and
`roster_generator_service._seed_duty_counts()` — which is why these
guards live in one file named after the distinction rather than being
scattered through the suites of the three modules.

DB-FREE ON PURPOSE. The equivalent checks in
tests/test_assistant_reports.py are DB-gated and skip wherever Postgres
is absent, which is exactly where the first two instances survived
review. These run everywhere.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import services.assignment_service as assignment_service
import services.assistant.reports as reports
import services.flight_service as flight_service
import services.roster_generator_service as rgs
from services.assistant.query_parser import ReportRequest


_DEP = dt.datetime(2026, 7, 5, 5, 0)
_ARR = dt.datetime(2026, 7, 5, 8, 0)

_ROSTER_COLUMNS = ["crew_id", "role_assigned", "operating_position", "status"]


def _flight(flight_id=1, **overrides):
    row = {
        "flight_id": flight_id, "flight_no": "EPE 787", "origin": "LHE",
        "destination": "KHI", "dep_time_planned": _DEP, "arr_time_planned": _ARR,
        "status": "PLANNED", "other_occupants_operating": "",
        "other_occupants_non_operating": "", "remarks": "",
    }
    row.update(overrides)
    return row


def _roster_row(crew_id, role_assigned, operating_position):
    return {"crew_id": crew_id, "role_assigned": role_assigned,
            "operating_position": operating_position, "status": "PLANNED"}


def _coverage(monkeypatch, roster_rows, flight_overrides=None):
    """Runs the real roster_coverage() over fake reads."""
    flights = pd.DataFrame([_flight(**(flight_overrides or {}))])
    monkeypatch.setattr(flight_service, "get_all_flights",
                        lambda **k: flights.copy())
    monkeypatch.setattr(assignment_service, "get_roster_for_flight",
                        lambda fid, **k: pd.DataFrame(roster_rows,
                                                      columns=_ROSTER_COLUMNS))
    request = ReportRequest(template="roster_coverage", resolved=True, reason="ok",
                            date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31))
    dataset = reports.roster_coverage(request)
    return dataset, dict(zip(dataset.headers, dataset.rows[0]))


# ------------------------------------------------------------------
# reports.roster_coverage()
# ------------------------------------------------------------------

def test_a_cpt_cpt_pair_is_reported_as_two_seats_not_two_commanders(monkeypatch):
    """The production defect (2026-08-31, flights 15/16): a fully
    crewed flight whose Second Pilot is CPT-graded rendered as two
    Commanders and an UNCOVERED Second Pilot.

    The database forbids the reading it produced —
    uq_roster_flight_operating_position_active (migrations/016) makes
    two active COMMANDER rows on one flight impossible — so the report
    was describing a state that cannot exist."""
    dataset, row = _coverage(monkeypatch, [
        _roster_row("CPT-04", "CPT", "COMMANDER"),
        _roster_row("CPT-03", "CPT", "SECOND_PILOT"),
    ])

    assert row["Commander"] == "CPT-04"
    assert row["Second Pilot"] == "CPT-03"
    assert "UNCOVERED" not in (row["Commander"], row["Second Pilot"])
    assert not any("uncovered cockpit seat" in note for note in dataset.notes)


def test_an_fo_in_the_second_pilot_seat_still_reads_normally(monkeypatch):
    """The ordinary case must not regress while fixing the odd one."""
    _, row = _coverage(monkeypatch, [
        _roster_row("CPT-01", "CPT", "COMMANDER"),
        _roster_row("FO-01", "FO", "SECOND_PILOT"),
    ])
    assert row["Commander"] == "CPT-01"
    assert row["Second Pilot"] == "FO-01"


def test_an_empty_seat_is_uncovered(monkeypatch):
    dataset, row = _coverage(monkeypatch, [
        _roster_row("CPT-01", "CPT", "COMMANDER"),
    ])
    assert row["Commander"] == "CPT-01"
    assert row["Second Pilot"] == "UNCOVERED"
    assert any("uncovered cockpit seat" in note for note in dataset.notes)


def test_cockpit_crew_with_no_seat_recorded_are_named_not_dropped(monkeypatch):
    """Silently dropping a crew member from a coverage report is worse
    than the bug this replaced. They are named in a note rather than
    placed in a column, because claiming a seat the data does not
    record would be a different kind of wrong.

    Zero such rows exist in production (checked 2026-08-28) and nothing
    since migration 016 can create one, so this is purely defensive."""
    dataset, row = _coverage(monkeypatch, [
        _roster_row("CPT-01", "CPT", "COMMANDER"),
        _roster_row("FO-09", "FO", None),
    ])

    assert "FO-09" not in row["Commander"]
    assert "FO-09" not in row["Second Pilot"]
    assert any(reports.SEAT_NOT_RECORDED in note and "FO-09" in note
               for note in dataset.notes)
    # Aboard, so counted; and the seat they did not fill is still
    # uncovered rather than quietly treated as covered by them.
    assert row["POB"] == 2
    assert row["Second Pilot"] == "UNCOVERED"


def test_seatless_crew_do_not_silently_cover_a_seat(monkeypatch):
    """The failure mode worth naming: an FO with no recorded seat must
    not make the Second Pilot seat look filled."""
    dataset, row = _coverage(monkeypatch, [
        _roster_row("FO-09", "FO", None),
    ])
    assert row["Commander"] == "UNCOVERED"
    assert row["Second Pilot"] == "UNCOVERED"
    assert any("uncovered cockpit seat" in note for note in dataset.notes)


def test_headers_name_seats_rather_than_grades():
    """The header rename is half the fix. Columns headed CPT/FO invite
    exactly the grouping that was wrong, and a reader cannot tell a seat
    report from a grade report by looking at it."""
    assert "Commander" in reports.ROSTER_COVERAGE_HEADERS
    assert "Second Pilot" in reports.ROSTER_COVERAGE_HEADERS
    assert "CPT" not in reports.ROSTER_COVERAGE_HEADERS
    assert "FO" not in reports.ROSTER_COVERAGE_HEADERS


# ------------------------------------------------------------------
# roster_generator_service._seed_duty_counts()
# ------------------------------------------------------------------

_DUTY_COLUMNS = ["crew_id", "duty_id", "duty_date", "report_time",
                 "debrief_time", "role_assigned", "operating_position"]


def _duty(crew_id, duty_id, day, position, role="CPT"):
    return {"crew_id": crew_id, "duty_id": duty_id, "duty_date": dt.date(2026, 8, day),
            "report_time": dt.datetime(2026, 8, day, 4, 0),
            "debrief_time": dt.datetime(2026, 8, day, 12, 0),
            "role_assigned": role, "operating_position": position}


def _seed(monkeypatch, rows, crew_ids, position):
    monkeypatch.setattr(
        assignment_service, "search_roster",
        lambda **k: pd.DataFrame(rows, columns=_DUTY_COLUMNS))
    return rgs._seed_duty_counts(crew_ids, position,
                                 dt.date(2026, 8, 1), dt.date(2026, 8, 31))


def test_fairness_counts_duties_in_the_seat_not_duties_at_the_grade(monkeypatch):
    """A CPT who has flown Second Pilot duties has not thereby been
    given command. The old filter was on role_assigned, and since every
    row for a CPT reads role_assigned='CPT' whichever seat they sat in,
    it was a no-op — Second Pilot duties counted toward the Commander
    total, and command was then offered to whoever looked least used
    for reasons that had nothing to do with command."""
    rows = [
        _duty("CPT-01", "D1", 3, "COMMANDER"),
        _duty("CPT-01", "D2", 4, "SECOND_PILOT"),
        _duty("CPT-01", "D3", 5, "SECOND_PILOT"),
        _duty("CPT-02", "D4", 3, "COMMANDER"),
        _duty("CPT-02", "D5", 4, "COMMANDER"),
    ]
    commander = _seed(monkeypatch, rows, ["CPT-01", "CPT-02"], "COMMANDER")
    assert commander == {"CPT-01": 1, "CPT-02": 2}, commander

    second_pilot = _seed(monkeypatch, rows, ["CPT-01", "CPT-02"], "SECOND_PILOT")
    assert second_pilot == {"CPT-01": 2, "CPT-02": 0}, second_pilot


def test_seat_counts_still_dedupe_sectors_into_duties(monkeypatch):
    """The oldest trap in this codebase — one duty of three sectors is
    one duty, not three. Regrouping by seat must not quietly reintroduce
    raw row counting (migrations/003's own warning)."""
    rows = [
        _duty("CPT-01", "D1", 3, "COMMANDER"),
        _duty("CPT-01", "D1", 3, "COMMANDER"),
        _duty("CPT-01", "D1", 3, "COMMANDER"),
    ]
    assert _seed(monkeypatch, rows, ["CPT-01"], "COMMANDER") == {"CPT-01": 1}


def test_seatless_duties_count_toward_neither_seat(monkeypatch):
    """A duty with no operating_position (LM/ENGR, or a pre-016 cockpit
    row) belongs to no seat, so it counts toward no seat's fairness —
    which is what "duties flown in this seat" means."""
    rows = [_duty("CPT-01", "D1", 3, None)]
    assert _seed(monkeypatch, rows, ["CPT-01"], "COMMANDER") == {"CPT-01": 0}
    assert _seed(monkeypatch, rows, ["CPT-01"], "SECOND_PILOT") == {"CPT-01": 0}
