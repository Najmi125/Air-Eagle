"""
services/display_labels.py

How crew and flights are NAMED on screen. Display only — every one of
these takes an identifier and returns text, and nothing here is ever
written back or parsed.

Exists because the identifiers the system runs on are not the ones the
operator thinks in (2026-08-20). `crew_id` is a foreign key across
roster and audit_log and `flight_id` is the flight's identity; both must
stay exactly as they are. But a controller thinks "AE92" and "EPE 786",
so a screen showing only CPT-01 and #4242 makes them translate in their
head on every read.

One module rather than a format_func per call site, because there are
nine of those across four pages and the fallbacks are the whole
difficulty: one crew member has no operator_staff_id, and every ad-hoc
flight has a null flight_no. Nine copies means nine chances for one of
them to render "None (CPT-01)" in front of the operator.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import pandas as pd


def _clean(value: Any) -> Optional[str]:
    """None, NaN and whitespace-only all mean "not set"."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def crew_label(row: Any, with_name: bool = True) -> str:
    """`AE92 (CPT-01) — Ali Raza`, operator staff ID leading.

    Falls back to `CPT-01 — Ali Raza` when operator_staff_id is not set
    — one real crew member has none, so this is a live case, not a
    defensive branch. Never renders "None (CPT-01)".

    crew_id is always present in the output, in brackets: it is what
    appears in audit_log and what a support conversation will be about,
    so it stays visible rather than being replaced by the staff ID.
    """
    crew_id = _clean(row["crew_id"]) or "?"
    staff_id = _clean(row["operator_staff_id"]) if "operator_staff_id" in row else None

    identity = f"{staff_id} ({crew_id})" if staff_id else crew_id

    if with_name:
        name = _clean(row["name"]) if "name" in row else None
        if name:
            return f"{identity} — {name}"
    return identity


# ------------------------------------------------------------------
# Timestamps
# ------------------------------------------------------------------
# Operator convention (2026-08-31): a time reads `2003z`, not
# `20:03:35`. Everything this system schedules against is UTC, and the
# trailing z says so on every value rather than in a column header the
# reader has to remember.
#
# SECONDS ARE DROPPED DELIBERATELY, everywhere. FDP, rest and the
# D7.1.2 buffers are all defined in minutes, nothing here operates at
# second granularity, and rendering `:35` implies a precision the data
# does not have.
#
# THE CSV EXPORT DOES NOT USE THIS — see reporting.dataset_to_csv().
# That divergence is deliberate, not an oversight anyone should tidy up.

UTC_SUFFIX = "z"


