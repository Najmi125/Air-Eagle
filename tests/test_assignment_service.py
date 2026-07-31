"""
tests/test_assignment_service.py

Every "illegal" scenario here is built from the actual D21 charter
rest formula (max(12h, 2xFDP)), already verified independently in
tests/test_pcaa_ano012_core.py — not just asserted to be illegal by
fiat. If D21's numbers ever change, these scenarios need re-deriving,
which is the point: they're tied to the real rule, not a guess.
"""
import sys
from pathlib import Path
import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pandas as pd
from sqlalchemy import text

import services.assignment_service as assignment_service
import services.crew_service as crew_service
import services.flight_service as flight_service
import services.audit_service as audit_service


@pytest.fixture(autouse=True)
def _patch_engine(migrated_db, monkeypatch):
    for mod in (assignment_service, crew_service, flight_service, audit_service):
        monkeypatch.setattr(mod, "get_engine", lambda: migrated_db)
    return migrated_db


# A safely-future date for every qualification expiry field, so a
# crew member created via _add_crew() is fully qualified by default.
# Existing tests in this file are about FDP/rest/role logic, not
# qualifications — without this default, EVERY one of them would
# start failing with NEEDS_REVIEW (missing expiry date) the moment
# the qualification gate (2026-07-31) was added, which would silently
# swap what these tests are actually verifying. Tests that need to
# exercise the qualification gate itself override individual fields.
_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY,
    "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY,
    "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY,
    "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY,
    "dg_expiry": _FAR_FUTURE_EXPIRY,
}


def _add_crew(role="CPT", crew_id_hint=None, **overrides):
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    cid = crew_service.add_crew(crew_data)
    return cid


def _add_flight(dep, arr, domestic=True, origin="KHI", destination="LHE"):
    return flight_service.add_flight({
        "origin": origin, "destination": destination,
        "dep_time_planned": dep, "arr_time_planned": arr,
        "domestic": domestic,
    })


def _audit_rows(engine, action_type=None):
    q = "SELECT * FROM audit_log"
    params = {}
    if action_type:
        q += " WHERE action_type = :at"
        params["at"] = action_type
    return pd.read_sql(text(q), engine, params=params)


