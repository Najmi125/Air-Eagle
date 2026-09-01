"""
tests/test_roster_generator_service.py

DB-integration tests for services/roster_generator_service.py — Phase
7's final piece. Grounded in the real EPE 786/787 (domestic, Mon-Fri)
and EPE 802/804/805 (international, Tue/Thu/Fri/Sat) rotations, reused
end-to-end through the real template -> approval -> generation arc
(expand_and_persist() + approve_instance(), not synthetic shortcuts).

Every seat decision here goes through the real assign_pair_to_duty()/
assign_crew_to_duty() gate — these tests verify the generator's WIRING
(right pool, right window, right idempotency, right status/visibility)
and the specific, previously-broken domestic age-ordering scenario
from HANDOVER.md's 2026-08-04 entry, never re-derive FTL/pairing math
themselves.

Rebuilt for the flight-deck crew package (2026-08-13): seats are
Commander/Second Pilot (SeatResult.operating_position), not CPT/FO
(SeatResult.role) — crew are still graded CPT/FO as before (crew.role,
unchanged), but SEAT_ELIGIBLE_GRADES makes a CPT eligible for EITHER
seat now, not just Commander. Several fairness/ordering scenarios
below were re-derived rather than mechanically renamed, since that
widened eligibility pool genuinely changes which crewing outcomes are
reachable — see each test's own docstring for what changed and why.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
from sqlalchemy import text

import services.rotation_template_service as rts
import services.roster_generator_service as rgs
import services.assignment_service as assignment_service
import services.crew_service as crew_service
import services.flight_service as flight_service
from services.assistant import reports as assistant_reports
from services.assistant.query_parser import ReportRequest
from core.duty_summary import group_roster_rows_into_duties

DOMESTIC_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
     "flight_no": "EPE 786", "domestic": True},
    {"leg_order": 2, "origin": "LHE", "destination": "KHI",
     "dep_time": dt.time(22, 0), "arr_time": dt.time(23, 45),
     "flight_no": "EPE 787", "domestic": True},
]
DOMESTIC_DAYS = [1, 2, 3, 4, 5]  # ISO weekday, Mon-Fri

INTERNATIONAL_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(1, 45), "arr_time": dt.time(3, 30),
     "flight_no": "EPE 802", "domestic": False},
    {"leg_order": 2, "origin": "LHE", "destination": "DWC",
     "dep_time": dt.time(4, 30), "arr_time": dt.time(8, 0),
     "flight_no": "EPE 804", "domestic": False},
    {"leg_order": 3, "origin": "DWC", "destination": "KHI",
     "dep_time": dt.time(9, 0), "arr_time": dt.time(11, 0),
     "flight_no": "EPE 805", "domestic": False},
]
INTERNATIONAL_DAYS = [2, 4, 5, 6]  # ISO weekday, Tue/Thu/Fri/Sat

_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY, "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY, "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY, "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY, "dg_expiry": _FAR_FUTURE_EXPIRY,
}
_YOUNG_DOB = dt.date(1985, 1, 1)  # ~41 in the 2026 test window -- clear of the 65 threshold


@pytest.fixture(autouse=True)
def _patch_engine(_patch_all_service_engines):
    return _patch_all_service_engines


def _add_crew(role, dob=_YOUNG_DOB, **overrides):
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI", "date_of_birth": dob}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _make_domestic_instances(date_from, date_to, rotation_code="EPE-786-787"):
    rts.create_template(
        rotation_code=rotation_code, days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2026, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-KHI domestic",
    )
    created = rts.expand_and_persist(rotation_code, date_from, date_to)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created


def _make_international_instances(date_from, date_to, rotation_code="EPE-802-805"):
    rts.create_template(
        rotation_code=rotation_code, days_of_week=INTERNATIONAL_DAYS, legs=INTERNATIONAL_LEGS,
        effective_from=dt.date(2026, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-DWC-KHI international",
    )
    created = rts.expand_and_persist(rotation_code, date_from, date_to)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created


def _roster_duty_count(engine):
    """Distinct DUTIES, not sector rows — 003_roster_table.sql's own
    header, in capitals, warns exactly against this: roster stores ONE
    ROW PER CREW PER FLIGHT SECTOR, so a raw SELECT COUNT(*) over-counts
    by the leg count (a 2-leg rotation crewed by CPT+FO is 4 rows, 2
    duties). core/duty_summary.py's group_roster_rows_into_duties() is
    the canonical dedup — used here rather than a raw COUNT(*), which
    is what keeps this helper honest about which unit it measures."""
    df = pd.read_sql(text("SELECT * FROM roster"), engine)
    return len(group_roster_rows_into_duties(df))


def _seed_seat(engine, crew_id, flight_ids, role_assigned, operating_position, status="PLANNED"):
    """Seeds a real, ACTIVE occupant of one seat directly via SQL —
    represents a seat pre-filled by some OTHER means (a manual
    assignment made before the generator ever ran) without needing a
    real partner to already exist, which the real pair-assignment API
    can no longer construct one seat at a time. generate_for_window()'s
    _seat_occupant() check doesn't care how a row got there, only that
    it's ACTIVE — same reasoning as tests/test_assignment_service.py's
    own _seed_seat_occupant()."""
    import uuid
    duty_id = f"SEEDED-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        for fid in flight_ids:
            flight = flight_service.get_flight(fid)
            conn.execute(text("""
                INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                    report_time, debrief_time, fdp_hours, role_assigned, operating_position, status)
                VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                    :report_time, :debrief_time, :fdp_hours, :role_assigned, :operating_position, :status)
            """), {
                "crew_id": crew_id, "flight_id": fid, "duty_id": duty_id,
                "duty_date": flight["dep_time_planned"].date(),
                "report_time": flight["dep_time_planned"], "debrief_time": flight["arr_time_planned"],
                "fdp_hours": 2.0, "role_assigned": role_assigned,
                "operating_position": operating_position, "status": status,
            })
    return duty_id


def _seed_proposed_pair(instance_id, commander_id, second_pilot_id):
    """A fully-crewed rotation left in PROPOSED — what generation used to
    produce before accept started writing PLANNED (2026-09-01).

    Written through the REAL pair API, not SQL, so these rows are exactly
    what the old generator left behind: validated, paired, atomic. The
    publish tests below need such rows and can no longer get them from
    generate_for_window(), which is the only reason this helper exists —
    publish_window() itself is untouched, and so is everything it
    guarantees."""
    flight_ids = rts.get_promoted_flight_ids(instance_id)
    result = assignment_service.assign_pair_to_duty(
        commander_id, second_pilot_id, flight_ids, roster_status="PROPOSED")
    assert result.status == "ALLOWED", result.status
    return flight_ids


# ------------------------------------------------------------------
# Basic fill + status
# ------------------------------------------------------------------

def test_generate_for_window_fills_domestic_rotation_as_planned(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    _add_crew("CPT")
    _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    assert len(summary.filled) == 2
    assert {s.operating_position for s in summary.filled} == {"COMMANDER", "SECOND_PILOT"}
    assert summary.uncovered == []
    assert summary.already_covered == []

    # PLANNED, not PROPOSED (operator decision, 2026-09-01): accept IS
    # publication, so generate_for_window() -- which is preview+accept in
    # one call -- leaves a roster crew can see. See
    # roster_generator_service.ACCEPTED_ROSTER_STATUS.
    roster = assignment_service.search_roster(
        date_from=dt.date(2026, 8, 3), date_to=dt.date(2026, 8, 3), include_proposed=True)
    assert set(roster["status"]) == {"PLANNED"}


def test_generate_for_window_fills_international_rotation_as_planned(_patch_engine):
    _make_international_instances(dt.date(2026, 8, 4), dt.date(2026, 8, 4))
    _add_crew("CPT")
    _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 4), dt.date(2026, 8, 4))

    assert len(summary.filled) == 2
    assert summary.uncovered == []


def test_generate_for_window_no_approved_instances_in_range_is_a_no_op(_patch_engine):
    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert summary.filled == summary.uncovered == summary.already_covered == []


# ------------------------------------------------------------------
# The real gate, not the generator, decides legality — back-to-back
# international is illegal (measured rest/FDP constraint), back-to-back
# domestic is legal. Both proven by the OUTCOME of real assign_pair_
# to_duty() calls, never asserted by re-deriving the rest math here.
# ------------------------------------------------------------------

def test_back_to_back_international_duty_for_the_sole_candidate_leaves_both_seats_uncovered(_patch_engine):
    """Thu 8/6 -> Fri 8/7: consecutive international operating days.
    Exactly ONE CPT candidate exists (the only possible Commander), so
    if Friday's seats still get filled, it can only be because the
    real gate allowed a second consecutive international duty for that
    same pilot.

    Under the pair model, atomicity means BOTH seats go uncovered
    together on Friday, not just Commander: assign_pair_to_duty()
    tries every (sole Commander candidate, Second Pilot candidate)
    combination, and every one fails for the SAME reason (the
    Commander's own rest breach) regardless of which Second Pilot is
    tried — an individually-fine Second Pilot candidate doesn't get a
    seat on their own, since "both seats validated and committed
    together, or neither" is the whole point of assign_pair_to_duty().
    This is a deliberate consequence of pair atomicity, not a gap."""
    _make_international_instances(dt.date(2026, 8, 6), dt.date(2026, 8, 7))
    _add_crew("CPT")
    _add_crew("FO")
    _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 6), dt.date(2026, 8, 7))

    thu_filled = [s for s in summary.filled if s.rotation_date == dt.date(2026, 8, 6)]
    assert {s.operating_position for s in thu_filled} == {"COMMANDER", "SECOND_PILOT"}

    fri_uncovered_positions = {s.operating_position for s in summary.uncovered
                                if s.rotation_date == dt.date(2026, 8, 7)}
    assert fri_uncovered_positions == {"COMMANDER", "SECOND_PILOT"}


def test_back_to_back_domestic_duty_for_the_sole_candidate_is_allowed(_patch_engine):
    """Mon 8/3 -> Tue 8/4, one CPT and one FO only -- if domestic
    back-to-back really is legal (the grounding fact this session
    already measured), both days must get fully covered by that same
    sole pair; any UNCOVERED result here would mean the real gate
    rejected a domestic pairing this codebase has already confirmed
    legal."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 4))
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 4))

    assert summary.uncovered == []
    assert len(summary.filled) == 4
    assert {s.crew_id for s in summary.filled if s.operating_position == "COMMANDER"} == {cpt_id}
    assert {s.crew_id for s in summary.filled if s.operating_position == "SECOND_PILOT"} == {fo_id}


