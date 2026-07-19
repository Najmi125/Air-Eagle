"""
tests/test_duty_summary.py

Pure logic — no database needed. These are exactly the regression
tests specified in the Project Instructions (Section 9): the case
that was actually wrong in production ("FDP totals inflated on Crew
Profile page" — a 2-sector duty counted its FDP twice).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from core.duty_summary import (
    group_roster_rows_into_duties,
    calculate_crew_duty_summary,
    calculate_max_rolling_fdp,
    available_duty_keys,
)


def _two_sector_duty_df():
    """
    One duty, two sectors, same crew/duty_id/report/debrief — the
    exact shape the roster table produces for a multi-sector duty.
    Both rows show fdp_hours=9 (duty-level value repeated per sector
    row, which is how the schema actually stores it).
    """
    return pd.DataFrame([
        {
            "crew_id": "CPT-01", "duty_id": "D-100", "duty_date": "2026-07-20",
            "report_time": "2026-07-20 05:00", "debrief_time": "2026-07-20 14:00",
            "fdp_hours": 9.0, "status": "PLANNED",
        },
        {
            "crew_id": "CPT-01", "duty_id": "D-100", "duty_date": "2026-07-20",
            "report_time": "2026-07-20 05:00", "debrief_time": "2026-07-20 14:00",
            "fdp_hours": 9.0, "status": "PLANNED",
        },
    ])


def test_two_sector_duty_is_not_double_counted():
    """
    The exact case from Section 9: this must NOT produce
    total_fdp=18, total_duties=2. It must produce total_fdp=9,
    total_duties=1, sector_rows=2.
    """
    df = _two_sector_duty_df()
    summary = calculate_crew_duty_summary(df)

    assert summary["sector_rows"] == 2
    assert summary["unique_duties"] == 1
    assert summary["total_fdp_hours"] == 9.0


def test_group_roster_rows_collapses_to_one_row_per_duty():
    df = _two_sector_duty_df()
    duty_df = group_roster_rows_into_duties(df)
    assert len(duty_df) == 1


def test_two_separate_duties_are_both_counted():
    """Sanity check the other direction: genuinely different duties
    must NOT get collapsed into one."""
    df = pd.DataFrame([
        {
            "crew_id": "CPT-01", "duty_id": "D-100", "duty_date": "2026-07-20",
            "report_time": "2026-07-20 05:00", "debrief_time": "2026-07-20 14:00",
            "fdp_hours": 9.0, "status": "PLANNED",
        },
        {
            "crew_id": "CPT-01", "duty_id": "D-101", "duty_date": "2026-07-21",
            "report_time": "2026-07-21 06:00", "debrief_time": "2026-07-21 12:00",
            "fdp_hours": 6.0, "status": "PLANNED",
        },
    ])
    summary = calculate_crew_duty_summary(df)
    assert summary["unique_duties"] == 2
    assert summary["total_fdp_hours"] == 15.0


def test_disrupted_duties_counted_at_duty_level_not_sector_level():
    """A 2-sector disrupted duty must count as ONE disrupted duty,
    not two, for the same reason as the FDP dedup case."""
    df = _two_sector_duty_df()
    df["status"] = "DISRUPTED"
    summary = calculate_crew_duty_summary(df)
    assert summary["disrupted_duties"] == 1


def test_empty_dataframe_does_not_crash():
    df = pd.DataFrame(columns=["crew_id", "duty_id", "fdp_hours"])
    summary = calculate_crew_duty_summary(df)
    assert summary["sector_rows"] == 0
    assert summary["unique_duties"] == 0
    assert summary["total_fdp_hours"] == 0.0


def test_none_dataframe_does_not_crash():
    summary = calculate_crew_duty_summary(None)
    assert summary["sector_rows"] == 0
    assert summary["unique_duties"] == 0


def test_missing_duty_identity_columns_raises_clear_error():
    """If a page passes a DataFrame with none of the duty identity
    columns, this must fail loudly, not silently return wrong
    numbers — matching the platform's own 'never fill missing policy
    with silent defaults' principle."""
    df = pd.DataFrame([{"some_other_col": 1}])
    try:
        group_roster_rows_into_duties(df)
        assert False, "expected ValueError for missing duty identity columns"
    except ValueError:
        pass


def test_available_duty_keys_uses_only_present_columns():
    df = pd.DataFrame(columns=["crew_id", "duty_id"])
    keys = available_duty_keys(df)
    assert keys == ["crew_id", "duty_id"]


def test_max_rolling_fdp_finds_the_heaviest_window():
    """Three duties on consecutive days, 8h each. A 2-day rolling
    window should find the max pairwise sum = 16h, not the single-day
    value or the full-range sum."""
    duty_df = pd.DataFrame([
        {"duty_date": "2026-07-20", "fdp_hours": 8.0},
        {"duty_date": "2026-07-21", "fdp_hours": 8.0},
        {"duty_date": "2026-07-22", "fdp_hours": 8.0},
    ])
    max_fdp = calculate_max_rolling_fdp(duty_df, window_days=2)
    assert max_fdp == 16.0


def test_max_rolling_fdp_empty_input():
    assert calculate_max_rolling_fdp(pd.DataFrame()) == 0.0
    assert calculate_max_rolling_fdp(None) == 0.0
