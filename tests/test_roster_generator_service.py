"""
tests/test_roster_generator_service.py

DB-integration tests for services/roster_generator_service.py — Phase
7's final piece. Grounded in the real EPE 786/787 (domestic, Mon-Fri)
and EPE 802/804/805 (international, Tue/Thu/Fri/Sat) rotations, reused
end-to-end through the real template -> approval -> generation arc
(expand_and_persist() + approve_instance(), not synthetic shortcuts).

Every seat decision here goes through the real assign_crew_to_duty()
gate — these tests verify the generator's WIRING (right pool, right
window, right idempotency, right status/visibility) and the specific,
previously-broken domestic age-ordering scenario from HANDOVER.md's
2026-08-04 entry, never re-derive FTL/pairing math themselves.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import text

import services.rotation_template_service as rts
import services.roster_generator_service as rgs
import services.assignment_service as assignment_service
import services.crew_service as crew_service
from services.assistant import reports as assistant_reports
from services.assistant.query_parser import ReportRequest

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
        effective_from=dt.date(2026, 1, 1), meal_provided=True,
        description="KHI-LHE-KHI domestic",
    )
    created = rts.expand_and_persist(rotation_code, date_from, date_to)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created


def _make_international_instances(date_from, date_to, rotation_code="EPE-802-805"):
    rts.create_template(
        rotation_code=rotation_code, days_of_week=INTERNATIONAL_DAYS, legs=INTERNATIONAL_LEGS,
        effective_from=dt.date(2026, 1, 1), meal_provided=True,
        description="KHI-LHE-DWC-KHI international",
    )
    created = rts.expand_and_persist(rotation_code, date_from, date_to)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created


def _roster_row_count(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM roster")).scalar()


# ------------------------------------------------------------------
# Basic fill + status
# ------------------------------------------------------------------

def test_generate_for_window_fills_domestic_rotation_as_proposed(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    _add_crew("CPT")
    _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    assert len(summary.filled) == 2
    assert {s.role for s in summary.filled} == {"CPT", "FO"}
    assert summary.uncovered == []
    assert summary.already_covered == []

    roster = assignment_service.search_roster(
        date_from=dt.date(2026, 8, 3), date_to=dt.date(2026, 8, 3), include_proposed=True)
    assert set(roster["status"]) == {"PROPOSED"}


def test_generate_for_window_fills_international_rotation_as_proposed(_patch_engine):
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
# domestic is legal. Both proven by the OUTCOME of real assign_crew_to_
# duty() calls, never asserted by re-deriving the rest math here.
# ------------------------------------------------------------------

def test_back_to_back_international_duty_for_the_sole_candidate_is_uncovered_not_double_booked(_patch_engine):
    """Thu 8/6 -> Fri 8/7: consecutive international operating days.
    Exactly ONE CPT candidate exists, so there is no fairness-driven
    escape route to a different pilot -- if Friday's CPT seat still
    gets filled, it can only be because the real gate allowed a second
    consecutive international duty for the same pilot; if it's
    UNCOVERED, the gate rejected it, which is what back-to-back
    international rest math requires."""
    _make_international_instances(dt.date(2026, 8, 6), dt.date(2026, 8, 7))
    _add_crew("CPT")
    _add_crew("FO")
    _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 6), dt.date(2026, 8, 7))

    thu_filled = [s for s in summary.filled if s.rotation_date == dt.date(2026, 8, 6)]
    assert {s.role for s in thu_filled} == {"CPT", "FO"}

    fri_uncovered_roles = {s.role for s in summary.uncovered if s.rotation_date == dt.date(2026, 8, 7)}
    assert "CPT" in fri_uncovered_roles  # the sole CPT candidate was rejected for Friday


def test_back_to_back_domestic_duty_for_the_sole_candidate_is_allowed(_patch_engine):
    """Mon 8/3 -> Tue 8/4, one CPT and one FO only -- if domestic
    back-to-back really is legal (the grounding fact this session
    already measured), both days must get fully covered by that same
    sole pilot; any UNCOVERED result here would mean the real gate
    rejected a domestic pairing this codebase has already confirmed
    legal."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 4))
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 4))

    assert summary.uncovered == []
    assert len(summary.filled) == 4
    assert {s.crew_id for s in summary.filled if s.role == "CPT"} == {cpt_id}
    assert {s.crew_id for s in summary.filled if s.role == "FO"} == {fo_id}