# ------------------------------------------------------------------
# Fairness: even duty counts within a seat, scoped to the window.
# SEAT_ELIGIBLE_GRADES widens SECOND_PILOT eligibility to CPT-or-FO
# (2026-08-12), so a CPT/FO cross-pool comparison no longer has a
# fixed meaning the way it did under the old exact-grade-match model
# — this only asserts what's still structurally guaranteed: COMMANDER
# duty counts stay even within the CPT pool (the only pool eligible
# for that seat, unchanged by the widening).
# ------------------------------------------------------------------

def test_commander_duty_counts_stay_even_across_the_cpt_pool(_patch_engine):
    """6 CPT (the real Air Eagle Commander pool size), one domestic
    rotation Mon-Fri (5 Commander seats). Even counts (max-min <= 1)
    falls out of fewest-duty-first ordering alone."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 7))
    cpt_ids = [_add_crew("CPT") for _ in range(6)]
    fo_ids = [_add_crew("FO") for _ in range(4)]

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 7))

    assert summary.uncovered == []
    assert len(summary.filled) == 10  # 5 rotations x 2 seats

    commander_counts = {cid: 0 for cid in cpt_ids}
    second_pilot_counts = {cid: 0 for cid in cpt_ids + fo_ids}
    for s in summary.filled:
        if s.operating_position == "COMMANDER":
            commander_counts[s.crew_id] += 1
        else:
            second_pilot_counts[s.crew_id] += 1

    assert max(commander_counts.values()) - min(commander_counts.values()) <= 1
    # Second Pilot counts stay even across whoever ACTUALLY got used for
    # that seat this run (not necessarily every eligible person — a CPT
    # who was never picked as Second Pilot has a legitimate 0, same as
    # fewest-duty-first ordering intends).
    used_second_pilots = {cid: n for cid, n in second_pilot_counts.items() if n > 0}
    if used_second_pilots:
        assert max(used_second_pilots.values()) - min(used_second_pilots.values()) <= 1


# ------------------------------------------------------------------
# Age-aware candidate ordering (HANDOVER.md, 2026-08-04 grounding;
# re-derived for the pair model, 2026-08-13): a 65+ Commander candidate
# tried FIRST (fewest-duty-first ordering has no other basis to prefer
# a younger one when both start at 0 duties) must not doom the seat —
# the Second Pilot search for THAT commander is itself age-aware
# (core/roster_generation.py's order_candidates(), domestic +
# partner_age>=65 -> under-65-first), so it finds the one under-65
# partner immediately rather than the seat going uncovered. Unlike the
# old sequential CPT/FO-fill model, the widened SECOND_PILOT eligibility
# means this now resolves by keeping the 65+ pilot AS Commander and
# pairing them with a young Second Pilot, not by avoiding them — a
# different, but equally correct, way of finding a legal crewing.
# ------------------------------------------------------------------

def test_domestic_seat_fully_crewed_when_first_tried_commander_is_65_plus(_patch_engine):
    old_cpt = _add_crew("CPT", dob=dt.date(1959, 1, 1))    # 67 in Aug 2026 -- tried first (duty_count tie, insertion order)
    young_fo = _add_crew("FO", dob=dt.date(1986, 1, 1))    # 40 -- the one legal Second Pilot
    _add_crew("FO", dob=dt.date(1958, 1, 1))               # 68 -- decoy, would also be illegal if tried
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    assert summary.uncovered == []
    filled_by_position = {s.operating_position: s.crew_id for s in summary.filled}
    assert filled_by_position["COMMANDER"] == old_cpt
    assert filled_by_position["SECOND_PILOT"] == young_fo


# ------------------------------------------------------------------
# Idempotency
# ------------------------------------------------------------------

def test_generate_for_window_is_idempotent_on_a_fully_generated_window(_patch_engine):
    engine = _patch_engine
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    _add_crew("CPT")
    _add_crew("FO")

    first = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert len(first.filled) == 2

    second = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert second.filled == []
    assert len(second.already_covered) == 2
    assert _roster_duty_count(engine) == 2  # not duplicated (2 duties: Commander's, Second Pilot's)


def test_generate_for_window_only_fills_gaps_on_a_partially_generated_window(_patch_engine):
    """Monday's Commander seat pre-filled by some other means (a
    manual assignment made before the generator ever ran) — seeded
    directly, since the real pair-assignment API can no longer
    construct a real one-seat-only state from scratch (see _seed_seat()'s
    own docstring). Generate must recognize it as already covered and
    only fill the genuine gap (Second Pilot), on Monday; Tuesday, with
    neither seat real yet, gets a fresh pair."""
    engine = _patch_engine
    instance_ids = _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 4))
    cpt_id = _add_crew("CPT")
    _add_crew("FO")

    mon_flight_ids = rts.get_promoted_flight_ids(instance_ids[0])
    _seed_seat(engine, cpt_id, mon_flight_ids, "CPT", "COMMANDER")

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 4))

    # Monday's Commander seat was already covered (manually) -- untouched.
    mon_already = [s for s in summary.already_covered if s.rotation_date == dt.date(2026, 8, 3)]
    assert len(mon_already) == 1
    assert mon_already[0].operating_position == "COMMANDER"
    assert mon_already[0].crew_id == cpt_id

    mon_filled_positions = {s.operating_position for s in summary.filled if s.rotation_date == dt.date(2026, 8, 3)}
    assert mon_filled_positions == {"SECOND_PILOT"}  # only the genuine gap

    tue_filled_positions = {s.operating_position for s in summary.filled if s.rotation_date == dt.date(2026, 8, 4)}
    assert tue_filled_positions == {"COMMANDER", "SECOND_PILOT"}

    mon_cpt_roster = assignment_service.get_roster_for_flight(mon_flight_ids[0], include_proposed=True)
    mon_cpt_row = mon_cpt_roster[mon_cpt_roster["operating_position"] == "COMMANDER"].iloc[0]
    assert mon_cpt_row["status"] == "PLANNED"  # never touched/replaced


def test_uncovered_seat_pair_is_retried_and_succeeds_once_the_blocker_is_removed(_patch_engine):
    """A missing qualification field, not is_active=False: is_active
    isn't in crew_service.UPDATABLE_FIELDS, so _add_crew(is_active=False)
    would silently no-op (add_crew() filters crew_data to that
    allowlist before inserting) and there's no reactivate_crew()
    either — update_crew(..., {"is_active": True}) would silently
    no-op the same way. license_expiry IS in UPDATABLE_FIELDS, giving
    a real, achievable block-then-unblock via the actual service API.

    Under the pair model, the Commander's own NEEDS_MANUAL_REVIEW
    (missing license) holds the WHOLE pair, not just their own seat —
    both seats come back uncovered on the first run, even though the
    Second Pilot candidate is individually fine, same atomicity
    reasoning as the back-to-back-international test above."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    cpt_id = _add_crew("CPT", license_expiry=None)
    fo_id = _add_crew("FO")

    first = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert {s.operating_position for s in first.uncovered} == {"COMMANDER", "SECOND_PILOT"}

    crew_service.update_crew(cpt_id, {"license_expiry": dt.date(2099, 1, 1)})

    second = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert second.uncovered == []
    assert second.already_covered == []  # nothing was written on the first, blocked run
    filled_by_position = {s.operating_position: s.crew_id for s in second.filled}
    assert filled_by_position == {"COMMANDER": cpt_id, "SECOND_PILOT": fo_id}


