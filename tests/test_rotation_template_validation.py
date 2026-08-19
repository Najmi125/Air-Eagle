"""
tests/test_rotation_template_validation.py

_validate_legs() is pure — no engine, no connection — so these run
without Postgres, unlike the rest of the rotation-template suite. That
is deliberate: route continuity is the check most likely to be quietly
broken by a later refactor, and a guard that only runs where a database
happens to be available is a guard that mostly doesn't run (see
HANDOVER.md 2026-08-18 for what that cost).

Continuity added 2026-08-19. core/duty_builder.py's build_duty() had
always enforced destination == next origin, but only at EXPANSION time,
so a disconnected template saved cleanly and failed days later, far
from where the mistake was made.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt

import pytest

from services.rotation_template_service import _validate_legs


def _leg(order, flight_no, origin, destination):
    return {
        "leg_order": order, "flight_no": flight_no,
        "origin": origin, "destination": destination,
        "dep_time": dt.time(6, 0), "arr_time": dt.time(8, 0),
        "day_offset": 0, "domestic": True,
    }


def test_continuous_legs_are_accepted():
    _validate_legs([
        _leg(1, "EPE-786", "KHI", "LHE"),
        _leg(2, "EPE-787", "LHE", "KHI"),
    ])


def test_single_leg_is_accepted():
    """Nothing to be discontinuous with — the zip() must not misfire on
    a one-element list."""
    _validate_legs([_leg(1, "EPE-786", "KHI", "LHE")])


def test_disconnected_legs_are_rejected():
    """The real case: leg 1 lands at LHE, leg 2 departs from DWC. This
    used to save cleanly and only fail at expand_and_persist()."""
    with pytest.raises(ValueError) as excinfo:
        _validate_legs([
            _leg(1, "EPE-802", "KHI", "LHE"),
            _leg(2, "EPE-804", "DWC", "KHI"),
        ])

    message = str(excinfo.value)
    assert "LHE" in message and "DWC" in message
    assert "two places at once" in message


def test_discontinuity_is_detected_across_three_legs():
    """The break is between legs 2 and 3, not 1 and 2 — the check has
    to walk every consecutive pair, not just the first."""
    with pytest.raises(ValueError) as excinfo:
        _validate_legs([
            _leg(1, "EPE-802", "KHI", "LHE"),
            _leg(2, "EPE-804", "LHE", "DWC"),
            _leg(3, "EPE-805", "SHJ", "KHI"),
        ])

    assert "DWC" in str(excinfo.value) and "SHJ" in str(excinfo.value)


def test_continuity_is_checked_in_leg_order_not_list_order():
    """legs arrive as a list, but leg_order is the authority. A list
    that happens to be shuffled must still be judged in flight order,
    otherwise a valid rotation could be rejected (or worse, a broken one
    accepted) purely on argument ordering."""
    shuffled = [
        _leg(2, "EPE-787", "LHE", "KHI"),
        _leg(1, "EPE-786", "KHI", "LHE"),
    ]

    _validate_legs(shuffled)


def test_flight_no_still_required():
    """The pre-existing rule must survive the continuity addition."""
    with pytest.raises(ValueError) as excinfo:
        _validate_legs([_leg(1, "", "KHI", "LHE")])

    assert "flight_no is required" in str(excinfo.value)
