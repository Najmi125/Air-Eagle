"""
tests/test_flight_status_rule.py

_apply_operated_rule() is pure — it takes the stored row and the pending
field updates and returns the fields to write — so these run without
Postgres. Deliberate: this rule decides what the permanent record says
about whether a flight flew, and a guard that only runs where a database
happens to be available is a guard that mostly doesn't run.

The rule closes a gap where flights.status could ONLY ever become
CANCELLED: cancel_flight() was its sole writer, so a flight that flew
stayed PLANNED forever and DISRUPTED was unreachable.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from services.flight_service import _apply_operated_rule

_DEP = dt.datetime(2026, 8, 20, 19, 0)
_ARR = dt.datetime(2026, 8, 20, 20, 45)


def _row(status="PLANNED", dep_actual=None, arr_actual=None):
    return pd.Series({
        "flight_id": 1, "status": status,
        "dep_time_actual": dep_actual, "arr_time_actual": arr_actual,
    })


# ---------------- the automatic transition ----------------

def test_both_actuals_in_one_call_marks_operated():
    fields = _apply_operated_rule(_row(), {"dep_time_actual": _DEP, "arr_time_actual": _ARR})
    assert fields["status"] == "OPERATED"


def test_arrival_completing_a_previously_recorded_departure_marks_operated():
    """The case a page-level rule would get wrong. Departure is commonly
    recorded on one shift and arrival on the next, so this call carries
    only ONE column — the rule has to merge against the stored row."""
    stored = _row(dep_actual=_DEP)

    fields = _apply_operated_rule(stored, {"arr_time_actual": _ARR})

    assert fields["status"] == "OPERATED"


def test_one_actual_alone_leaves_the_status_untouched():
    """One actual means in progress, not complete."""
    assert "status" not in _apply_operated_rule(_row(), {"dep_time_actual": _DEP})
    assert "status" not in _apply_operated_rule(_row(), {"arr_time_actual": _ARR})


def test_rule_is_idempotent_on_an_already_operated_flight():
    """Re-saving an unrelated field must not churn the status."""
    stored = _row(status="OPERATED", dep_actual=_DEP, arr_actual=_ARR)

    assert "status" not in _apply_operated_rule(stored, {"remarks": "typo fix"})


# ---------------- CANCELLED is terminal ----------------

def test_actuals_never_revive_a_cancelled_flight():
    """Cancellation is a deliberate act, written by cancel_flight()
    outside this path. A late actual-time entry must not undo it."""
    stored = _row(status="CANCELLED")

    fields = _apply_operated_rule(stored, {"dep_time_actual": _DEP, "arr_time_actual": _ARR})

    assert "status" not in fields
    assert fields["dep_time_actual"] == _DEP   # the times are still recorded


def test_explicit_status_cannot_move_a_cancelled_flight():
    with pytest.raises(ValueError, match="terminal"):
        _apply_operated_rule(_row(status="CANCELLED"), {"status": "OPERATED"})


# ---------------- DISRUPTED outranks the automatic label ----------------

def test_disrupted_survives_actuals_being_recorded():
    """A controller's manual judgement outranks the automatic label.
    "It flew" is recoverable from the actual times themselves; "it was
    disrupted" is recoverable from nothing else."""
    stored = _row(status="DISRUPTED")

    fields = _apply_operated_rule(stored, {"dep_time_actual": _DEP, "arr_time_actual": _ARR})

    assert "status" not in fields


def test_explicit_disrupted_wins_over_the_automatic_rule():
    fields = _apply_operated_rule(
        _row(dep_actual=_DEP, arr_actual=_ARR), {"status": "DISRUPTED"})

    assert fields["status"] == "DISRUPTED"


# ---------------- the invariant is not optional ----------------

def test_explicit_planned_on_a_flown_flight_is_refused():
    """Without this the rule would be a DEFAULT rather than an
    invariant: any caller could assert PLANNED over two recorded
    actuals and have it stick, and "a flight with both actuals is
    operated" would stop being true."""
    stored = _row(status="OPERATED", dep_actual=_DEP, arr_actual=_ARR)

    with pytest.raises(ValueError, match="has flown"):
        _apply_operated_rule(stored, {"status": "PLANNED"})


def test_explicit_planned_is_allowed_when_the_flight_has_not_flown():
    """Clearing a DISRUPTED label on a flight with no actuals lands on
    PLANNED, and must not be refused."""
    fields = _apply_operated_rule(_row(status="DISRUPTED"), {"status": "PLANNED"})

    assert fields["status"] == "PLANNED"


def test_nan_actuals_are_treated_as_absent():
    """get_flight() returns a pandas Series, so a NULL column arrives as
    NaN rather than None — a truthiness check would read it as present."""
    stored = _row(dep_actual=float("nan"), arr_actual=float("nan"))

    assert "status" not in _apply_operated_rule(stored, {"remarks": "x"})
