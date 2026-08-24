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


def flight_label(row: Any, include_route: bool = False) -> str:
    """`EPE 786 · 20 Aug`, flight number leading.

    The date is part of the label because flight numbers REPEAT daily —
    "EPE 786" alone is ambiguous the moment a selector spans more than
    one day, which every one of them does.

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
        if isinstance(dep, (dt.datetime, dt.date)):
            parts.append(f"{dep:%d %b}")
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