def _seed_duty(engine, crew_id, flight_id, role_assigned, report_time, debrief_time, fdp_hours):
    """
    Insert a roster row directly via SQL, bypassing
    assign_crew_to_duty()/assign_crew_to_new_flights() entirely.

    Needed because an 8h+ FDP duty now correctly triggers
    NEEDS_MANUAL_REVIEW (D25 nutrition-data-missing — meal/snack
    provision is never populated by this codebase yet) and therefore
    never gets written through the real assignment API. Several
    tests need such a duty to already exist as GIVEN history (to set
    up a D21 rest-conflict scenario, or to test candidate exclusion
    against existing history) without re-exercising the
    NEEDS_MANUAL_REVIEW gate itself — that gate has its own dedicated
    tests. This mirrors the same "seed given state via raw SQL"
    pattern already used in tests/test_schema.py.
    """
    import uuid
    duty_id = f"SEEDED-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                report_time, debrief_time, fdp_hours, role_assigned)
            VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                :report_time, :debrief_time, :fdp_hours, :role_assigned)
        """), {
            "crew_id": crew_id, "flight_id": flight_id, "duty_id": duty_id,
            "duty_date": report_time.date(), "report_time": report_time,
            "debrief_time": debrief_time, "fdp_hours": fdp_hours,
            "role_assigned": role_assigned,
        })
    return duty_id


# ------------------------------------------------------------------
# Immediate legality gate
# ------------------------------------------------------------------

def test_legal_assignment_is_allowed_and_written(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "ALLOWED"
    assert len(result.roster_ids) == 1

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1
    assert roster_df.iloc[0]["fdp_hours"] == pytest.approx(3.0)


def test_multi_sector_duty_creates_one_row_per_flight_sharing_duty_id(_patch_engine):
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                      origin="KHI", destination="LHE")
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0),
                      origin="LHE", destination="KHI")

    result = assignment_service.assign_crew_to_duty(crew_id, [f1, f2], "CPT")

    assert result.status == "ALLOWED"
    assert len(result.roster_ids) == 2

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 2
    assert roster_df["duty_id"].nunique() == 1  # both sectors, ONE duty
    assert roster_df["fdp_hours"].nunique() == 1  # same fdp_hours on both rows


def test_insufficient_rest_after_prior_duty_is_rejected(_patch_engine):
    """8h FDP duty requires max(12h, 2*8)=16h rest after (D21).
    A next assignment only 5h later must be REJECTED, and nothing
    written to roster for it."""
    crew_id = _add_crew("CPT")

    # Seeded directly, not via the real API: an 8h FDP duty now
    # correctly triggers NEEDS_MANUAL_REVIEW through assign_crew_to_duty()
    # (meal/snack data is never populated), which has its own dedicated
    # tests. This test is about what a SECOND, shorter assignment does
    # given such a duty already exists in history — not about re-testing
    # that gate.
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, f1, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    # Only 5h after debrief (12:15) — needs 16h. Should reject.
    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    result2 = assignment_service.assign_crew_to_duty(crew_id, [f2], "CPT")

    assert result2.status == "REJECTED"
    assert result2.legality_status == "ILLEGAL"

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1  # only the seeded prior duty, nothing for the rejected one


def test_rejected_assignment_still_writes_audit_record(_patch_engine):
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, f1, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    assignment_service.assign_crew_to_duty(crew_id, [f2], "CPT", app_user="tester")

    audit = _audit_rows(_patch_engine, "ASSIGNMENT_REJECTED")
    assert len(audit) == 1
    assert audit.iloc[0]["legality_result"] == "ILLEGAL"


def test_mixed_domestic_international_duty_uses_international_buffer(_patch_engine):
    """The real KHI-LHE-DWC-KHI rotation mixes a domestic-classified
    sector (KHI-LHE) with international ones (LHE-DWC, DWC-KHI)
    within one duty. This must NOT be rejected with a ValueError —
    any international sector makes the whole duty use the
    international (60/30) buffer, not the domestic (45/15) one,
    while each flight keeps its own domestic flag for Flight
    Log/reporting purposes.

    This particular duty is 9.5h, which correctly triggers
    NEEDS_MANUAL_REVIEW (D25 nutrition data missing) — nothing gets
    written to roster for a held assignment, so the buffer
    calculation itself is checked via computed_report_time/
    computed_debrief_time, which are populated regardless of status."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                      origin="KHI", destination="LHE", domestic=True)
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0),
                      origin="LHE", destination="DWC", domestic=False)
    f3 = _add_flight(dt.datetime(2026, 7, 20, 11, 0), dt.datetime(2026, 7, 20, 13, 0),
                      origin="DWC", destination="KHI", domestic=False)

    result = assignment_service.assign_crew_to_duty(crew_id, [f1, f2, f3], "CPT")

    assert result.status == "NEEDS_REVIEW"  # 9.5h FDP, no meal data — correctly held
    # report_time = first dep (05:00) - 60min (international buffer,
    # NOT domestic's 45min, since one sector is international)
    assert result.computed_report_time == dt.datetime(2026, 7, 20, 4, 0)
    # debrief_time = last arr (13:00) + 30min (international, not 15)
    assert result.computed_debrief_time == dt.datetime(2026, 7, 20, 13, 30)

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 0  # held, not written


def test_all_domestic_duty_still_uses_domestic_buffer(_patch_engine):
    """Sanity check the other direction — a duty where every sector
    is genuinely domestic must still get the domestic (45/15) buffer,
    not be pushed to international by the fix above."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                      origin="KHI", destination="LHE", domestic=True)
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0),
                      origin="LHE", destination="KHI", domestic=True)

    result = assignment_service.assign_crew_to_duty(crew_id, [f1, f2], "CPT")
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert roster_df.iloc[0]["report_time"] == dt.datetime(2026, 7, 20, 4, 15)   # 45min
    assert roster_df.iloc[0]["debrief_time"] == dt.datetime(2026, 7, 20, 10, 15)  # 15min


def test_geographically_disconnected_legs_rejected(_patch_engine):
    """Two flights that don't actually connect (arrival city != next
    departure city) can't form one physically continuous duty — a
    crew member can't be in two places at once."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                      origin="KHI", destination="LHE")
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0),
                      origin="KHI", destination="LHE")  # departs KHI, not LHE — disconnected

    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_duty(crew_id, [f1, f2], "CPT")


def test_unknown_flight_id_raises(_patch_engine):
    crew_id = _add_crew("CPT")
    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_duty(crew_id, [999999], "CPT")


def test_unknown_crew_id_raises(_patch_engine):
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0))
    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_duty("NO-SUCH-CREW", [flight_id], "CPT")


