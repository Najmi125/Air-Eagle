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


# ------------------------------------------------------------------
# compute_duty_window() — the review table's Report/Debrief columns
# ------------------------------------------------------------------
#
# Pure: takes a DataFrame, returns a tuple. No database, so these run
# everywhere — which matters, because the bug they cover reached
# production and the review table's own tests are all DB-gated.

import pandas as pd  # noqa: E402

from services.rotation_template_service import compute_duty_window  # noqa: E402


def _instance_legs(rows):
    return pd.DataFrame([
        {"leg_order": i, "flight_no": f"EPE {700 + i}",
         "origin": o, "destination": d,
         "dep_time_planned": dep, "arr_time_planned": arr, "domestic": dom}
        for i, (o, d, dep, arr, dom) in enumerate(rows, start=1)
    ])


def test_domestic_duty_window_matches_the_reported_production_rotation():
    """The operator's real domestic rotation (2026-08-19). The review
    table displayed 19:00->23:45, which is first-departure and
    last-arrival; the actual duty is 18:15->00:00 under D7.1.2's 45/15
    buffers. Asserted with the reported figures so a regression is
    recognisable as the same defect."""
    legs = _instance_legs([
        ("KHI", "LHE", dt.datetime(2026, 9, 1, 19, 0), dt.datetime(2026, 9, 1, 20, 45), True),
        ("LHE", "KHI", dt.datetime(2026, 9, 1, 22, 0), dt.datetime(2026, 9, 1, 23, 45), True),
    ])

    report, debrief = compute_duty_window(legs)

    assert report == dt.datetime(2026, 9, 1, 18, 15), "45 min before first departure"
    assert debrief == dt.datetime(2026, 9, 2, 0, 0), "15 min after last arrival"


def test_international_duty_window_matches_the_reported_production_rotation():
    """The operator's real international rotation: displayed
    01:45->11:00, actual duty 00:45->11:30 under the 60/30 buffers."""
    legs = _instance_legs([
        ("KHI", "DWC", dt.datetime(2026, 9, 1, 1, 45), dt.datetime(2026, 9, 1, 5, 0), False),
        ("DWC", "KHI", dt.datetime(2026, 9, 1, 7, 0), dt.datetime(2026, 9, 1, 11, 0), False),
    ])

    report, debrief = compute_duty_window(legs)

    assert report == dt.datetime(2026, 9, 1, 0, 45), "60 min before first departure"
    assert debrief == dt.datetime(2026, 9, 1, 11, 30), "30 min after last arrival"


def test_a_single_international_leg_makes_the_whole_duty_international():
    """build_duty() takes one duty-level flag while legs carry one each.
    assignment_service resolves that with all(...) at five sites: any
    international sector applies the longer 60/30 buffers to the WHOLE
    duty. Getting this backwards would understate the window, which is
    the same direction of error this function exists to correct — so it
    is pinned rather than left to the aggregation being read correctly."""
    legs = _instance_legs([
        ("KHI", "LHE", dt.datetime(2026, 9, 1, 19, 0), dt.datetime(2026, 9, 1, 20, 45), True),
        ("LHE", "DWC", dt.datetime(2026, 9, 1, 22, 0), dt.datetime(2026, 9, 1, 23, 45), False),
    ])

    report, debrief = compute_duty_window(legs)

    assert report == dt.datetime(2026, 9, 1, 18, 0), "60 min, not 45 — one leg is international"
    assert debrief == dt.datetime(2026, 9, 2, 0, 15), "30 min, not 15"


def test_empty_legs_return_none_rather_than_raising():
    assert compute_duty_window(pd.DataFrame()) is None


def test_unbuildable_legs_return_none_rather_than_raising():
    """A display value must never be able to take the review table down.
    build_duty() raises on out-of-order legs, and a draft is exactly
    where odd data can appear — the caller shows a placeholder."""
    legs = _instance_legs([
        ("KHI", "LHE", dt.datetime(2026, 9, 1, 22, 0), dt.datetime(2026, 9, 1, 23, 45), True),
        ("LHE", "KHI", dt.datetime(2026, 9, 1, 19, 0), dt.datetime(2026, 9, 1, 20, 45), True),
    ])

    assert compute_duty_window(legs) is None