# ------------------------------------------------------------------
# uncovered_seats (migrations/017) — durable, survives a page refresh
# unlike the in-memory GenerationSummary. get_open_uncovered_seats()
# is the read side pages/6_Roster_Generation.py now uses.
# ------------------------------------------------------------------

def test_uncovered_seats_table_written_on_genuine_no_legal_pair_outcome(_patch_engine):
    engine = _patch_engine
    _make_international_instances(dt.date(2026, 8, 6), dt.date(2026, 8, 7))
    _add_crew("CPT")
    _add_crew("FO")
    _add_crew("FO")

    rgs.generate_for_window(dt.date(2026, 8, 6), dt.date(2026, 8, 7))

    open_rows = pd.read_sql(text("SELECT * FROM uncovered_seats WHERE resolved_at IS NULL"), engine)
    assert len(open_rows) == 2  # Friday's Commander + Second Pilot
    assert set(open_rows["operating_position"]) == {"COMMANDER", "SECOND_PILOT"}
    assert all(open_rows["reason"].str.len() > 0)


def test_get_open_uncovered_seats_survives_in_memory_summary_being_gone(_patch_engine):
    """Simulates a refresh: the in-memory GenerationSummary from a
    prior generate_for_window() call is simply never referenced again
    — get_open_uncovered_seats(), reading the DB directly, must still
    report the same gap."""
    _make_international_instances(dt.date(2026, 8, 6), dt.date(2026, 8, 7))
    _add_crew("CPT")
    _add_crew("FO")

    rgs.generate_for_window(dt.date(2026, 8, 6), dt.date(2026, 8, 7))  # summary discarded, never read

    durable = rgs.get_open_uncovered_seats(dt.date(2026, 8, 6), dt.date(2026, 8, 7))
    assert len(durable) == 2
    assert set(durable["operating_position"]) == {"COMMANDER", "SECOND_PILOT"}
    assert set(durable["rotation_date"]) == {dt.date(2026, 8, 7)}


