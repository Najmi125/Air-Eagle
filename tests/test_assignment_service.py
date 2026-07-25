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


def _add_crew(role="CPT", crew_id_hint=None):
    cid = crew_service.add_crew({"name": f"Test {role}", "role": role, "base": "KHI"})
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
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0))
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0))

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
    written to roster."""
    crew_id = _add_crew("CPT")

    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    result1 = assignment_service.assign_crew_to_duty(crew_id, [f1], "CPT")
    assert result1.status == "ALLOWED"  # 8h FDP duty (04:15-12:15), first duty, no prior history

    # Only 5h after debrief (12:15) — needs 16h. Should reject.
    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    result2 = assignment_service.assign_crew_to_duty(crew_id, [f2], "CPT")

    assert result2.status == "REJECTED"
    assert result2.legality_status == "ILLEGAL"

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1  # only the first duty was ever written


def test_rejected_assignment_still_writes_audit_record(_patch_engine):
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    assignment_service.assign_crew_to_duty(crew_id, [f1], "CPT")

    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    assignment_service.assign_crew_to_duty(crew_id, [f2], "CPT", app_user="tester")

    audit = _audit_rows(_patch_engine, "ASSIGNMENT_REJECTED")
    assert len(audit) == 1
    assert audit.iloc[0]["legality_result"] == "ILLEGAL"


def test_mismatched_domestic_flags_across_legs_raises(_patch_engine):
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0), domestic=True)
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0), domestic=False)

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
# Downstream impact detection — the actual "catch" from the spec
# ------------------------------------------------------------------

def test_adhoc_assignment_that_breaks_future_scheduled_duty_is_flagged(_patch_engine):
    """
    Crew already has a future scheduled duty (Day 3, 05:00) that is
    currently legal (nothing precedes it). Assigning them to a NEW
    ad-hoc duty (Day 2, 8h FDP, needs 16h rest after) that debriefs
    only 10h before the future duty's report time must flag a
    downstream conflict on that future duty.
    """
    crew_id = _add_crew("CPT")

    # Future scheduled duty: Day 3, 05:00 report, 3h FDP. Legal in isolation.
    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    future_result = assignment_service.assign_crew_to_duty(crew_id, [future_flight], "CPT")
    assert future_result.status == "ALLOWED"
    assert future_result.downstream_conflicts == []  # nothing before it yet

    # New ad-hoc duty: Day 2, 8h FDP (11:00-19:00), needs 16h rest after.
    # Gap to future duty's 05:00 Day 3 report = only 10h. Should conflict.
    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 11, 45), dt.datetime(2026, 7, 21, 18, 45))
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

    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 11, 45), dt.datetime(2026, 7, 21, 18, 45))
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

    # illegal_crew has a heavy duty ending too close to the target.
    prior_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    assignment_service.assign_crew_to_duty(illegal_crew, [prior_flight], "CPT")

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
    assignment_service.assign_crew_to_duty(crew_id, [f1], "CPT")

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

    # Prior duty requiring 16h rest after (8h FDP, D21).
    prior_flights = [_flight_data(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))]
    prior_result, _ = assignment_service.assign_crew_to_new_flights(crew_id, prior_flights, "CPT")
    assert prior_result.status == "ALLOWED"

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
    prior_flights = [_flight_data(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))]
    assignment_service.assign_crew_to_new_flights(crew_id, prior_flights, "CPT")

    illegal_flights = [_flight_data(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))]
    assignment_service.assign_crew_to_new_flights(crew_id, illegal_flights, "CPT", app_user="tester")

    audit = _audit_rows(_patch_engine, "ADHOC_FLIGHT_REJECTED")
    assert len(audit) == 1
    assert audit.iloc[0]["legality_result"] == "ILLEGAL"


def test_adhoc_assignment_also_detects_downstream_conflicts(_patch_engine):
    """The downstream check must work identically for the ad-hoc path
    — it's the same underlying mechanism, not a separate one."""
    crew_id = _add_crew("CPT")

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    assignment_service.assign_crew_to_duty(crew_id, [future_flight], "CPT")

    adhoc_flights = [_flight_data(dt.datetime(2026, 7, 21, 11, 45), dt.datetime(2026, 7, 21, 18, 45))]
    result, flight_ids = assignment_service.assign_crew_to_new_flights(crew_id, adhoc_flights, "CPT")

    assert result.status == "ALLOWED"
    assert len(result.downstream_conflicts) == 1
    assert result.downstream_conflicts[0].flight_ids == [future_flight]


def test_adhoc_mismatched_domestic_across_legs_raises(_patch_engine):
    crew_id = _add_crew("CPT")
    flights_data = [
        _flight_data(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0), domestic=True),
        _flight_data(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0), domestic=False),
    ]
    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_new_flights(crew_id, flights_data, "CPT")