# ------------------------------------------------------------------
# Fairness (Q2/Q3): even duty counts within role, scoped to the window.
# Domestic-only week -- avoids any international rest-math uncertainty
# so the only thing under test is the fairness bookkeeping itself.
# ------------------------------------------------------------------

def test_duty_counts_stay_even_within_role_and_smaller_pool_carries_more_load(_patch_engine):
    """6 CPT / 4 FO (scaled down from the real 6/4 grounding ratio),
    one domestic rotation Mon-Fri (5 seats per role). Even counts
    within role (max-min <= 1) falls out of fewest-duty-first ordering
    alone -- no cross-role balancing exists or is expected (Q2)."""
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 7))
    cpt_ids = [_add_crew("CPT") for _ in range(6)]
    fo_ids = [_add_crew("FO") for _ in range(4)]

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 7))

    assert summary.uncovered == []
    assert len(summary.filled) == 10  # 5 rotations x 2 seats

    def _counts(crew_ids, role):
        counts = {cid: 0 for cid in crew_ids}
        for s in summary.filled:
            if s.role == role:
                counts[s.crew_id] += 1
        return counts

    cpt_counts = _counts(cpt_ids, "CPT")
    fo_counts = _counts(fo_ids, "FO")
    assert max(cpt_counts.values()) - min(cpt_counts.values()) <= 1
    assert max(fo_counts.values()) - min(fo_counts.values()) <= 1

    avg_cpt = sum(cpt_counts.values()) / len(cpt_counts)
    avg_fo = sum(fo_counts.values()) / len(fo_counts)
    assert avg_fo > avg_cpt  # smaller pool, same total demand -> more load per person


# ------------------------------------------------------------------
# Q4's corrected domestic fix, end-to-end: an entirely-65+ FO pool
# still gets a legal pairing when an eligible under-65 CPT candidate
# exists, because FO is filled BEFORE CPT (services/roster_generator_
# service.py's ROLES order, verified 2026-08-04 -- filling CPT first
# would lock onto the 67yo before the conditional fix ever sees the FO
# pool's composition, dooming the seat regardless of ordering within
# FO's own candidate list).
# ------------------------------------------------------------------

def test_domestic_seat_fully_crewed_despite_entirely_65_plus_fo_pool(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    old_cpt = _add_crew("CPT", dob=dt.date(1959, 1, 1))    # 67 in Aug 2026
    young_cpt = _add_crew("CPT", dob=dt.date(1986, 1, 1))  # 40 in Aug 2026
    _add_crew("FO", dob=dt.date(1960, 1, 1))               # 66
    _add_crew("FO", dob=dt.date(1958, 1, 1))               # 68

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    assert summary.uncovered == []
    filled_cpt = {s.crew_id for s in summary.filled if s.role == "CPT"}
    assert filled_cpt == {young_cpt}
    assert old_cpt not in filled_cpt


# ------------------------------------------------------------------
# Idempotency (Q7)
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
    assert _roster_row_count(engine) == 2  # not duplicated


def test_generate_for_window_only_fills_gaps_on_a_partially_generated_window(_patch_engine):
    instance_ids = _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 4))
    cpt_id = _add_crew("CPT")
    _add_crew("FO")

    mon_flight_ids = rts.get_promoted_flight_ids(instance_ids[0])
    manual = assignment_service.assign_crew_to_duty(
        cpt_id, mon_flight_ids, "CPT", roster_status="PLANNED")
    assert manual.status == "ALLOWED"

    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 4))

    # Monday's CPT seat was already covered (manually, as PLANNED) --
    # untouched, not re-generated as a second PROPOSED row.
    mon_already = [s for s in summary.already_covered if s.rotation_date == dt.date(2026, 8, 3)]
    assert len(mon_already) == 1
    assert mon_already[0].role == "CPT"
    assert mon_already[0].crew_id == cpt_id

    mon_filled_roles = {s.role for s in summary.filled if s.rotation_date == dt.date(2026, 8, 3)}
    assert mon_filled_roles == {"FO"}  # only the genuine gap

    tue_filled_roles = {s.role for s in summary.filled if s.rotation_date == dt.date(2026, 8, 4)}
    assert tue_filled_roles == {"CPT", "FO"}

    mon_cpt_roster = assignment_service.get_roster_for_flight(mon_flight_ids[0], include_proposed=True)
    mon_cpt_row = mon_cpt_roster[mon_cpt_roster["role_assigned"] == "CPT"].iloc[0]
    assert mon_cpt_row["status"] == "PLANNED"  # never touched/replaced


