"""
tests/test_duty_builder.py

Pure logic - no database needed.
"""
import sys
from pathlib import Path
import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from core.duty_builder import build_duty, recompute_fdp_after_delay, FlightLeg


# ------------------------------------------------------------------
# build_duty() - planning a new duty
# ------------------------------------------------------------------

def test_single_leg_domestic_uses_45_15_buffers():
    leg = FlightLeg(
        dep_time=dt.datetime(2026, 7, 20, 5, 45),
        arr_time=dt.datetime(2026, 7, 20, 7, 45),
        origin="KHI", destination="LHE",
    )
    result = build_duty([leg], domestic=True)
    assert result.report_time == dt.datetime(2026, 7, 20, 5, 0)     # 45 min before dep
    assert result.debrief_time == dt.datetime(2026, 7, 20, 8, 0)    # 15 min after arr
    assert result.fdp_hours == 3.0
    assert result.sector_count == 1


def test_single_leg_international_uses_60_30_buffers():
    leg = FlightLeg(
        dep_time=dt.datetime(2026, 7, 20, 5, 0),
        arr_time=dt.datetime(2026, 7, 20, 9, 0),
        origin="KHI", destination="DXB",
    )
    result = build_duty([leg], domestic=False)
    assert result.report_time == dt.datetime(2026, 7, 20, 4, 0)     # 60 min before dep
    assert result.debrief_time == dt.datetime(2026, 7, 20, 9, 30)   # 30 min after arr
    assert result.fdp_hours == 5.5


def test_multi_leg_uses_first_departure_and_last_arrival():
    leg1 = FlightLeg(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0), "KHI", "LHE")
    leg2 = FlightLeg(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0), "LHE", "ISB")
    result = build_duty([leg1, leg2], domestic=True)
    assert result.report_time == dt.datetime(2026, 7, 20, 4, 15)    # 45 min before FIRST dep
    assert result.debrief_time == dt.datetime(2026, 7, 20, 10, 15)  # 15 min after LAST arr
    assert result.sector_count == 2


def test_fdp_hours_is_never_block_time():
    """The FDP must be report-to-debrief, not sum of flight block
    times. Two 1-hour flights with a 4-hour ground gap between them
    must NOT produce fdp_hours == 2.0 (just the flying time)."""
    leg1 = FlightLeg(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 6, 0), "KHI", "LHE")
    leg2 = FlightLeg(dt.datetime(2026, 7, 20, 10, 0), dt.datetime(2026, 7, 20, 11, 0), "LHE", "KHI")
    result = build_duty([leg1, leg2], domestic=True)
    assert result.fdp_hours != 2.0
    assert result.fdp_hours == 7.0  # 4:15 report to 11:15 debrief


def test_empty_legs_raises():
    with pytest.raises(ValueError):
        build_duty([], domestic=True)


def test_out_of_order_legs_raises():
    leg1 = FlightLeg(dt.datetime(2026, 7, 20, 10, 0), dt.datetime(2026, 7, 20, 12, 0), "KHI", "LHE")
    leg2 = FlightLeg(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0), "LHE", "ISB")
    with pytest.raises(ValueError):
        build_duty([leg1, leg2], domestic=True)  # leg2 departs before leg1 arrives


def test_overlapping_legs_raises():
    leg1 = FlightLeg(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 9, 0), "KHI", "LHE")
    leg2 = FlightLeg(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 11, 0), "LHE", "ISB")
    with pytest.raises(ValueError):
        build_duty([leg1, leg2], domestic=True)  # leg2 departs before leg1 arrives


def test_geographically_disconnected_legs_raises():
    """A crew member can't be in two places at once — leg N's
    destination must equal leg N+1's origin. This was previously
    unchecked; only temporal ordering was validated."""
    leg1 = FlightLeg(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0), "KHI", "LHE")
    leg2 = FlightLeg(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0), "ISB", "KHI")
    with pytest.raises(ValueError):
        build_duty([leg1, leg2], domestic=True)  # leg2 departs ISB, leg1 arrived LHE


def test_geographically_continuous_legs_succeed():
    """Sanity check the positive case explicitly, not just via the
    other tests' incidental correctness."""
    leg1 = FlightLeg(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0), "KHI", "LHE")
    leg2 = FlightLeg(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0), "LHE", "DWC")
    leg3 = FlightLeg(dt.datetime(2026, 7, 20, 11, 0), dt.datetime(2026, 7, 20, 13, 0), "DWC", "KHI")
    result = build_duty([leg1, leg2, leg3], domestic=False)
    assert result.sector_count == 3


# ------------------------------------------------------------------
# recompute_fdp_after_delay() - the exact historical bug scenario
# ------------------------------------------------------------------

def test_delay_recompute_matches_the_documented_historical_example():
    """
    Section 8's exact numbers: report_time fixed at 05:00, a delay
    pushes debrief to 18:00. Correct answer is 13.0h. The WRONG
    (block-time-only) calculation the old repo actually shipped —
    new departure 15:15 to new arrival 17:30 — gives ~2.2h. This test
    is the permanent guard against that exact regression.
    """
    report_time = dt.datetime(2026, 7, 20, 5, 0)
    new_debrief_time = dt.datetime(2026, 7, 20, 18, 0)

    fdp_hours = recompute_fdp_after_delay(report_time, new_debrief_time)
    assert fdp_hours == 13.0

    # Demonstrate the wrong answer explicitly, so the contrast is
    # unmistakable rather than just asserting the right number.
    wrong_block_time_only = (
        dt.datetime(2026, 7, 20, 17, 30) - dt.datetime(2026, 7, 20, 15, 15)
    ).total_seconds() / 3600
    assert wrong_block_time_only == 2.25
    assert fdp_hours != wrong_block_time_only


def test_delay_recompute_report_time_does_not_shift():
    """report_time must be exactly what was passed in — this
    function must never derive or adjust it from a departure time,
    since the crew already reported before the delay was known."""
    report_time = dt.datetime(2026, 7, 20, 5, 0)
    new_debrief_time = dt.datetime(2026, 7, 20, 12, 0)
    fdp_hours = recompute_fdp_after_delay(report_time, new_debrief_time)
    assert fdp_hours == 7.0


def test_delay_recompute_rejects_debrief_before_report():
    with pytest.raises(ValueError):
        recompute_fdp_after_delay(
            dt.datetime(2026, 7, 20, 18, 0),
            dt.datetime(2026, 7, 20, 5, 0),
        )
