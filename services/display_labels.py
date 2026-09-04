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


# ------------------------------------------------------------------
# What a controller actually calls each pilot
# ------------------------------------------------------------------
# THE LOOKUP WINS. Everything below it — title stripping, the
# initial-plus-surname rule — is the FALLBACK for a crew member who is
# not listed here, not the primary path (operator decision,
# 2026-09-05).
#
# Why a table and not a cleverer rule: there is no rule. The name a
# controller says aloud is not derivable from the name in the database.
# CPT-03 is stored as a MAHMOOD and is called "Fahim"; the mechanical
# rule renders him `CPT S Mahmood`, which is correct, unambiguous, and
# not what anybody on the frequency would recognise. No amount of
# parsing gets from the stored string to "Fahim", because the
# information is not in the string.
#
# So the rule stays — as the fallback that keeps an unlisted crew
# member readable rather than blank — and the table carries the
# knowledge the data does not.
#
# TO ADD SOMEONE: one line, keyed by crew_id. The value is the PERSON
# part only; the grade is prefixed from the crew record, so that a
# promotion changes the label without anyone editing this file.
#
# crew_id is the key rather than the name, deliberately: it is the
# foreign key across roster and audit_log, it does not change, and it
# does not collide — six of Air Eagle's ten pilots are stored as some
# form of "MUHAMMAD", so a name-keyed table would map six people onto
# one entry.
#
# AN UNLISTED CREW MEMBER IS NOT A BUG and must never render as blank
# or as "None": they fall through to the rule below and read exactly as
# they did before this table existed. That is what makes the table safe
# to fill in gradually, one name at a time, instead of needing to be
# complete before it is correct.
#
# WHETHER THIS BELONGS IN A COLUMN INSTEAD is a live question, recorded
# in HANDOVER rather than settled here. Short version: a
# `crew.display_name` column would let OCC fix a name through Crew Data
# without a deploy, which is the real advantage. Against it today --
# ten pilots, a name that changes about never, and a wrong entry that
# is cosmetic rather than operational — a migration plus a form field
# plus a writer is a lot of machinery for a dict with a handful of
# lines. The column becomes the better home the moment OCC wants to
# edit these themselves.
# EVERY crew_id BELOW WAS VERIFIED AGAINST THE crew TABLE before it was
# committed (read-only SELECT, 2026-09-06). The mapping came from an
# operator list, not from the database, and a mis-keyed entry would
# label the WRONG PILOT on the roster board — silently, because a
# plausible name in the wrong seat looks exactly like a correct one.
# The stored name is quoted beside each so the check is repeatable
# without a database.
#
# What the table is FOR shows up in this list: the mechanical rule
# picks the surname, and the operator picks the given name people are
# actually known by. Those disagree for six of the ten.
CREW_DISPLAY_NAMES: dict[str, str] = {
    # crew_id   preferred      stored name              rule would give
    "CPT-01": "Waqar",     # MUHAMMAD WAQAR             CPT M Waqar
    "CPT-03": "Fahim",     # SYED FAHIM MAHMOOD         CPT S Mahmood
    "CPT-04": "Tahir",     # TAHIR MAHMOOD RAJA         CPT T Raja
    "CPT-05": "Adnan",     # ADNAN SARWAR KHAN          CPT A Khan
    "CPT-06": "Asad",      # CAPT MUHAMMAD ASAD ALI     CPT M Ali
    "FO-01": "Ibtisam",    # IBTISAM MUZZAFAR           FO I Muzzafar
    "FO-02": "Wasim",      # MUHAMMAD WASIM             FO M Wasim
    "FO-03": "Shahbaz",    # MUHAMMAD SHAHBAZ           FO M Shahbaz
    "FO-04": "Suleman",    # MUHAMMAD SULEMAN AZIZ      FO M Aziz

    # CPT-02 (MUHAMMAD SALEEM) IS DELIBERATELY ABSENT. The operator
    # supplied nine names; "Saleem" was inferred from the stored name
    # rather than given, and this table exists precisely because the
    # preferred name is NOT derivable from the stored one — so
    # inferring one entry would contradict the reason for the other
    # nine. The database confirms CPT-02 is MUHAMMAD SALEEM; it cannot
    # confirm what a controller calls him.
    #
    # Until the operator says, he falls through to the rule and reads
    # `CPT M Saleem`, which is correct and unambiguous. That is the
    # fallback doing its job, not a gap.
}


