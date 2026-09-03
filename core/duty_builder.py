"""
core/duty_builder.py

Canonical FDP calculator. Given an ordered list of flight legs that
together form one duty for one crew member, computes report_time,
debrief_time, and fdp_hours — the one place in the platform this
calculation happens (Section 8: "This rule lives in one place: the
canonical duty builder. Not in the UI. Not in the FTL monitor. Not
in analytics. Not in the delay page.").

fdp_hours = debrief_time - report_time, never block time. This is
the exact area of the historical "2.2h instead of 13h" bug: a page
recalculated FDP from new arrival/departure times after a delay,
using block time instead of the report-to-debrief duty window.
Because this function takes whatever dep_time/arr_time values it's
given, a delay recompute is just calling this same function again
with updated times — there's no separate "delay path" to
accidentally get wrong.

Deliberately does NOT decide which flights belong to one duty — an
operational/human decision (which flights get selected together when
building a duty), not something inferred from timestamps here.

Deliberately does NOT guess domestic vs international — that
determines which buffer applies (D7.1.2), and guessing wrong would
silently produce a wrong FDP. The caller must say which applies.
Air Eagle's route profile (domestic-only vs domestic+international)
was never confirmed — see HANDOVER.md — so this stays an explicit
parameter, not an assumption, until that's answered.

This file replaces the old repo's duty_builder.py entirely, which
was hardcoded to XYZ Airline's specific recurring flight numbers
(DUTY_TEMPLATES) — a design that assumed a fixed published schedule.
Air Eagle has both ad-hoc and scheduled ops with no fixed template
set, so this version is driven entirely by whatever flights are
actually passed in, not a lookup table.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Sequence

# Report/debrief buffers per ANO-012 D7.1.2
DOMESTIC_PRE_FLIGHT_MINUTES = 45
DOMESTIC_POST_FLIGHT_MINUTES = 15
INTERNATIONAL_PRE_FLIGHT_MINUTES = 60
INTERNATIONAL_POST_FLIGHT_MINUTES = 30


@dataclass
class FlightLeg:
    dep_time: datetime   # naive, UTC by convention — matches schema/core engine
    arr_time: datetime
    origin: str
    destination: str


@dataclass
class DutyResult:
    report_time: datetime
    debrief_time: datetime
    fdp_hours: float
    sector_count: int


def sector_continuity_problems(legs: Sequence["FlightLeg"]) -> List[str]:
    """Every way this sequence of legs fails to be one physically
    continuous duty, as sentences. Empty list means coherent.

    EXTRACTED 2026-09-03, and the extraction is the point. The rule
    already existed and was already correct — inside build_duty(),
    which RAISES. build_duty() runs when a duty is PLANNED, on planned
    times, and nothing ran it again when actuals were recorded, so a
    delay that made a duty physically impossible produced no warning at
    all (flight 53, EPE 786, planned 1900-2045z recorded 2200-2345z
    while its second sector still read 2200-2345z — sector 1 landing at
    the moment sector 2 departs).

    Two callers, one copy of the rule, and they need OPPOSITE
    behaviour, which is why this returns problems rather than raising:

      * PLANNING must refuse. You cannot schedule a duty that is
        impossible, so build_duty() turns any problem into ValueError.
      * RECORDING must not. What happened, happened; a controller
        entering an actual time mid-duty is doing their job, and
        blocking it would leave the record less accurate rather than
        more. The caller raises an alert instead.

    A second copy of these two comparisons living in the recording path
    is exactly how the two would drift into disagreeing about what
    "continuous" means.
    """
    problems = []
    for i in range(len(legs) - 1):
        if legs[i].arr_time >= legs[i + 1].dep_time:
            problems.append(
                f"Leg {i} arrives ({legs[i].arr_time}) at or after leg {i + 1} "
                f"departs ({legs[i + 1].dep_time}) — legs must be in "
                f"chronological, non-overlapping order with time for turnaround"
            )
        if legs[i].destination != legs[i + 1].origin:
            problems.append(
                f"Leg {i} arrives at {legs[i].destination} but leg {i + 1} "
                f"departs from {legs[i + 1].origin} — a crew member can't be "
                f"in two places at once. These flights don't form one "
                f"physically continuous duty."
            )
    return problems


def build_duty(legs: List[FlightLeg], domestic: bool) -> DutyResult:
    """
    For PLANNING a new duty, before it happens: report_time and
    debrief_time are both derived from the legs' departure/arrival
    times plus the D7.1.2 buffer.

    legs must already be in chronological, non-overlapping order —
    that ordering is the caller's decision (which flights constitute
    one duty), not something this function infers.

    Raises ValueError on empty input or out-of-order/overlapping legs
    — fails loudly rather than silently computing a wrong duty window
    from bad input, per the platform's "never fill missing policy
    with silent defaults" principle.

    Do NOT use this function to recompute FDP after a delay on a duty
    that has already started — see recompute_fdp_after_delay() below
    for why that's a different calculation, not just a re-run of this
    one with updated times.
    """
    if not legs:
        raise ValueError("build_duty() requires at least one flight leg")

    for problem in sector_continuity_problems(legs):
        raise ValueError(problem)

    pre = DOMESTIC_PRE_FLIGHT_MINUTES if domestic else INTERNATIONAL_PRE_FLIGHT_MINUTES
    post = DOMESTIC_POST_FLIGHT_MINUTES if domestic else INTERNATIONAL_POST_FLIGHT_MINUTES

    report_time = legs[0].dep_time - timedelta(minutes=pre)
    debrief_time = legs[-1].arr_time + timedelta(minutes=post)
    fdp_hours = round((debrief_time - report_time).total_seconds() / 3600, 2)

    return DutyResult(
        report_time=report_time,
        debrief_time=debrief_time,
        fdp_hours=fdp_hours,
        sector_count=len(legs),
    )


def recompute_fdp_after_delay(report_time: datetime, new_debrief_time: datetime) -> float:
    """
    For an EXISTING duty whose crew has already reported for duty:
    recompute fdp_hours after a delay changes the actual arrival
    (and therefore debrief_time). report_time is NOT recomputed here
    — it's the already-recorded time the crew reported, which does
    not shift just because departure got delayed afterward.

    This is deliberately a separate function from build_duty(), not
    build_duty() called again with updated times. Conflating the two
    is exactly the historical bug (Section 8): a page recalculated
    FDP from the new departure/arrival block time after a 360-minute
    delay and got 2.2h, when the correct answer — report_time 05:00
    (unchanged) to debrief_time 18:00 (updated) — was 13.0h.

    Raises ValueError if new_debrief_time isn't after report_time.
    """
    if new_debrief_time <= report_time:
        raise ValueError("new_debrief_time must be after report_time")
    return round((new_debrief_time - report_time).total_seconds() / 3600, 2)