def utc_stamp(value: Any, with_date: bool = True) -> str:
    """`25 Aug 2003z`, or `2003z` when the date is already established
    by context (a single-day board, a column of one date).

    Returns "" for a missing value rather than "None" or "NaT" — these
    land straight in a table cell, and an empty cell reads as "not
    recorded" while "NaT" reads as a bug.

    with_date defaults to True because nearly every list in this app
    spans weeks: rosters, flight logs and audit trails are all
    multi-day, and a bare `2003z` in a list of 103 flights is ambiguous
    in exactly the way flight_label()'s own date exists to prevent.
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass

    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()

    if isinstance(value, dt.datetime):
        stamp = f"{value:%H%M}{UTC_SUFFIX}"
        return f"{value:%d %b} {stamp}" if with_date else stamp
    if isinstance(value, dt.date):
        # A date carries no time; returning "0000z" would invent one.
        return f"{value:%d %b}"
    if isinstance(value, dt.time):
        return f"{value:%H%M}{UTC_SUFFIX}"

    return str(value)


def format_timestamps(df: pd.DataFrame, with_date: bool = True) -> pd.DataFrame:
    """A COPY of df with every datetime-like column rendered through
    utc_stamp(), for handing to st.dataframe().

    Columns are detected by dtype rather than by name, so a query that
    starts returning a new timestamp column is formatted without anyone
    remembering to add it to a list — the failure mode being a single
    raw `2026-08-25 20:03:35` sitting in an otherwise converted table.

    Object columns are checked too: a frame built from Python dicts (as
    several pages do) holds datetimes with dtype=object, and those are
    exactly the tables where a raw timestamp was showing.

    PLAIN DATES ARE LEFT ALONE, and that exclusion is the important
    part. `25 Aug 2003z` drops the year, which is right for a schedule
    read inside a month-long window and WRONG for a crew qualification:
    rendering a medical expiring 2026-07-01 as "01 Jul" removes the one
    digit that says whether the pilot may fly. A DATE column carries no
    time to render in the operator's convention anyway, so there is
    nothing to gain against that risk. Only values with a clock —
    datetime / Timestamp — are converted.

    Returns a copy. The caller's frame is what the rest of the page
    computes from, and formatting is the last thing that happens before
    display, never a mutation of the data.
    """
    if df is None or df.empty:
        return df

    def _is_timestamp(value):
        # datetime BEFORE date: datetime subclasses date, so the naive
        # order would convert expiry columns too. dt.time is included —
        # a bare clock time (template leg departures) has no year to
        # lose, so the objection above does not apply to it.
        return isinstance(value, (dt.datetime, pd.Timestamp, dt.time))

    out = df.copy()
    for column in out.columns:
        series = out[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            out[column] = series.map(lambda v: utc_stamp(v, with_date=with_date))
        elif series.dtype == object:
            values = series.dropna()
            if len(values) and all(_is_timestamp(v) for v in values):
                out[column] = series.map(lambda v: utc_stamp(v, with_date=with_date))
    return out


def flight_label(row: Any, include_route: bool = False) -> str:
    """`EPE 786 · 20 Aug 0145z`, flight number leading.

    The date is part of the label because flight numbers REPEAT daily —
    "EPE 786" alone is ambiguous the moment a selector spans more than
    one day, which every one of them does.

    The TIME joined it on 2026-08-31, in the operator's own `0145z`
    form. Two reasons: it is the convention everywhere else on screen
    now, and the actuals selector on Flt Schedule carries 103 options
    where the departure time is often what a controller is actually
    scanning for. Production has no two flights sharing a number, date
    and route (checked 2026-08-31), so this adds discrimination rather
    than repairing a collision — but a rotation flying the same number
    twice in one day is an ordinary thing and would have collided.

    Falls back to `#4242 · 20 Aug` when flight_no is null. That is not
    an edge case: ad-hoc and charter flights legitimately have no flight
    number (flight_service treats flight_no as optional for exactly that
    reason), so the identifier stands in and is marked with # so it
    reads as an id rather than as a number that might be a callsign.

    include_route appends `KHI→LHE`, for selectors where the route is
    what distinguishes two otherwise identical-looking options.
    """
    flight_no = _clean(row["flight_no"]) if "flight_no" in row else None
    flight_id = _clean(row["flight_id"]) if "flight_id" in row else None

    identity = flight_no or (f"#{flight_id}" if flight_id else "#?")

    parts = [identity]

    dep = row["dep_time_planned"] if "dep_time_planned" in row else None
    if dep is not None and not (isinstance(dep, float) and pd.isna(dep)):
        if isinstance(dep, (dt.datetime, dt.date, pd.Timestamp)):
            parts.append(utc_stamp(dep))
        else:
            parts.append(str(dep))

    if include_route:
        origin = _clean(row["origin"]) if "origin" in row else None
        destination = _clean(row["destination"]) if "destination" in row else None
        if origin and destination:
            parts.append(f"{origin}→{destination}")

    return " · ".join(parts)


def crew_labels(crew_df: pd.DataFrame, with_name: bool = True) -> dict:
    """{crew_id: label} for a whole DataFrame — what a selectbox's
    format_func wants, built once instead of per option."""
    return {row["crew_id"]: crew_label(row, with_name=with_name)
            for _, row in crew_df.iterrows()}


def flight_labels(flights_df: pd.DataFrame, include_route: bool = True) -> dict:
    """{flight_id: label} for a whole DataFrame."""
    return {row["flight_id"]: flight_label(row, include_route=include_route)
            for _, row in flights_df.iterrows()}