# ------------------------------------------------------------------
# Role-match enforcement — the actual fix for a confirmed critical
# bypass: role_assigned was never cross-checked against the crew
# member's real registered role, while the FTL exemption decision
# correctly used the real role. An ENGR (FTL-exempt) crew member
# could be assigned with role_assigned="CPT", retaining the
# exemption while being recorded as filling the Captain role with
# zero FDP/rest checking ever applied.
# ------------------------------------------------------------------

def test_role_assigned_must_match_crew_actual_role(_patch_engine):
    """The exact bypass scenario: an ENGR crew member (FTL-exempt)
    must NOT be assignable with role_assigned='CPT'. This would
    otherwise retain the FTL exemption (decided from crew_row['role'])
    while being recorded as filling a role that should be fully
    subject to FDP/rest checking."""
    engr_crew = _add_crew("ENGR")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_duty(engr_crew, [flight_id], "CPT")


def test_role_assigned_matching_real_role_succeeds(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")
    assert result.status == "ALLOWED"


def test_role_match_recognizes_ame_engr_synonym(_patch_engine):
    """A crew member registered with role 'AME' (stored as canonical
    'ENGR') must still be assignable with role_assigned='AME' — the
    synonym must be recognized on the comparison side too, not just
    at storage time."""
    # Must go through _add_crew(), not crew_service.add_crew()
    # directly — the qualification gate (2026-07-31) needs the
    # expiry defaults _add_crew() sets, or this crew member has no
    # expiry dates and correctly trips NEEDS_REVIEW instead of
    # ALLOWED. _add_crew's role kwarg passes straight through to
    # crew_service.add_crew(), so the AME synonym is exercised
    # exactly as before.
    crew_id = _add_crew("AME")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "AME")
    assert result.status == "ALLOWED"


def test_role_match_is_case_insensitive(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "cpt")
    assert result.status == "ALLOWED"


def test_role_mismatch_via_control_room_path_also_rejected(_patch_engine):
    """The same enforcement must apply through
    assign_crew_to_new_flights() (Control Room) — both paths share
    _validate_new_duty(), so this is really confirming they stayed
    in sync."""
    engr_crew = _add_crew("ENGR")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]

    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_new_flights(engr_crew, flights_data, "CPT")


# ------------------------------------------------------------------
# NEEDS_MANUAL_REVIEW gate — confirmed bug, now fixed: this status
# previously fell through to the same write path as LEGAL/WARNING
# and was silently treated as ALLOWED, directly contradicting its
# own defined meaning ("cannot be determined deterministically,
# requires authorized review"). A 7h+ FDP duty reliably produces this
# status via D25 (meal/snack provision data is never populated by
# this codebase), which is what these tests use to exercise it with
# a real rule firing, not a synthetic/mocked one.
# ------------------------------------------------------------------

def test_needs_manual_review_does_not_write_and_returns_needs_review_status(_patch_engine):
    crew_id = _add_crew("CPT")
    # 7h FDP, no prior history, no other violation — the ONLY thing
    # flagged should be D25 nutrition-data-missing.
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))
    result = assignment_service.assign_crew_to_duty(crew_id, [f1], "CPT")

    assert result.status == "NEEDS_REVIEW"
    assert result.legality_status == "NEEDS_MANUAL_REVIEW"
    assert any(a.rule_code == "D25_NUTRITION_DATA_MISSING" for a in result.alerts)
    assert result.roster_ids == []

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 0  # nothing written — held, not silently allowed


