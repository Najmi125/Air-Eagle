"""
tests/test_cross_rotation_legality.py

THE test the preview/accept redesign exists for.

WHAT IT GUARDS. Roster generation used to write PROPOSED roster rows as
it walked the window. That meant cross-rotation legality was enforced by
a SIDE EFFECT nobody had written down: by the time rotation 2 was
validated, rotation 1's rows were already in the roster table, so
_fetch_duty_rows() returned them and the FTL gate checked rest, overlap
and cumulative limits across the whole window without anyone asking it
to.

generate_preview() writes nothing. Remove the writes and that
enforcement disappears in silence — every rotation validates against an
empty history, every rotation passes on its own, and the SET is
illegal. Nothing raises. No count looks wrong. The seats just fill, and
a controller publishes a roster that puts one pilot in two cockpits at
once.

assignment_service.ProvisionalDuties is what replaces the side effect,
and this file is what holds it in place.

WHY IT IS 36 ROTATIONS AND NOT 2. Two rotations prove the mechanism
exists. They do not prove it survives the case that matters, because at
two rotations almost any implementation looks correct, and — more
importantly — a HEALTHY crew pool hides the defect completely. With six
commanders and thirty-six rotations, the generator's own fairness
ordering spreads pilots six days apart all by itself, so the assignment
set comes out legal whether the provisional union works or not. The
defect only bites where real operations bite: a pool that is tight
against the schedule. That is the fixture below.

WHY TWO TESTS AND NOT ONE. The second test asserts that the first one
FAILS when the provisional union is neutralised. A guard that passes
both with and without the thing it guards is not a guard, and this
codebase has paid for that lesson before — see the round-trip counter's
own note that 647 tests passed while nothing measured what was broken.
The pair below makes "watched it fail in between" a permanent property
of the suite rather than something someone did once and wrote down.

NO DATABASE NEEDED, deliberately — same reasoning, and the same
isolate_from_database() net, as tests/test_generation_round_trips.py.
That net is imported rather than re-created: it enumerates every module
holding a get_engine/log_audit copy, and a second hand-rolled copy of it
is exactly the drift it was written to stop.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# tests/ itself, so the shared fixture below imports by module name
# regardless of how pytest was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import pytest

from test_generation_round_trips import CREW_COLUMNS, isolate_from_database

_WINDOW_START = dt.date(2026, 9, 1)
_ROTATIONS = 36
_ROTATIONS_PER_DAY = 3
_DAYS = _ROTATIONS // _ROTATIONS_PER_DAY          # 12

# A pool that is one commander short of the schedule. Three rotations
# operate simultaneously each day; two commanders exist. The third
# rotation each day therefore has no legal commander — every candidate
# is already airborne — and that is the ONLY thing making it illegal, so
# a failure here cannot be a marginal rest-arithmetic argument.
_COMMANDERS = 2
_SECOND_PILOTS = 2


def _valid_crew_row(crew_id, role, dob_year):
    """A crew member who PASSES. The round-trips fixture deliberately
    expires everyone so the search runs to exhaustion; this one is the
    opposite — the search must succeed, or "no pilot was double-booked"
    passes because no pilot was booked at all."""
    row = {column: None for column in CREW_COLUMNS}
    row.update({
        "crew_id": crew_id, "name": f"Test {crew_id}", "role": role,
        "base": "KHI", "is_active": True,
        "date_of_birth": dt.date(dob_year, 1, 1),
    })
    for column in CREW_COLUMNS:
        if column.endswith("_expiry"):
            row[column] = _WINDOW_START + dt.timedelta(days=365)
    return row


def _rotation_geometry(rotation_number: int):
    """(date, leg times) for rotation N of 36.

    Three rotations per day, staggered one hour apart, each two sectors
    out and back. Every pair of same-day rotations OVERLAPS in the air;
    each rotation on its own is an ordinary, legal domestic duty with
    ~18h rest before the next day's, so daily flying is fine and only
    the same-day clash is illegal.

    Legs are chronological, continuous (destination -> next origin) and
    non-overlapping WITHIN the duty, or build_duty() would reject the
    duty before any candidate was considered and the test would be
    measuring nothing.
    """
    index = rotation_number - 1
    day = _WINDOW_START + dt.timedelta(days=index // _ROTATIONS_PER_DAY)
    slot = index % _ROTATIONS_PER_DAY            # 0, 1 or 2 -> +0h, +1h, +2h
    offset = dt.timedelta(hours=slot)

    def at(hour, minute):
        return dt.datetime.combine(day, dt.time(hour, minute)) + offset

    return day, [
        # KHI -> LHE
        (at(6, 0), at(7, 45), "KHI", "LHE"),
        # LHE -> KHI
        (at(9, 0), at(10, 45), "LHE", "KHI"),
    ]


def _flight_row(flight_id):
    """flight_ids are allocated (n*10, n*10+1), so the low digit says
    which leg of rotation n this is."""
    rotation_number, leg = divmod(flight_id, 10)
    _, legs = _rotation_geometry(rotation_number)
    dep, arr, origin, destination = legs[leg]
    return {
        "flight_id": flight_id, "flight_no": f"EPE {700 + rotation_number}",
        "origin": origin, "destination": destination,
        "dep_time_planned": dep, "arr_time_planned": arr,
        "dep_time_actual": None, "arr_time_actual": None,
        "domestic": True, "status": "PLANNED", "meal_provided": True,
        "snack_provided": True, "cargo_dg": False,
    }


def _build_preview(monkeypatch, disable_provisional_union=False):
    """Runs the REAL generate_preview() over fake leaves.

    disable_provisional_union neutralises the seam
    assignment_service._provisional_duty_rows() — the one line that
    feeds this run's own decisions back into the legality gate.
    Everything else is untouched, so the difference between the two
    calls is exactly the mechanism under test and nothing else.
    """
    from services import assignment_service, crew_service, flight_service
    from services import roster_generator_service as rgs
    from services import rotation_template_service as rts

    crew = ([_valid_crew_row(f"CPT-{i:02d}", "CPT", 1980 + i) for i in range(1, _COMMANDERS + 1)]
            + [_valid_crew_row(f"FO-{i:02d}", "FO", 1990 + i) for i in range(1, _SECOND_PILOTS + 1)])
    crew_df = pd.DataFrame(crew, columns=CREW_COLUMNS)

    instances = pd.DataFrame([
        {"id": n, "rotation_code": f"EPE-{n:03d}",
         "rotation_date": _rotation_geometry(n)[0],
         "status": "APPROVED", "template_id": 1, "version": 1}
        for n in range(1, _ROTATIONS + 1)
    ])

    isolate_from_database(monkeypatch)
    monkeypatch.setattr(crew_service, "get_all_crew", lambda **k: crew_df.copy())
    monkeypatch.setattr(flight_service, "get_flight",
                        lambda fid: pd.Series(_flight_row(fid)))
    # An empty COMMITTED history, with the real column shape — so the
    # only duty history any candidate has is the one this run itself
    # decided. That is what makes the provisional union the sole
    # mechanism under test.
    monkeypatch.setattr(
        assignment_service, "_read_duty_rows",
        lambda *a, **k: pd.DataFrame(columns=assignment_service.DUTY_ROW_COLUMNS))
    monkeypatch.setattr(assignment_service, "_find_paired_pilot", lambda *a, **k: None)
    monkeypatch.setattr(
        assignment_service, "search_roster",
        lambda **k: pd.DataFrame(columns=["crew_id", "duty_id", "operating_position",
                                          "role_assigned", "duty_date"]))
    monkeypatch.setattr(
        assignment_service, "get_roster_for_flight",
        lambda *a, **k: pd.DataFrame(columns=["roster_id", "crew_id", "flight_id",
                                              "operating_position", "role_assigned", "status"]))
    monkeypatch.setattr(rts, "get_instances", lambda **k: instances.copy())
    monkeypatch.setattr(rts, "get_promoted_flight_ids", lambda iid: [iid * 10, iid * 10 + 1])

    if disable_provisional_union:
        monkeypatch.setattr(
            assignment_service, "_provisional_duty_rows",
            lambda *a, **k: pd.DataFrame(columns=assignment_service.DUTY_ROW_COLUMNS))

    return rgs.generate_preview(_WINDOW_START,
                                _WINDOW_START + dt.timedelta(days=_DAYS),
                                app_user="occ1")


def _double_bookings(preview):
    """Every case of one pilot being in two places at once, found from
    the preview's OWN recorded report/debrief times.

    Deliberately not a rule and not a call into the legality engine: two
    duties for the same human being that overlap on the clock is a plain
    contradiction, true under any regulation, and it cannot pass because
    a rule was misread. Returns (crew_id, earlier_rotation,
    later_rotation) triples.
    """
    spans = {}
    for rotation in preview.rotations:
        for seat in rotation.seats.values():
            if seat.crew_id is None or seat.report_time is None:
                continue
            spans.setdefault(seat.crew_id, []).append(
                (seat.report_time, seat.debrief_time, rotation.rotation_code))

    clashes = []
    for crew_id, entries in spans.items():
        entries.sort()
        for earlier, later in zip(entries, entries[1:]):
            if later[0] < earlier[1]:
                clashes.append((crew_id, earlier[2], later[2]))
    return clashes


def _filled_rotations(preview):
    from services.roster_generator_service import OUTCOME_PROPOSED
    return preview.by_outcome(OUTCOME_PROPOSED)


def test_a_pilot_is_never_proposed_for_two_overlapping_rotations(monkeypatch):
    """THE guard.

    Thirty-six rotations, three of them in the air together each day,
    two commanders. Every rotation is individually legal for either
    commander — same aircraft type, valid documents, ordinary domestic
    FDP, and ~18h of rest before the next day's. What is illegal is
    taking two of them at once, and that is only visible to a validator
    that can see what this same run already decided.

    Asserts on double-booking rather than on a specific rotation count,
    because the count is a consequence of the fairness ordering and the
    ordering is allowed to change; a pilot in two cockpits is not.
    """
    preview = _build_preview(monkeypatch)

    clashes = _double_bookings(preview)
    assert not clashes, (
        f"{len(clashes)} double-booking(s) proposed across a "
        f"{_ROTATIONS}-rotation window — the provisional union is not "
        f"reaching the legality gate. First few: {clashes[:5]}"
    )


def test_the_guard_is_not_vacuous(monkeypatch):
    """The guard above passes trivially if nothing was assigned.

    Deliberately asserts RANGES and REASONS, not exact counts. How many
    rotations a tight pool covers is a consequence of the fairness
    ordering — a CPT taken for a Second Pilot seat is a commander lost
    for the next rotation that day, so coverage moves when the ordering
    moves, and the ordering is allowed to move. Pinning the number here
    would make this file fail for reasons that have nothing to do with
    what it guards.

    What must hold is that the run did real work AND that the gate
    genuinely refused things, so the no-double-booking result above is
    an outcome rather than an absence.
    """
    from services.roster_generator_service import OUTCOME_UNCOVERED

    preview = _build_preview(monkeypatch)
    filled = _filled_rotations(preview)
    uncovered = preview.by_outcome(OUTCOME_UNCOVERED)

    assert len(preview.rotations) == _ROTATIONS, len(preview.rotations)
    assert len(filled) >= _DAYS, (
        f"only {len(filled)} of {_ROTATIONS} rotations filled — too few for "
        f"'no pilot was double-booked' to mean anything"
    )
    assert len(uncovered) > 0, "the gate refused nothing; the pool is not tight"
    assert len(filled) + len(uncovered) == _ROTATIONS

    # The refusals came from the FTL engine reading this run's own
    # provisional duties — not from the qualification pre-filter, and not
    # from an empty pool. This is the assertion that says the provisional
    # rows reached the rules rather than merely being stored: a negative
    # available-rest figure can only be computed against a duty the
    # engine can see.
    overlap_reasons = [r for r in uncovered if "overlap" in (r.outcome_reason or "").lower()]
    assert overlap_reasons, (
        "no rotation was refused for overlapping an earlier duty — the "
        "provisional rows are not reaching the legality engine. Reasons "
        f"seen: {[r.outcome_reason for r in uncovered][:2]}"
    )
    for rotation in uncovered:
        assert rotation.outcome_reason != "No candidates in pool", (
            f"{rotation.rotation_code} was rejected before the legality gate "
            f"ran — the fixture is not exercising the path under test"
        )

    # Nothing was written. The preview is a proposal until accepted, and
    # a test that could not tell the difference would not notice the
    # whole redesign being reverted.
    assert not preview.is_accepted


def test_the_guard_fails_when_the_provisional_union_is_disabled(monkeypatch):
    """The mutation, made permanent.

    With _provisional_duty_rows() neutralised, the run's own decisions
    stop reaching the gate. Every rotation then validates against an
    empty history, every one passes, and all thirty-six fill — including
    the third rotation of each day, whose commander is already airborne
    on one of the other two.

    This asserts the failure rather than describing it, because
    "we watched it fail once" is not a property of the suite and does
    not survive the next refactor. If this test ever passes with an
    empty `clashes`, the guard above has stopped guarding anything and
    both tests are lying together.

    It compares the two runs directly rather than pinning a number,
    which also records the shape of the defect: switching the check off
    makes the generator look STRICTLY MORE productive. Every rotation
    fills, nothing is reported uncovered, and the roster is illegal.
    That is why this failed silently in the first place — the bad
    outcome is the one that reads as success.
    """
    with_union = _build_preview(monkeypatch)
    without_union = _build_preview(monkeypatch, disable_provisional_union=True)

    assert len(_filled_rotations(without_union)) == _ROTATIONS, (
        "with no history at all, every rotation should look fillable — "
        "if it does not, this fixture is not isolating the union"
    )
    assert len(_filled_rotations(without_union)) > len(_filled_rotations(with_union))

    clashes = _double_bookings(without_union)
    assert len(clashes) >= _DAYS, (
        f"expected at least one double-booking per day across {_DAYS} days "
        f"with the provisional union disabled; found {len(clashes)}"
    )
    assert not _double_bookings(with_union), (
        "the two runs are indistinguishable — the seam is not being disabled"
    )