def test_uncovered_seat_is_retried_and_succeeds_once_the_blocker_is_removed(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    cpt_id = _add_crew("CPT", is_active=False)
    _add_crew("FO")

    first = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert [s.role for s in first.uncovered] == ["CPT"]  # no active CPT candidate exists

    crew_service.update_crew(cpt_id, {"is_active": True})

    second = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert second.uncovered == []
    assert {s.role for s in second.filled} == {"CPT"}
    assert second.filled[0].crew_id == cpt_id
    assert len(second.already_covered) == 1  # the FO seat, unchanged from the first run


# ------------------------------------------------------------------
# publish_window (Q5)
# ------------------------------------------------------------------

def test_publish_window_flips_only_proposed_rows_in_range(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    _make_domestic_instances(dt.date(2026, 9, 1), dt.date(2026, 9, 1), rotation_code="EPE-786-787-SEP")
    _add_crew("CPT")
    _add_crew("FO")

    rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    rgs.generate_for_window(dt.date(2026, 9, 1), dt.date(2026, 9, 1))

    published = rgs.publish_window(dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    assert published == 2

    aug_roster = assignment_service.search_roster(
        date_from=dt.date(2026, 8, 3), date_to=dt.date(2026, 8, 3), include_proposed=True)
    assert set(aug_roster["status"]) == {"PLANNED"}

    sep_roster = assignment_service.search_roster(
        date_from=dt.date(2026, 9, 1), date_to=dt.date(2026, 9, 1), include_proposed=True)
    assert set(sep_roster["status"]) == {"PROPOSED"}  # untouched -- out of range


def test_publish_window_with_nothing_proposed_in_range_returns_zero(_patch_engine):
    assert rgs.publish_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3)) == 0


# ------------------------------------------------------------------
# Visibility: PROPOSED counts as covered for OCC's own coverage report,
# but stays hidden from crew-facing reads until published -- the "crew
# sees only published" requirement, and roster_coverage()'s one
# deliberate exception to it.
# ------------------------------------------------------------------

def test_roster_coverage_shows_proposed_seat_as_covered_but_crew_read_hides_it(_patch_engine):
    _make_domestic_instances(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    cpt_id = _add_crew("CPT")
    _add_crew("FO")

    rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))

    dataset = assistant_reports.roster_coverage(
        ReportRequest(date_from=dt.date(2026, 8, 3), date_to=dt.date(2026, 8, 3)))
    assert len(dataset.rows) == 2  # EPE 786 + EPE 787
    for row in dataset.rows:
        cpt_cell, fo_cell = row[3], row[4]
        assert cpt_cell != "UNCOVERED"
        assert fo_cell != "UNCOVERED"

    assert assignment_service.get_roster_for_crew(cpt_id).empty
    assert not assignment_service.get_roster_for_crew(cpt_id, include_proposed=True).empty