def test_needs_manual_review_still_reports_computed_duty_times(_patch_engine):
    """A human reviewing a held assignment needs to see what WAS
    computed, even though nothing was saved."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))
    result = assignment_service.assign_crew_to_duty(crew_id, [f1], "CPT")

    assert result.status == "NEEDS_REVIEW"
    assert result.computed_report_time == dt.datetime(2026, 7, 20, 4, 15)
    assert result.computed_debrief_time == dt.datetime(2026, 7, 20, 11, 15)
    assert result.computed_fdp_hours == 7.0


def test_needs_manual_review_writes_audit_record_with_held_action_type(_patch_engine):
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))
    assignment_service.assign_crew_to_duty(crew_id, [f1], "CPT", app_user="tester")

    audit = _audit_rows(_patch_engine, "ASSIGNMENT_HELD_FOR_REVIEW")
    assert len(audit) == 1
    assert audit.iloc[0]["legality_result"] == "NEEDS_MANUAL_REVIEW"
    assert audit.iloc[0]["app_user"] == "tester"

    # Must NOT also appear as a normal creation or rejection record.
    assert len(_audit_rows(_patch_engine, "ASSIGNMENT_CREATED")) == 0
    assert len(_audit_rows(_patch_engine, "ASSIGNMENT_REJECTED")) == 0


def test_needs_manual_review_via_control_room_saves_neither_flight_nor_assignment(_patch_engine):
    """Same fix, Control Room path — consistent with the existing
    'no orphan flight' guarantee for ILLEGAL, now extended to
    NEEDS_MANUAL_REVIEW too."""
    crew_id = _add_crew("CPT")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))]

    result, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, flights_data, "CPT")

    assert result.status == "NEEDS_REVIEW"
    assert flight_ids == []
    assert len(flight_service.get_all_flights()) == 0

    audit = _audit_rows(_patch_engine, "ADHOC_FLIGHT_HELD_FOR_REVIEW")
    assert len(audit) == 1


def test_warning_only_status_still_allowed_and_written(_patch_engine):
    """Regression guard: WARNING must NOT be swept up into the same
    hold-for-review treatment as NEEDS_MANUAL_REVIEW — only genuine
    uncertainty gets held, not a legal-but-flagged duty. A duty just
    over 4h but at or under 6h gets the snack-required WARNING
    (D2.18_D25_SNACK_REQUIRED needs snack_provided is False
    specifically, which never fires here since it's never set to
    False — only None) — so this test instead confirms a duty with
    NO alerts at all (comfortably under every threshold) writes
    normally, as the plainest possible regression guard that the new
    branch didn't accidentally start blocking LEGAL too."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = assignment_service.assign_crew_to_duty(crew_id, [f1], "CPT")

    assert result.status == "ALLOWED"
    assert result.legality_status == "LEGAL"
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1


# ------------------------------------------------------------------
# Downstream impact detection — the actual "catch" from the spec
# ------------------------------------------------------------------

def test_adhoc_assignment_that_breaks_future_scheduled_duty_is_flagged(_patch_engine):
    """
    Crew already has a future scheduled duty (Day 3, 05:00) that is
    currently legal (nothing precedes it). Assigning them to a NEW
    ad-hoc duty (Day 2, 5h FDP, needs the 12h rest floor) that
    debriefs only 10h before the future duty's report time must flag
    a downstream conflict on that future duty.

    Deliberately kept under 6h FDP (5h, not the original 8h) so this
    stays LEGAL/ALLOWED on its own — an 8h+ duty now correctly
    triggers NEEDS_MANUAL_REVIEW (no meal/snack data), which is a
    separate, already-tested gate. The 12h rest floor applies
    regardless of duty length, so this still tests the same
    downstream-conflict mechanism with the same numbers.
    """
    crew_id = _add_crew("CPT")

    # Future scheduled duty: Day 3, 05:00 report, 3h FDP. Legal in isolation.
    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    future_result = assignment_service.assign_crew_to_duty(crew_id, [future_flight], "CPT")
    assert future_result.status == "ALLOWED"
    assert future_result.downstream_conflicts == []  # nothing before it yet

    # New ad-hoc duty: Day 2, 5h FDP (14:00-19:00), needs the 12h floor.
    # Gap to future duty's 05:00 Day 3 report = only 10h. Should conflict.
    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 18, 45))
    adhoc_result = assignment_service.assign_crew_to_duty(crew_id, [adhoc_flight], "CPT")

    assert adhoc_result.status == "ALLOWED"  # the ad-hoc duty itself is legal
    assert len(adhoc_result.downstream_conflicts) == 1
    conflict = adhoc_result.downstream_conflicts[0]
    assert conflict.flight_ids == [future_flight]
    assert conflict.role_assigned == "CPT"


def test_adhoc_assignment_with_no_downstream_impact_flags_nothing(_patch_engine):
    """Same future duty, but a SHORT ad-hoc duty (2h FDP, needs only
    the 12h floor) with enough gap must NOT flag a conflict."""
    crew_id = _add_crew("CPT")

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    assignment_service.assign_crew_to_duty(crew_id, [future_flight], "CPT")

    # 2h FDP (14:00-16:00 Day 2), needs 12h floor. Gap to Day3 05:00 = 13h. OK.
    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 15, 45))
    adhoc_result = assignment_service.assign_crew_to_duty(crew_id, [adhoc_flight], "CPT")

    assert adhoc_result.status == "ALLOWED"
    assert adhoc_result.downstream_conflicts == []


