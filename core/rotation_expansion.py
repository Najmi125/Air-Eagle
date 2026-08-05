"""
core/rotation_expansion.py

Pure date/time expansion of a recurring rotation template into dated
instances. No DB, no Streamlit — same placement principle as
core/duty_builder.py (pure FDP/rest math) and core/duty_summary.py
(pure aggregation): a function that's fully unit-testable with no
fixtures.

Expansion's only job is turning a template's (day_offset, time-of-day)
legs into real datetimes for a given calendar window. It does not
compute FDP, rest, or any legality — that's core/duty_builder.py's
build_duty() and core/legality/pcaa_ano012_core.py's validator,
unchanged and untouched here. An ExpandedLeg's dep_time_planned/
arr_time_planned feed directly into a FlightLeg the same way a real
flights row already does.

Scope limitation, deliberate: a single leg is assumed not to cross
midnight itself (both of Air Eagle's real rotations hold this — see
HANDOVER.md's 2026-08-04 entry for the hand-verified numbers). A leg
whose arr_time is not after its dep_time on the same day_offset is
rejected rather than silently producing a backward-flowing or
ambiguous datetime; day_offset is the supported way to model a leg
that genuinely departs on a later calendar day than the rotation's
nominal start.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class TemplateLeg:
    """One leg of a rotation_template_legs row — time-of-day only, no
    calendar date. Mirrors the DB columns directly."""
    leg_order: int
    origin: str
    destination: str
    dep_time: dt.time
    arr_time: dt.time
    flight_no: Optional[str] = None
    day_offset: int = 0
    domestic: bool = True


@dataclass(frozen=True)
class ExpandedLeg:
    """One leg with real, computed datetimes — what actually gets
    persisted to rotation_instance_legs, and what feeds
    core/duty_builder.py's FlightLeg unchanged."""
    leg_order: int
    origin: str
    destination: str
    dep_time_planned: dt.datetime
    arr_time_planned: dt.datetime
    flight_no: Optional[str]
    domestic: bool


@dataclass(frozen=True)
class RotationInstanceDraft:
    """One calendar occurrence of a template — what
    rotation_template_service.py persists into rotation_instances/
    rotation_instance_legs."""
    rotation_date: dt.date
    legs: tuple


def expand_template(
    days_of_week: Sequence[int],
    legs: Sequence[TemplateLeg],
    date_from: dt.date,
    date_to: dt.date,
) -> List[RotationInstanceDraft]:
    """
    One RotationInstanceDraft per date in [date_from, date_to] (both
    inclusive) whose ISO weekday (1=Monday..7=Sunday) is in
    days_of_week. Each leg's real datetime = rotation_date +
    leg.day_offset days, combined with the leg's own time-of-day.

    Raises ValueError if date_from > date_to, if days_of_week/legs is
    empty, or if any leg's computed arr_time_planned is not strictly
    after its dep_time_planned (see the module docstring's scope
    limitation) — surfaced as a data problem rather than silently
    producing a backward-flowing leg.
    """
    if date_from > date_to:
        raise ValueError("date_from must not be after date_to")
    if not days_of_week:
        raise ValueError("days_of_week must not be empty")
    if not legs:
        raise ValueError("legs must not be empty")

    weekdays = set(days_of_week)
    ordered_legs = sorted(legs, key=lambda leg: leg.leg_order)

    drafts: List[RotationInstanceDraft] = []
    current = date_from
    while current <= date_to:
        if current.isoweekday() in weekdays:
            expanded_legs = []
            for leg in ordered_legs:
                leg_date = current + dt.timedelta(days=leg.day_offset)
                dep = dt.datetime.combine(leg_date, leg.dep_time)
                arr = dt.datetime.combine(leg_date, leg.arr_time)
                if arr <= dep:
                    raise ValueError(
                        f"Leg {leg.leg_order} ({leg.origin}-{leg.destination}) on "
                        f"{current.isoformat()}: arr_time {leg.arr_time} is not after "
                        f"dep_time {leg.dep_time} within day_offset={leg.day_offset} — "
                        f"a single leg crossing midnight is not supported, use a "
                        f"larger day_offset on a later leg instead"
                    )
                expanded_legs.append(ExpandedLeg(
                    leg_order=leg.leg_order,
                    origin=leg.origin,
                    destination=leg.destination,
                    dep_time_planned=dep,
                    arr_time_planned=arr,
                    flight_no=leg.flight_no,
                    domestic=leg.domestic,
                ))
            drafts.append(RotationInstanceDraft(
                rotation_date=current, legs=tuple(expanded_legs),
            ))
        current += dt.timedelta(days=1)

    return drafts