# Titles that appear INSIDE the stored name field. Air Eagle's crew
# records were imported from a spreadsheet where at least one name
# reads "CAPT MUHAMMAD ASAD ALI" (checked in production 2026-09-03), so
# taking the first word as a given name yields "C Ali" — a pilot
# initialled from a rank. Names are also stored uppercase and two carry
# trailing whitespace.
NAME_TITLES = frozenset({
    "CAPT", "CAPTAIN", "CPT", "FO", "F/O", "FIRST", "OFFICER",
    "MR", "MRS", "MS", "DR",
})


def crew_seat_name(row: Any) -> str:
    """`CPT M Waqar` — grade, given-name initial, surname.

    For the Roster table, where two crew share a row and the full
    `AE-95 (CPT-01) — MUHAMMAD WAQAR` of crew_label() does not fit. A
    controller reads the seat and wants to know who is in it.

    CREW_DISPLAY_NAMES IS CONSULTED FIRST (2026-09-05). A crew member
    listed there renders as the operator names them — `CPT Fahim` --
    and the mechanical rule below never runs for them.

    THE RULE WAS CHOSEN AGAINST THE REAL NAMES, not in the abstract
    (2026-09-03), and remains the FALLBACK for anyone unlisted.
    First-name-only renders six of Air Eagle's ten pilots as
    "Muhammad", which identifies nobody; initial-plus-surname separates
    all ten. It also meant CPT-03 read `CPT S Mahmood` rather than the
    "Fahim" a controller says aloud — which is the specific gap the
    lookup exists to close, and the reason the trade is no longer
    accepted silently.

    Titles stored inside the name are stripped (see NAME_TITLES), so
    "CAPT MUHAMMAD ASAD ALI" gives `CPT M Ali` and not `CPT C Ali`.
    Storage is uppercase, so the surname is title-cased; a single-word
    name is returned whole rather than initialled into nothing.

    The GRADE is used, not the operating position: this answers "who is
    this person" — the seat is already the column they are sitting in.
    """
    grade = _clean(row["role"]) if "role" in row else None
    name = _clean(row["name"]) if "name" in row else None
    crew_id = _clean(row["crew_id"]) if "crew_id" in row else None

    # The lookup, ahead of everything. Note it is checked BEFORE the
    # missing-name branch: a crew record with a blank name is exactly
    # the case where knowing what people call this person is worth
    # most, and falling through to `CPT CPT-03` because the stored
    # string is empty would waste the one source that still has the
    # answer.
    preferred = CREW_DISPLAY_NAMES.get(crew_id) if crew_id else None
    if preferred:
        return f"{grade} {preferred}" if grade else preferred

    if not name:
        # Never render "None": a crew record with no name still has an
        # id, and an id is more use than a blank cell.
        return " ".join(part for part in (grade, crew_id) if part) or "—"

    parts = [word for word in name.split()
             if word.upper().strip(".") not in NAME_TITLES]
    if not parts:
        parts = name.split()

    if len(parts) == 1:
        who = parts[0].title()
    else:
        who = f"{parts[0][0].upper()} {parts[-1].title()}"

    return f"{grade} {who}" if grade else who


def crew_labels(crew_df: pd.DataFrame, with_name: bool = True) -> dict:
    """{crew_id: label} for a whole DataFrame — what a selectbox's
    format_func wants, built once instead of per option."""
    return {row["crew_id"]: crew_label(row, with_name=with_name)
            for _, row in crew_df.iterrows()}


def flight_labels(flights_df: pd.DataFrame, include_route: bool = True) -> dict:
    """{flight_id: label} for a whole DataFrame."""
    return {row["flight_id"]: flight_label(row, include_route=include_route)
            for _, row in flights_df.iterrows()}