def test_downstream_conflict_includes_legal_candidates(_patch_engine):
    """When a downstream conflict is flagged, the suggested candidates
    must actually be legal for that future duty — not just any crew
    with the right role."""
    crew_a = _add_crew("CPT")
    crew_b = _add_crew("CPT")  # a second captain, free of conflicts, should qualify

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    assignment_service.assign_crew_to_duty(crew_a, [future_flight], "CPT")

    # 5h FDP, under the 6h nutrition-review threshold — see the
    # comment on test_adhoc_assignment_that_breaks_future_scheduled_duty_is_flagged
    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 18, 45))
    result = assignment_service.assign_crew_to_duty(crew_a, [adhoc_flight], "CPT")

    assert len(result.downstream_conflicts) == 1
    assert crew_b in result.downstream_conflicts[0].candidates
    assert crew_a not in result.downstream_conflicts[0].candidates  # excluded — they're the conflicted one


# ------------------------------------------------------------------
# find_legal_candidates_for_duty
# ------------------------------------------------------------------

def test_find_legal_candidates_excludes_illegal_crew(_patch_engine):
    legal_crew = _add_crew("CPT")
    illegal_crew = _add_crew("CPT")

    # illegal_crew has a heavy duty ending too close to the target —
    # seeded directly since an 8h FDP duty now correctly triggers
    # NEEDS_MANUAL_REVIEW through the real API (see _seed_duty's
    # docstring). This test is about candidate exclusion given
    # existing history, not about that gate.
    prior_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, illegal_crew, prior_flight, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))

    candidates = assignment_service.find_legal_candidates_for_duty([target_flight], "CPT")

    assert legal_crew in candidates
    assert illegal_crew not in candidates


def test_find_legal_candidates_only_matches_role(_patch_engine):
    lm_crew = _add_crew("LM")
    cpt_crew = _add_crew("CPT")

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    candidates = assignment_service.find_legal_candidates_for_duty([target_flight], "LM")

    assert lm_crew in candidates
    assert cpt_crew not in candidates


# ------------------------------------------------------------------
# FTL exemption for LM / Engr (confirmed 2026-07-21: Loadmasters and
# Engr — line-maintenance AME, not flight-deck — are not subject to
# ANO-012's FTL/rest rules at all)
# ------------------------------------------------------------------

def test_lm_assignment_allowed_despite_rest_that_would_reject_a_cpt(_patch_engine):
    """The exact scenario from test_insufficient_rest_after_prior_duty_is_rejected
    (8h FDP duty, only 5h before the next one — clearly ILLEGAL for a
    CPT under D21) must be ALLOWED for an LM, since FTL doesn't apply
    to them at all."""
    crew_id = _add_crew("LM")

    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    result1 = assignment_service.assign_crew_to_duty(crew_id, [f1], "LM")
    assert result1.status == "ALLOWED"
    assert result1.legality_status == "LEGAL"

    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    result2 = assignment_service.assign_crew_to_duty(crew_id, [f2], "LM")

    assert result2.status == "ALLOWED"
    assert result2.legality_status == "LEGAL"

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 2  # both duties actually written


def test_engr_assignment_allowed_despite_rest_that_would_reject_a_cpt(_patch_engine):
    crew_id = _add_crew("ENGR")

    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    assignment_service.assign_crew_to_duty(crew_id, [f1], "ENGR")

    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    result2 = assignment_service.assign_crew_to_duty(crew_id, [f2], "ENGR")

    assert result2.status == "ALLOWED"
    assert result2.legality_status == "LEGAL"


def test_cpt_assignment_still_rejected_for_the_same_scenario(_patch_engine):
    """Regression guard: the exemption must be scoped to LM/ENGR only
    — a CPT in the identical scenario must still be REJECTED. If this
    test ever fails, the exemption has leaked to a role it shouldn't
    apply to."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, f1, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    result2 = assignment_service.assign_crew_to_duty(crew_id, [f2], "CPT")

    assert result2.status == "REJECTED"


def test_ftl_exempt_crew_has_no_downstream_conflicts(_patch_engine):
    """The identical setup that produces a downstream conflict for a
    CPT (test_adhoc_assignment_that_breaks_future_scheduled_duty_is_flagged)
    must produce NO conflicts for an LM — there's no FTL to protect."""
    crew_id = _add_crew("LM")

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    assignment_service.assign_crew_to_duty(crew_id, [future_flight], "LM")

    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 11, 45), dt.datetime(2026, 7, 21, 18, 45))
    result = assignment_service.assign_crew_to_duty(crew_id, [adhoc_flight], "LM")

    assert result.status == "ALLOWED"
    assert result.downstream_conflicts == []


