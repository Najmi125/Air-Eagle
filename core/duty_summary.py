"""Canonical duty-level roster aggregation utilities.

FTLguard stores roster assignments at sector-row level: one row per crew per
flight sector. Duty metrics such as FDP, duty count, rest gaps, and rolling
utilization must be calculated once per unique crew duty, not once per sector.

Streamlit pages should call these helpers instead of doing page-level
``df['fdp_hours'].sum()`` on sector rows.
"""

from __future__ import annotations

from typing import Iterable

from datetime import timedelta 
import pandas as pd



# Preferred grouping keys for the current roster schema.  The helper will use
# only keys present in the supplied DataFrame so it can work with existing page
# queries while encouraging pages to include all keys.
DEFAULT_DUTY_KEYS: tuple[str, ...] = (
    "crew_id",
    "duty_date",
    "duty_id",
    "report_time",
    "debrief_time",
)


def available_duty_keys(df: pd.DataFrame, keys: Iterable[str] = DEFAULT_DUTY_KEYS) -> list[str]:
    """Return duty identity keys available in ``df``.

    At minimum, callers should provide ``crew_id`` and ``duty_id``.  For safer
    production use, include ``duty_date``, ``report_time``, and ``debrief_time``
    because some demo duty IDs can repeat on different dates.
    """

    return [key for key in keys if key in df.columns]


def group_roster_rows_into_duties(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse sector-level roster rows into one row per crew duty.

    The first row is kept because duty-level fields such as ``fdp_hours``,
    ``report_time``, and ``debrief_time`` are repeated across sectors belonging
    to the same duty.
    """

    if df is None or df.empty:
        return pd.DataFrame(columns=list(df.columns) if df is not None else [])

    keys = available_duty_keys(df)
    if not keys:
        raise ValueError(
            "Cannot group roster rows into duties: none of the duty identity "
            f"columns are present. Expected one or more of {DEFAULT_DUTY_KEYS}."
        )

    # Ensure deterministic output before dropping duplicate sector rows.
    sort_cols = [col for col in ("crew_id", "duty_date", "report_time", "debrief_time", "duty_id") if col in df.columns]
    ordered = df.sort_values(sort_cols) if sort_cols else df.copy()
    return ordered.drop_duplicates(subset=keys, keep="first").reset_index(drop=True)


def calculate_crew_duty_summary(df: pd.DataFrame) -> dict[str, float | int]:
    """Return canonical crew-duty summary metrics for a roster DataFrame."""

    sector_rows = 0 if df is None else len(df)
    duty_df = group_roster_rows_into_duties(df)

    total_fdp = 0.0
    if "fdp_hours" in duty_df.columns and not duty_df.empty:
        total_fdp = float(pd.to_numeric(duty_df["fdp_hours"], errors="coerce").fillna(0).sum())

    disrupted = 0
    if "status" in duty_df.columns and not duty_df.empty:
        disrupted = int((duty_df["status"] == "DISRUPTED").sum())

    return {
        "sector_rows": int(sector_rows),
        "unique_duties": int(len(duty_df)),
        "total_fdp_hours": round(total_fdp, 2),
        "disrupted_duties": disrupted,
    }
def calculate_max_rolling_fdp(duty_df, window_days=7, date_col="duty_date", fdp_col="fdp_hours"):
    """
    Calculate maximum rolling FDP hours over a window.

    Important:
    duty_df must already be duty-level, not sector-row-level.

    Example:
    If window_days=7, this returns the highest FDP total found in any
    rolling 7-day period inside the selected roster data.
    """
    if duty_df is None or duty_df.empty:
        return 0.0

    df = duty_df.copy()

    if date_col not in df.columns or fdp_col not in df.columns:
        return 0.0

    df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.date
    df[fdp_col] = pd.to_numeric(df[fdp_col], errors="coerce").fillna(0)

    df = df.dropna(subset=[date_col])

    if df.empty:
        return 0.0

    unique_dates = sorted(df[date_col].unique())
    max_total = 0.0

    for start_date in unique_dates:
        end_date = start_date + timedelta(days=window_days - 1)

        window_total = float(
            df[
                (df[date_col] >= start_date) &
                (df[date_col] <= end_date)
            ][fdp_col].sum()
        )

        max_total = max(max_total, window_total)

    return max_total
