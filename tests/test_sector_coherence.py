"""Actuals are used for arithmetic — they must also be used for
ordering, and they must be checked.

Reported from live use (2026-09-02). Flight 53 (EPE 786, KHI-LHE)
planned 1900-2045z was recorded 2200-2345z while its second sector,
flight 54, still read 2200-2345z. Sector 1 therefore landed at the
moment sector 2 departed: the duty is physically impossible. Status
went to OPERATED and nothing was reported.

Two separate defects sit underneath that, and the second is worse:

  * THE RULE WAS NEVER ASKED. build_duty() has always checked sector
    continuity, correctly, and raises — but it only ever runs when a
    duty is PLANNED, on planned times. Nothing re-asked it when
    actuals arrived.
  * ORDERING IGNORED THE ACTUALS. debrief_time was computed from
    sectors[-1], the last sector by PLANNED departure, never re-sorted
    once actuals landed. A delay on a NON-FINAL sector therefore left
    the duty ending earlier on paper than the crew actually finished,
    and the recorded FDP understated it. A plausible number that is
    too low is worse than a missing one — nothing about it invites a
    second look.

DB-free. The rule is a pure function and the arithmetic is arithmetic;
neither needs Postgres to be pinned, which matters because every
DB-gated test of this path skips wherever Postgres is absent.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.duty_builder import (
    DOMESTIC_POST_FLIGHT_MINUTES, FlightLeg, build_duty,
    sector_continuity_problems,
)
from core.legality.pcaa_ano012_core import Sector

D = dt.datetime

# The reported flight, exactly.
_S1_PLANNED = Sector(departure_utc=D(2026, 9, 2, 19, 0), arrival_utc=D(2026, 9, 2, 20, 45),
                     origin="KHI", destination="LHE")
_S1_ACTUAL = Sector(departure_utc=D(2026, 9, 2, 22, 0), arrival_utc=D(2026, 9, 2, 23, 45),
                    origin="KHI", destination="LHE")
_S2_PLANNED = Sector(departure_utc=D(2026, 9, 2, 22, 0), arrival_utc=D(2026, 9, 2, 23, 45),
                     origin="LHE", destination="KHI")


def _legs(*sectors):
    return [FlightLeg(dep_time=s.departure_utc, arr_time=s.arrival_utc,
                      origin=s.origin, destination=s.destination) for s in sectors]


# ------------------------------------------------------------------
# One rule, two callers
# ------------------------------------------------------------------

def test_the_reported_duty_is_detected_as_incoherent():
    """Sector 1 lands at 2345z; sector 2 departs at 2200z."""
    problems = sector_continuity_problems(_legs(_S1_ACTUAL, _S2_PLANNED))

    assert len(problems) == 1, problems
    assert "at or after" in problems[0]
    assert "23:45" in problems[0] and "22:00" in problems[0]


def test_the_same_duty_on_planned_times_is_coherent():
    """Not vacuous: as PLANNED this duty is fine, which is precisely
    why planning-time checking never caught it."""
    assert sector_continuity_problems(_legs(_S1_PLANNED, _S2_PLANNED)) == []


def test_planning_still_refuses_what_recording_only_flags():
    """The two callers need OPPOSITE behaviour from one rule. You
    cannot SCHEDULE an impossible duty; you must be able to RECORD one,
    because it already happened."""
    with pytest.raises(ValueError, match="at or after"):
        build_duty(_legs(_S1_ACTUAL, _S2_PLANNED), domestic=True)


def test_a_geographic_break_is_still_caught():
    """The other half of continuity — a crew member cannot be in two
    places at once — must survive the extraction."""
    legs = _legs(_S1_ACTUAL,
                 Sector(departure_utc=D(2026, 9, 3, 2, 0), arrival_utc=D(2026, 9, 3, 4, 0),
                        origin="ISB", destination="KHI"))
    problems = sector_continuity_problems(legs)

    assert len(problems) == 1
    assert "two places at once" in problems[0]


def test_a_coherent_multi_sector_duty_reports_nothing():
    legs = _legs(_S1_PLANNED,
                 Sector(departure_utc=D(2026, 9, 2, 21, 30), arrival_utc=D(2026, 9, 2, 23, 15),
                        origin="LHE", destination="KHI"))
    assert sector_continuity_problems(legs) == []


def test_a_single_sector_duty_has_nothing_to_compare():
    assert sector_continuity_problems(_legs(_S1_ACTUAL)) == []
    assert sector_continuity_problems([]) == []


# ------------------------------------------------------------------
# Ordering: debrief comes from the LATEST arrival, not the last row
# ------------------------------------------------------------------

def _debrief(sectors):
    """What _recompute_one_duty_after_delay() now computes."""
    return max(s.arrival_utc for s in sectors) + dt.timedelta(
        minutes=DOMESTIC_POST_FLIGHT_MINUTES)


def _debrief_old(sectors):
    """What it computed before 2026-09-03 — sectors[-1], i.e. the last
    sector by PLANNED departure, whatever the actuals say."""
    return sectors[-1].arrival_utc + dt.timedelta(minutes=DOMESTIC_POST_FLIGHT_MINUTES)


def test_a_delay_on_a_non_final_sector_no_longer_understates_the_duty():
    """THE dangerous one. Sector 1 is delayed until it lands AFTER the
    untouched final sector; the old arithmetic ended the duty before
    the crew had finished flying."""
    late_first = Sector(departure_utc=D(2026, 9, 2, 23, 0), arrival_utc=D(2026, 9, 3, 1, 0),
                        origin="KHI", destination="LHE")
    sectors = [late_first, _S2_PLANNED]   # order as loaded: by planned departure

    assert _debrief_old(sectors) == D(2026, 9, 3, 0, 0)
    assert _debrief(sectors) == D(2026, 9, 3, 1, 15)
    assert _debrief(sectors) > _debrief_old(sectors), (
        "the correction must move the duty END LATER — understating FDP "
        "is the direction that gets someone hurt"
    )


def test_the_ordering_fix_changes_nothing_when_no_sector_overtakes():
    """Every duty whose sectors are still in order — which is nearly
    all of them — must be unaffected, or this would silently rewrite
    FDP across the whole roster."""
    sectors = [_S1_PLANNED,
               Sector(departure_utc=D(2026, 9, 2, 21, 30), arrival_utc=D(2026, 9, 2, 23, 15),
                      origin="LHE", destination="KHI")]
    assert _debrief(sectors) == _debrief_old(sectors)


def test_the_reported_case_keeps_its_fdp_which_is_why_nothing_looked_wrong():
    """Worth pinning because it explains the report. In the flight-53
    case the two arrivals TIE, so the recomputed FDP was unchanged at
    5.75h — not stale, recomputed to the same number. The duty was
    incoherent while every number on screen stayed plausible."""
    sectors = [_S1_ACTUAL, _S2_PLANNED]
    report_time = D(2026, 9, 2, 18, 15)

    assert _debrief(sectors) == _debrief_old(sectors) == D(2026, 9, 3, 0, 0)
    fdp = round((_debrief(sectors) - report_time).total_seconds() / 3600, 2)
    assert fdp == 5.75


# ------------------------------------------------------------------
# The SERVICE, not a reimplementation of it
# ------------------------------------------------------------------
# The tests above pin the rule and the arithmetic. On their own they
# are theatre: deleting the continuity check from
# _recompute_one_duty_after_delay(), or putting sectors[-1] back, left
# every one of them GREEN, because none of them called it. Same shape
# as the _read_duty_rows seam lesson — a test that reimplements the
# thing it is testing measures its own copy.
#
# These drive the real function over fake leaves, so both mutations
# fail here.

import pandas as pd

import services.assignment_service as assignment_service
import services.crew_service as crew_service
import services.flight_service as flight_service


class _Conn:
    def __init__(self, statements):
        self._statements = statements

    def execute(self, statement, params=None):
        self._statements.append((str(statement), params or {}))
        # rowcount, because _mark_duty_needs_review() reads it (added
        # 2026-09-05, when the flag write was extracted so the delay
        # path and the crew-change path could not drift apart). The
        # delay path ignores the return value, but a fake that omits
        # what a real result carries fails for a reason that has
        # nothing to do with the thing under test.
        return self

    @property
    def rowcount(self):
        return 1

    def fetchall(self):
        return []


class _Txn:
    def __init__(self, statements):
        self._statements = statements

    def __enter__(self):
        return _Conn(self._statements)

    def __exit__(self, *exc):
        return False


class _Engine:
    def __init__(self):
        self.statements = []

    def begin(self):
        return _Txn(self.statements)

    def connect(self):
        return _Txn(self.statements)


def _duty_rows(sectors):
    """What _read_duty_rows() returns — one row per sector, in the
    order the real query gives them: by PLANNED departure, never
    re-sorted once actuals land. That ordering IS the defect, so the
    fixture has to reproduce it rather than help."""
    return pd.DataFrame([
        {"roster_id": i + 1, "duty_id": "DUTY-1",
         "report_time": D(2026, 9, 2, 18, 15), "debrief_time": D(2026, 9, 3, 0, 0),
         "role_assigned": "CPT", "operating_position": "COMMANDER",
         "flight_id": 53 + i,
         "dep_time": s.departure_utc, "arr_time": s.arrival_utc,
         "origin": s.origin, "destination": s.destination,
         "meal_provided": False, "snack_provided": False}
        for i, s in enumerate(sectors)
    ])


@pytest.fixture
def recompute(monkeypatch):
    """Runs the REAL _recompute_one_duty_after_delay() over fakes."""
    engine = _Engine()
    crew = pd.Series({
        "crew_id": "CPT-01", "name": "MUHAMMAD WAQAR", "role": "CPT", "base": "KHI",
        "is_active": True, "home_base": "KHI",
        "date_of_birth": dt.date(1980, 1, 1), "operator_staff_id": "AE-95",
        "license_expiry": dt.date(2099, 1, 1), "medical_expiry": dt.date(2099, 1, 1),
        "sim_expiry": dt.date(2099, 1, 1), "route_check_expiry": dt.date(2099, 1, 1),
        "ir_expiry": dt.date(2099, 1, 1), "sep_expiry": dt.date(2099, 1, 1),
        "crm_expiry": dt.date(2099, 1, 1), "dg_expiry": dt.date(2099, 1, 1),
    })

    def run(sectors):
        monkeypatch.setattr(assignment_service, "get_engine", lambda: engine)
        monkeypatch.setattr(assignment_service, "log_audit", lambda **k: None)
        monkeypatch.setattr(crew_service, "get_crew", lambda cid: crew)
        monkeypatch.setattr(flight_service, "get_flight",
                            lambda fid: pd.Series({"flight_id": fid, "domestic": True}))
        # The SEAM, so the real record-building and the real caching run.
        monkeypatch.setattr(assignment_service, "_read_duty_rows",
                            lambda *a, **k: _duty_rows(sectors))
        return assignment_service._recompute_one_duty_after_delay(
            engine, "CPT-01", "DUTY-1", app_user="occ1"), engine

    return run


def test_the_service_flags_the_reported_duty_for_review(recompute):
    """The whole finding, end to end through the real function: an
    incoherent recorded duty must come back NEEDS_MANUAL_REVIEW and
    must say why."""
    outcome, engine = recompute([_S1_ACTUAL, _S2_PLANNED])
    assert outcome is not None
    validation_result, _downstream, _summary = outcome

    assert validation_result.status.value == "NEEDS_MANUAL_REVIEW", validation_result.status
    continuity = [a for a in validation_result.alerts if a.rule_code == "SECTOR_CONTINUITY"]
    assert len(continuity) == 1, [a.rule_code for a in validation_result.alerts]
    assert "not physically continuous" in continuity[0].message
    assert "at or after" in continuity[0].message

    # And the flag is DURABLE, not just returned — a message that
    # prints once is what this defect looked like from the outside.
    assert any("status = 'NEEDS_REVIEW'" in sql for sql, _ in engine.statements), (
        [sql for sql, _ in engine.statements]
    )


def test_the_service_leaves_a_coherent_duty_alone(recompute):
    """Not vacuous: the same path over sensible times must not flag."""
    later = Sector(departure_utc=D(2026, 9, 2, 21, 30), arrival_utc=D(2026, 9, 2, 23, 15),
                   origin="LHE", destination="KHI")
    outcome, engine = recompute([_S1_PLANNED, later])
    validation_result, _downstream, _summary = outcome

    assert not [a for a in validation_result.alerts if a.rule_code == "SECTOR_CONTINUITY"]
    assert not any("status = 'NEEDS_REVIEW'" in sql for sql, _ in engine.statements)


def test_the_service_writes_debrief_from_the_latest_arrival(recompute):
    """The ordering fix, measured on what the service actually writes
    rather than on a copy of its arithmetic."""
    late_first = Sector(departure_utc=D(2026, 9, 2, 23, 0), arrival_utc=D(2026, 9, 3, 1, 0),
                        origin="KHI", destination="LHE")
    _outcome, engine = recompute([late_first, _S2_PLANNED])

    written = [params for sql, params in engine.statements if "debrief_time" in sql]
    assert written, [sql for sql, _ in engine.statements]
    debrief = written[0]["debrief_time"]

    assert debrief == D(2026, 9, 3, 1, 15), debrief
    assert debrief > D(2026, 9, 3, 0, 0), (
        "sectors[-1] would have ended the duty at 0000z, before the crew "
        "had finished flying — understating the recorded FDP"
    )