def test_find_legal_candidates_for_lm_returns_all_active_regardless_of_history(_patch_engine):
    """Complementary to the CPT version of this test — for an
    FTL-exempt role, a crew member with a heavy prior duty is still a
    valid candidate, since there's no FTL history that could exclude
    them."""
    crew_a = _add_crew("LM")
    crew_b = _add_crew("LM")

    # crew_a has a duty that would be a severe FTL conflict — for a
    # CPT this would exclude them. For LM it must not.
    prior_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    assignment_service.assign_crew_to_duty(crew_a, [prior_flight], "LM")

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    candidates = assignment_service.find_legal_candidates_for_duty([target_flight], "LM")

    assert crew_a in candidates
    assert crew_b in candidates


def test_adhoc_ftl_exempt_assignment_via_control_room_path(_patch_engine):
    """The exemption must apply identically through
    assign_crew_to_new_flights() (Control Room), not just
    assign_crew_to_duty() (Roster) — same underlying validation core,
    so this is really confirming they stayed in sync."""
    crew_id = _add_crew("LM")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))]
    result1, _ = assignment_service.assign_crew_to_new_flights(crew_id, flights_data, "LM")
    assert result1.status == "ALLOWED"

    tight_flights = [_flight_data(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))]
    result2, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, tight_flights, "LM")
    assert result2.status == "ALLOWED"
    assert len(flight_ids) == 1


# ------------------------------------------------------------------
# Crew qualification gate (2026-07-31, revised 2026-08-01) — is_active
# plus 8 expiry fields (license/medical/SIM/route-check/IR/SEP/CRM/DG).
# type_rating_expiry and contract_expiry were dropped from the crew
# schema entirely (migrations/008) — see assignment_service.py's
# QUALIFICATION_EXPIRY_FIELDS comment for why. Deliberately
# orthogonal to FTL_EXEMPT_ROLES: that set only exempts FDP/rest
# MATH, not whether the person currently holds valid documents to be
# on the roster at all.
# ------------------------------------------------------------------

def test_expired_license_is_illegal_and_blocks_save(_patch_engine):
    crew_id = _add_crew("CPT", license_expiry=dt.date(2020, 1, 1))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "REJECTED"
    assert result.legality_status == "ILLEGAL"
    assert any(a.rule_code == "AE-CREW-QUAL-001_LICENSE_EXPIRED" for a in result.alerts)
    assert len(assignment_service.get_roster_for_crew(crew_id)) == 0


def test_expired_medical_is_illegal_and_blocks_save(_patch_engine):
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2020, 1, 1))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_MEDICAL_EXPIRED" for a in result.alerts)


def test_missing_expiry_date_is_needs_review_not_silently_allowed(_patch_engine):
    """A NULL expiry is neither a silent pass nor a silent reject —
    it's an unresolved data gap that needs a human to look at it,
    same principle as the NEEDS_MANUAL_REVIEW gate this reuses."""
    crew_id = _add_crew("CPT", sim_expiry=None)
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "NEEDS_REVIEW"
    assert any(a.rule_code == "AE-CREW-QUAL-001_SIM_EXPIRY_MISSING" for a in result.alerts)
    assert len(assignment_service.get_roster_for_crew(crew_id)) == 0


def test_inactive_crew_is_illegal_and_blocks_save(_patch_engine):
    crew_id = _add_crew("CPT")
    crew_service.deactivate_crew(crew_id)
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_INACTIVE_CREW" for a in result.alerts)


def test_multiple_expired_documents_all_reported_not_just_first(_patch_engine):
    """Every failing reason is collected, not just the first one hit —
    HANDOVER.md documents first-failure-only evaluation as a real,
    already-found bug elsewhere in this file
    (_check_downstream_impact's original before/after comparison),
    not a hypothetical concern here."""
    crew_id = _add_crew(
        "CPT",
        license_expiry=dt.date(2020, 1, 1),
        medical_expiry=dt.date(2021, 1, 1),
        sep_expiry=dt.date(2019, 6, 1),
    )
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "REJECTED"
    illegal_codes = {a.rule_code for a in result.alerts if a.status == "ILLEGAL"}
    assert "AE-CREW-QUAL-001_LICENSE_EXPIRED" in illegal_codes
    assert "AE-CREW-QUAL-001_MEDICAL_EXPIRED" in illegal_codes
    assert "AE-CREW-QUAL-001_SEP_EXPIRED" in illegal_codes