def test_get_open_uncovered_seats_excludes_resolved_rows(_patch_engine):
    """Re-running Generate after the blocker clears resolves the open
    row (via assign_crew_to_duty()/assign_pair_to_duty()'s own
    _resolve_uncovered_seat() call) — the durable read must not keep
    showing it."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    cpt_id = _add_crew("CPT", license_expiry=None)
    _add_crew("FO")

    rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert len(rgs.get_open_uncovered_seats(dt.date(2026, 8, 3), dt.date(2026, 8, 3))) == 2

    crew_service.update_crew(cpt_id, {"license_expiry": dt.date(2099, 1, 1)})
    rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    assert len(rgs.get_open_uncovered_seats(dt.date(2026, 8, 3), dt.date(2026, 8, 3))) == 0


def test_get_open_uncovered_seats_reflects_manual_unassign_too(_patch_engine):
    """Not just the generator's own writer — remove_assignment_from_
    duty() vacating a rotation-linked seat must show up here too,
    uncovered_seats being the single durable source of truth for
    "which seats are currently empty," not a generator-only log."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    cpt_id = _add_crew("CPT")
    _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert summary.uncovered == []
    commander_row = next(s for s in summary.filled if s.operating_position == "COMMANDER")
    flight_ids = rts.get_promoted_flight_ids(commander_row.rotation_instance_id)
    roster = assignment_service.get_roster_for_flight(flight_ids[0], include_proposed=True)
    duty_id = roster[roster["crew_id"] == cpt_id].iloc[0]["duty_id"]

    assignment_service.remove_assignment_from_duty(cpt_id, duty_id, reason="sick")

    open_rows = rgs.get_open_uncovered_seats(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert len(open_rows) == 1
    assert open_rows.iloc[0]["operating_position"] == "COMMANDER"


# ------------------------------------------------------------------
# publish_window — per-rotation re-validation gate (2026-08-12,
# flight-deck crew package): both seats must be filled AND the actual
# assigned pair must re-validate LEGAL/WARNING fresh at publish time,
# not just trust the original PROPOSED-time result.
# ------------------------------------------------------------------

def test_publish_window_flips_only_proposed_rows_in_range(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    _make_domestic_instances(dt.date(2026, 9, 1), dt.date(2026, 9, 1), rotation_code="EPE-786-787-SEP")
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    aug = rts.get_instances(status="APPROVED")
    aug_id = int(aug[aug["rotation_date"] == dt.date(2026, 8, 3)].iloc[0]["id"])
    sep_id = int(aug[aug["rotation_date"] == dt.date(2026, 9, 1)].iloc[0]["id"])
    _seed_proposed_pair(aug_id, cpt_id, fo_id)
    _seed_proposed_pair(sep_id, cpt_id, fo_id)

    published = rgs.publish_window(dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    # publish_window() returns a raw UPDATE rowcount (sector rows, per
    # its own docstring), not a duty count — 2 legs x 2 crew (Commander
    # + Second Pilot) in the August rotation = 4 rows flipped, not 2 duties.
    assert published == 4

    aug_roster = assignment_service.search_roster(
        date_from=dt.date(2026, 8, 3), date_to=dt.date(2026, 8, 3), include_proposed=True)
    assert set(aug_roster["status"]) == {"PLANNED"}

    sep_roster = assignment_service.search_roster(
        date_from=dt.date(2026, 9, 1), date_to=dt.date(2026, 9, 1), include_proposed=True)
    assert set(sep_roster["status"]) == {"PROPOSED"}  # untouched -- out of range


def test_publish_window_with_nothing_proposed_in_range_returns_zero(_patch_engine):
    assert rgs.publish_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3)) == 0


def test_publish_window_skips_rotation_with_only_one_seat_filled(_patch_engine):
    """A rotation missing one seat (e.g. the other pilot was manually
    unassigned after Generate ran) must not publish at all — skipped,
    left PROPOSED, rather than publishing an incomplete cockpit."""
    engine = _patch_engine
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    instance_id = int(rts.get_instances(status="APPROVED").iloc[0]["id"])
    flight_ids = _seed_proposed_pair(instance_id, cpt_id, fo_id)
    roster = assignment_service.get_roster_for_flight(flight_ids[0], include_proposed=True)
    duty_id = roster[roster["crew_id"] == cpt_id].iloc[0]["duty_id"]
    assignment_service.remove_assignment_from_duty(cpt_id, duty_id)

    published = rgs.publish_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    assert published == 0
    remaining = assignment_service.search_roster(
        date_from=dt.date(2026, 8, 3), date_to=dt.date(2026, 8, 3), include_proposed=True)
    assert set(remaining["status"]) == {"PROPOSED"}


def test_publish_window_skips_rotation_that_fails_fresh_revalidation(_patch_engine):
    """A rotation whose pair was legal at PROPOSED time but no longer
    is by publish time (crew data changed in between) must be skipped,
    not published on stale trust of the original result — and must not
    take down the rest of the window's publish."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    _make_domestic_instances(dt.date(2026, 9, 1), dt.date(2026, 9, 1), rotation_code="EPE-786-787-SEP")
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    for _, instance in rts.get_instances(status="APPROVED").iterrows():
        _seed_proposed_pair(int(instance["id"]), cpt_id, fo_id)
    # The Commander's license expires between PROPOSED and publish time.
    crew_service.update_crew(cpt_id, {"license_expiry": dt.date(2020, 1, 1)})

    published = rgs.publish_window(dt.date(2026, 8, 1), dt.date(2026, 9, 30))

    assert published == 0  # both rotations use the same now-unqualified Commander
    still_proposed = assignment_service.search_roster(
        date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 9, 30), include_proposed=True)
    assert set(still_proposed["status"]) == {"PROPOSED"}


# ------------------------------------------------------------------
# Visibility: PROPOSED counts as covered for OCC's own coverage report,
# but stays hidden from crew-facing reads until published -- the "crew
# sees only published" requirement, and roster_coverage()'s one
# deliberate exception to it.
# ------------------------------------------------------------------

def test_roster_coverage_shows_proposed_seat_as_covered_but_crew_read_hides_it(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    # Seeded PROPOSED deliberately: generation writes PLANNED now, and
    # what this test pins is the PROPOSED contract itself -- covered for
    # OCC, hidden from crew -- which still governs every pre-2026-09-01
    # row and every roster_coverage() read of one.
    instance_id = int(rts.get_instances(status="APPROVED").iloc[0]["id"])
    _seed_proposed_pair(instance_id, cpt_id, fo_id)

    dataset = assistant_reports.roster_coverage(
        ReportRequest(date_from=dt.date(2026, 8, 3), date_to=dt.date(2026, 8, 3)))
    assert len(dataset.rows) == 2  # EPE 786 + EPE 787
    for row in dataset.rows:
        cpt_cell, fo_cell = row[3], row[4]
        assert cpt_cell != "UNCOVERED"
        assert fo_cell != "UNCOVERED"

    assert assignment_service.get_roster_for_crew(cpt_id).empty
    assert not assignment_service.get_roster_for_crew(cpt_id, include_proposed=True).empty