def test_qualification_checked_against_duty_date_not_todays_date(_patch_engine):
    """A document that hasn't expired yet by 'today' but WILL have
    expired by the actual duty date must still be caught — checking
    against date.today() instead of the duty's own date is exactly
    the class of bug this project's hard-lessons catalogue already
    calls out."""
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2026, 7, 22))
    # Duty date 2026-07-25 is after the medical's 2026-07-22 expiry,
    # even though 2026-07-22 is still in the future relative to
    # whatever "today" is when this test actually runs.
    flight_id = _add_flight(dt.datetime(2026, 7, 25, 5, 45), dt.datetime(2026, 7, 25, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_MEDICAL_EXPIRED" for a in result.alerts)


def test_qualification_valid_on_duty_date_is_not_flagged(_patch_engine):
    """Sanity check the other direction: a document valid well past
    the duty date must not be flagged."""
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2026, 12, 31))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "ALLOWED"


def test_expiry_exactly_on_duty_date_is_expired(_patch_engine):
    """Confirmed boundary convention: a document expiring ON the duty
    date itself counts as already expired, not valid through it."""
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2026, 7, 20))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_MEDICAL_EXPIRED" for a in result.alerts)


def test_qualification_checked_against_debrief_date_not_report_date(_patch_engine):
    """Documents must stay valid through the END of the duty, not
    just at report time. Uses Air Eagle's real EPE 786/787 rotation
    timings (KHI-LHE-KHI, domestic, Mon-Fri nightly): report 18:15,
    debrief 00:00 the following day. A document expiring ON the
    debrief date (2026-07-21) must be caught as ILLEGAL even though
    it's still valid on the report date (2026-07-20) — checking only
    the report date would have incorrectly passed this."""
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2026, 7, 21))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 19, 0), dt.datetime(2026, 7, 20, 23, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assert result.computed_report_time == dt.datetime(2026, 7, 20, 18, 15)
    assert result.computed_debrief_time == dt.datetime(2026, 7, 21, 0, 0)
    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_MEDICAL_EXPIRED" for a in result.alerts)


def test_engr_still_subject_to_qualification_check_despite_ftl_exemption(_patch_engine):
    """Guard-rail: FTL_EXEMPT_ROLES must not leak into qualification
    exemption. The identical setup that's ALLOWED for an ENGR from an
    FTL standpoint (test_engr_assignment_allowed_despite_rest_that_
    would_reject_a_cpt) must still be REJECTED here, purely on
    qualification grounds."""
    crew_id = _add_crew("ENGR", license_expiry=dt.date(2020, 1, 1))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "ENGR")

    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_LICENSE_EXPIRED" for a in result.alerts)


def test_find_legal_candidates_excludes_inactive_or_expired_crew(_patch_engine):
    qualified = _add_crew("CPT")
    inactive = _add_crew("CPT")
    crew_service.deactivate_crew(inactive)
    expired_license = _add_crew("CPT", license_expiry=dt.date(2020, 1, 1))

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    candidates = assignment_service.find_legal_candidates_for_duty([target_flight], "CPT")

    assert qualified in candidates
    assert inactive not in candidates
    assert expired_license not in candidates


def test_find_legal_candidates_for_ftl_exempt_role_still_excludes_unqualified(_patch_engine):
    """The FTL-exempt trivial branch of find_legal_candidates_for_duty
    must not bypass the qualification gate either — otherwise a
    deactivated/expired-document LM or ENGR could still be suggested
    as a downstream-swap candidate."""
    qualified_lm = _add_crew("LM")
    unqualified_lm = _add_crew("LM", medical_expiry=dt.date(2020, 1, 1))

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    candidates = assignment_service.find_legal_candidates_for_duty([target_flight], "LM")

    assert qualified_lm in candidates
    assert unqualified_lm not in candidates


def test_control_room_path_also_enforces_qualification_gate(_patch_engine):
    """Same underlying validation core (_validate_new_duty) as
    assign_crew_to_duty() — confirming the two entry points stayed in
    sync for this gate too, same reasoning as the role-match and
    NEEDS_MANUAL_REVIEW parity tests above."""
    crew_id = _add_crew("CPT", license_expiry=dt.date(2020, 1, 1))
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]

    result, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, flights_data, "CPT")

    assert result.status == "REJECTED"
    assert flight_ids == []


# ------------------------------------------------------------------
# remove_assignment
# ------------------------------------------------------------------

def test_remove_assignment_cancels_not_deletes(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")

    assignment_service.remove_assignment(crew_id, flight_id, "CPT", reason="test removal")

    active = assignment_service.get_roster_for_crew(crew_id, include_cancelled=False)
    assert len(active) == 0

    everyone = assignment_service.get_roster_for_crew(crew_id, include_cancelled=True)
    assert len(everyone) == 1
    assert everyone.iloc[0]["status"] == "CANCELLED"


def test_remove_then_reassign_same_crew_flight_role_succeeds(_patch_engine):
    """Proves the Phase 3 partial-unique-index fix actually gets used
    correctly end-to-end through the service layer, not just at the
    raw SQL level."""
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")
    assignment_service.remove_assignment(crew_id, flight_id, "CPT")

    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "CPT")
    assert result.status == "ALLOWED"


def test_remove_nonexistent_assignment_raises(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    with pytest.raises(ValueError):
        assignment_service.remove_assignment(crew_id, flight_id, "CPT")


# ------------------------------------------------------------------
# assign_crew_to_new_flights — Control Room's atomic flight+assignment
# ------------------------------------------------------------------

def _flight_data(dep, arr, domestic=True, origin="KHI", destination="LHE"):
    return {
        "origin": origin, "destination": destination,
        "dep_time_planned": dep, "arr_time_planned": arr,
        "domestic": domestic,
    }


def test_legal_adhoc_assignment_creates_both_flight_and_roster_row(_patch_engine):
    crew_id = _add_crew("CPT")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]

    result, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, flights_data, "CPT")

    assert result.status == "ALLOWED"
    assert len(flight_ids) == 1
    assert flight_service.get_flight(flight_ids[0]) is not None
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1
    assert roster_df.iloc[0]["flight_id"] == flight_ids[0]


def test_illegal_adhoc_assignment_creates_no_flight_at_all(_patch_engine):
    """The actual Control Room guarantee: an illegal ad-hoc assignment
    must not leave an orphan, uncrewed flight sitting in Flight Log.
    Nothing gets saved to either table."""
    crew_id = _add_crew("CPT")

    # Prior duty requiring 16h rest after (8h FDP, D21) — seeded
    # directly (flight created normally, roster row seeded via raw
    # SQL) since an 8h duty now correctly triggers NEEDS_MANUAL_REVIEW
    # through the real assign_crew_to_new_flights() call, which has
    # its own dedicated tests.
    prior_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, prior_flight, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    flights_before = len(flight_service.get_all_flights())

    # Only 5h after prior debrief — illegal.
    illegal_flights = [_flight_data(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))]
    result, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, illegal_flights, "CPT")

    assert result.status == "REJECTED"
    assert flight_ids == []

    flights_after = len(flight_service.get_all_flights())
    assert flights_after == flights_before  # no orphan flight created


def test_illegal_adhoc_assignment_writes_audit_without_a_flight_reference(_patch_engine):
    crew_id = _add_crew("CPT")
    prior_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, prior_flight, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    illegal_flights = [_flight_data(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))]
    assignment_service.assign_crew_to_new_flights(crew_id, illegal_flights, "CPT", app_user="tester")

    audit = _audit_rows(_patch_engine, "ADHOC_FLIGHT_REJECTED")
    assert len(audit) == 1
    assert audit.iloc[0]["legality_result"] == "ILLEGAL"


def test_adhoc_assignment_also_detects_downstream_conflicts(_patch_engine):
    """The downstream check must work identically for the ad-hoc path
    — it's the same underlying mechanism, not a separate one.

    5h FDP (not the original 8h) — see the comment on
    test_adhoc_assignment_that_breaks_future_scheduled_duty_is_flagged
    for why."""
    crew_id = _add_crew("CPT")

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    assignment_service.assign_crew_to_duty(crew_id, [future_flight], "CPT")

    adhoc_flights = [_flight_data(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 18, 45))]
    result, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, adhoc_flights, "CPT")

    assert result.status == "ALLOWED"
    assert len(result.downstream_conflicts) == 1
    assert result.downstream_conflicts[0].flight_ids == [future_flight]


def test_adhoc_mixed_domestic_international_uses_international_buffer(_patch_engine):
    """Same fix as the Roster path, through Control Room's atomic
    flight+assignment — must not reject a mixed-sector rotation with
    a ValueError. Kept under 6h FDP total so this stays ALLOWED
    (proving the save actually succeeds), not just NEEDS_REVIEW."""
    crew_id = _add_crew("CPT")
    flights_data = [
        _flight_data(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                     origin="KHI", destination="LHE", domestic=True),
        _flight_data(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 9, 0),
                     origin="LHE", destination="DWC", domestic=False),
    ]
    result, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, flights_data, "CPT")
    assert result.status == "ALLOWED"
    assert len(flight_ids) == 2
