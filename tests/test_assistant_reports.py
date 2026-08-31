"""
tests/test_assistant_reports.py

DB-integration tests for services/assistant/reports.py's seven report
functions plus run_report()'s dispatcher, and boundary-value tests
that cross-check services/assistant/regulation_reference.py's stated
numbers against core/legality/pcaa_ano012_core.py's ACTUAL enforced
behavior (not just its own retyped copy of the same numbers).

Coverage note on the boundary tests: D9.1.1/D9.1.2/D9.1.3 (duty) and
D9.2.1/D9.2.2/D9.2.3 (flight time) and D21.1 (charter rest) are all
independently re-derived here against the real validator. D8.2.1 is
cross-checked against the existing get_max_fdp_minutes() boundaries
already proven in tests/test_pcaa_ano012_core.py. D23.1/D23.2/D25 are
NOT independently re-derived in this pass (would require a
significantly larger duty-sequence setup for each) — regulation_
reference.py's entries for those three rely on the plain-English
description matching the docstrings/comments at the cited line ranges,
not a boundary test. Flagged here rather than silently claimed as
covered.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import text

import services.assignment_service as assignment_service
import services.audit_service as audit_service
import services.crew_service as crew_service
import services.flight_service as flight_service
import services.assistant.reports as reports
from services.assistant import regulation_reference
from services.assistant.query_parser import ReportRequest, TEMPLATES
from core.legality.pcaa_ano012_core import (
    ANO012CoreValidator, CrewMember, Duty, Sector, DutyType,
    AlertStatus, ValidationResult,
)


@pytest.fixture(autouse=True)
def _patch_engine(_patch_all_service_engines):
    """Thin per-file wrapper — the actual patching logic lives once in
    conftest.py's _patch_all_service_engines, so no module here can be
    forgotten (see that fixture's docstring for why this matters)."""
    return _patch_all_service_engines


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


def _add_crew(role="CPT", **overrides):
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _add_flight(dep, arr, domestic=True, origin="KHI", destination="LHE", **overrides):
    data = {
        "origin": origin, "destination": destination,
        "dep_time_planned": dep, "arr_time_planned": arr,
        "domestic": domestic,
    }
    data.update(overrides)
    return flight_service.add_flight(data)


def _seed_duty(engine, crew_id, flight_id, role_assigned, report_time, debrief_time,
                fdp_hours, duty_id=None, operating_position=None):
    """Insert a roster row directly via SQL, bypassing the assignment
    API's legality gate entirely — same pattern and rationale as
    tests/test_assignment_service.py's own _seed_duty(): these tests
    are about the REPORT queries reading roster/flights/crew/audit_log
    correctly, not about re-exercising the qualification/FTL gate."""
    import uuid
    duty_id = duty_id or f"SEEDED-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                report_time, debrief_time, fdp_hours, role_assigned, operating_position)
            VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                :report_time, :debrief_time, :fdp_hours, :role_assigned, :operating_position)
        """), {
            "crew_id": crew_id, "flight_id": flight_id, "duty_id": duty_id,
            "duty_date": report_time.date(), "report_time": report_time,
            "debrief_time": debrief_time, "fdp_hours": fdp_hours,
            "role_assigned": role_assigned, "operating_position": operating_position,
        })
    return duty_id


def _resolved_request(template, **overrides):
    return ReportRequest(template=template, resolved=True, reason="ok", **overrides)


# ------------------------------------------------------------------
# crew_duty_history
# ------------------------------------------------------------------

def test_crew_duty_history_returns_sector_rows_not_deduped(_patch_engine):
    """Two sectors of ONE duty must come back as two rows sharing the
    same duty_id — this is the sector-level shape the report is
    supposed to preserve, per the explicit plan decision (2026-08-01)
    that crew_duty_history stays sector-level, not duty-level."""
    engine = _patch_engine
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 8, 0))
    f2 = _add_flight(dt.datetime(2026, 7, 20, 9, 0), dt.datetime(2026, 7, 20, 12, 0))
    report = dt.datetime(2026, 7, 20, 4, 15)
    debrief = dt.datetime(2026, 7, 20, 12, 15)
    _seed_duty(engine, crew_id, f1, "CPT", report, debrief, fdp_hours=8.0, duty_id="D-SHARED")
    _seed_duty(engine, crew_id, f2, "CPT", report, debrief, fdp_hours=8.0, duty_id="D-SHARED")

    request = _resolved_request("crew_duty_history", crew_ids=[crew_id])
    ds = reports.crew_duty_history(request)

    assert len(ds.rows) == 2
    duty_id_idx = ds.headers.index("duty_id")
    assert {row[duty_id_idx] for row in ds.rows} == {"D-SHARED"}
    assert ds.notes and "duty_id" in ds.notes[0]


def test_crew_duty_history_filters_by_crew_and_date_range(_patch_engine):
    engine = _patch_engine
    crew_a = _add_crew("CPT")
    crew_b = _add_crew("FO")
    fa = _add_flight(dt.datetime(2026, 7, 10, 5, 0), dt.datetime(2026, 7, 10, 8, 0))
    fb = _add_flight(dt.datetime(2026, 8, 10, 5, 0), dt.datetime(2026, 8, 10, 8, 0))
    _seed_duty(engine, crew_a, fa, "CPT",
               dt.datetime(2026, 7, 10, 4, 15), dt.datetime(2026, 7, 10, 8, 15), fdp_hours=4.0)
    _seed_duty(engine, crew_b, fb, "FO",
               dt.datetime(2026, 8, 10, 4, 15), dt.datetime(2026, 8, 10, 8, 15), fdp_hours=4.0)

    request = _resolved_request(
        "crew_duty_history", crew_ids=[crew_a],
        date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.crew_duty_history(request)
    crew_idx = ds.headers.index("crew_id")
    assert len(ds.rows) == 1
    assert ds.rows[0][crew_idx] == crew_a


# ------------------------------------------------------------------
# flight_records
# ------------------------------------------------------------------

def test_flight_records_includes_cancelled_by_default(_patch_engine):
    _add_flight(dt.datetime(2026, 7, 1, 5, 0), dt.datetime(2026, 7, 1, 8, 0), status="CANCELLED")
    _add_flight(dt.datetime(2026, 7, 2, 5, 0), dt.datetime(2026, 7, 2, 8, 0))

    request = _resolved_request(
        "flight_records", date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.flight_records(request)
    status_idx = ds.headers.index("status")
    assert len(ds.rows) == 2
    assert {row[status_idx] for row in ds.rows} == {"CANCELLED", "PLANNED"}


def test_flight_records_flight_no_matches_regardless_of_spacing(_patch_engine):
    """query_parser.parse_flight_no() produces 'EPE 786' (with a
    space); the DB's actual stored format wasn't confirmed at the time
    get_all_flights()'s flight_no filter was written, so the SQL
    normalizes both sides (strip spaces, uppercase) before comparing —
    this proves that normalization actually works, not just that an
    exact string match would."""
    _add_flight(dt.datetime(2026, 7, 1, 5, 0), dt.datetime(2026, 7, 1, 8, 0), flight_no="EPE786")
    _add_flight(dt.datetime(2026, 7, 2, 5, 0), dt.datetime(2026, 7, 2, 8, 0), flight_no="EPE787")

    request = _resolved_request("flight_records", flight_no="EPE 786")
    ds = reports.flight_records(request)
    assert len(ds.rows) == 1
    assert ds.rows[0][ds.headers.index("flight_no")] == "EPE786"


# ------------------------------------------------------------------
# crew_qualifications
# ------------------------------------------------------------------

def test_crew_qualifications_filters_to_expiry_window(_patch_engine):
    in_window = _add_crew("CPT", medical_expiry=dt.date(2026, 7, 15))
    out_of_window = _add_crew("FO", medical_expiry=dt.date(2027, 1, 1))

    request = _resolved_request(
        "crew_qualifications",
        date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.crew_qualifications(request)
    crew_idx = ds.headers.index("crew_id")
    ids = {row[crew_idx] for row in ds.rows}
    assert in_window in ids
    assert out_of_window not in ids
    assert ds.notes


def test_crew_qualifications_no_date_range_returns_all_active_crew(_patch_engine):
    c1 = _add_crew("CPT")
    c2 = _add_crew("FO")
    request = _resolved_request("crew_qualifications")
    ds = reports.crew_qualifications(request)
    crew_idx = ds.headers.index("crew_id")
    ids = {row[crew_idx] for row in ds.rows}
    assert {c1, c2} <= ids
    assert ds.notes == ()


# ------------------------------------------------------------------
# utilization
# ------------------------------------------------------------------

def test_utilization_totals_are_deduped_by_duty_not_sector(_patch_engine):
    """The exact Section 9 mistake, at the report layer: a 2-sector
    duty (fdp_hours=8 repeated on both rows) must total 8h, not 16h."""
    engine = _patch_engine
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0))
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0))
    report = dt.datetime(2026, 7, 20, 4, 15)
    debrief = dt.datetime(2026, 7, 20, 10, 15)
    _seed_duty(engine, crew_id, f1, "CPT", report, debrief, fdp_hours=8.0, duty_id="D-1")
    _seed_duty(engine, crew_id, f2, "CPT", report, debrief, fdp_hours=8.0, duty_id="D-1")

    request = _resolved_request("utilization", crew_ids=[crew_id])
    ds = reports.utilization(request)

    assert len(ds.rows) == 1
    row = dict(zip(ds.headers, ds.rows[0]))
    assert row["unique_duties"] == 1
    assert row["total_fdp_hours"] == 8.0


def test_utilization_peak_window_added_only_when_window_days_set(_patch_engine):
    engine = _patch_engine
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 1, 5, 0), dt.datetime(2026, 7, 1, 13, 0))
    f2 = _add_flight(dt.datetime(2026, 7, 11, 5, 0), dt.datetime(2026, 7, 11, 13, 0))
    _seed_duty(engine, crew_id, f1, "CPT",
               dt.datetime(2026, 7, 1, 5, 0), dt.datetime(2026, 7, 1, 13, 0), fdp_hours=8.0)
    _seed_duty(engine, crew_id, f2, "CPT",
               dt.datetime(2026, 7, 11, 5, 0), dt.datetime(2026, 7, 11, 13, 0), fdp_hours=8.0)

    no_window = reports.utilization(_resolved_request("utilization", crew_ids=[crew_id]))
    assert "peak_7_day_fdp_hours" not in no_window.headers

    windowed = reports.utilization(
        _resolved_request("utilization", crew_ids=[crew_id], window_days=7)
    )
    assert "peak_7_day_fdp_hours" in windowed.headers
    row = dict(zip(windowed.headers, windowed.rows[0]))
    # The two duties are 10 days apart -> no 7-day window contains both,
    # so the peak must be a single duty's 8h, not their 16h sum.
    assert row["peak_7_day_fdp_hours"] == 8.0
    assert windowed.notes


# ------------------------------------------------------------------
# roster_coverage
# ------------------------------------------------------------------

def test_roster_coverage_shows_cockpit_crew_and_free_text_occupants(_patch_engine):
    """Air Eagle's crew records are CPT/FO only (2026-08-02 operator
    decision) — LM/AME are never crew rows, so they only ever show up
    as the free text OCC typed into flights.other_occupants_operating/
    non_operating. POB must count real heads, including the "Nx ROLE"
    shorthand for more than one person in a single free-text entry."""
    engine = _patch_engine
    cpt = _add_crew("CPT")
    fo = _add_crew("FO")
    flight_id = _add_flight(
        dt.datetime(2026, 7, 5, 5, 0), dt.datetime(2026, 7, 5, 8, 0),
        origin="KHI", destination="LHE", flight_no="EPE 786",
        other_occupants_operating="Abdulghani (LM), 2x AME",
        other_occupants_non_operating="Client rep",
        remarks="VIP aboard",
    )
    _seed_duty(engine, cpt, flight_id, "CPT",
               dt.datetime(2026, 7, 5, 4, 15), dt.datetime(2026, 7, 5, 8, 15), fdp_hours=4.0,
               operating_position="COMMANDER")
    _seed_duty(engine, fo, flight_id, "FO",
               dt.datetime(2026, 7, 5, 4, 15), dt.datetime(2026, 7, 5, 8, 15), fdp_hours=4.0,
               operating_position="SECOND_PILOT")

    request = _resolved_request(
        "roster_coverage", date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.roster_coverage(request)
    assert len(ds.rows) == 1
    row = dict(zip(ds.headers, ds.rows[0]))

    assert row["Date"] == dt.date(2026, 7, 5)
    assert row["Flight"] == "EPE 786"
    assert row["Route"] == "KHI-LHE"
    assert row["Commander"] == cpt
    assert row["Second Pilot"] == fo
    assert row["Other occupants — operating"] == "Abdulghani (LM), 2x AME"
    assert row["Other occupants — non-operating"] == "Client rep"
    assert row["Remarks"] == "VIP aboard"
    # 2 cockpit crew + Abdulghani (1) + 2x AME (2) + Client rep (1) = 6
    assert row["POB"] == 6
    assert ds.notes
    assert not any("uncovered cockpit seat" in note for note in ds.notes)


def test_roster_coverage_marks_uncovered_only_for_missing_cockpit_seat(_patch_engine):
    """Occupant columns must never trigger UNCOVERED — only an unfilled
    cockpit SEAT does."""
    engine = _patch_engine
    cpt = _add_crew("CPT")
    flight_id = _add_flight(
        dt.datetime(2026, 7, 5, 5, 0), dt.datetime(2026, 7, 5, 8, 0),
        other_occupants_operating="2x AME",
    )
    _seed_duty(engine, cpt, flight_id, "CPT",
               dt.datetime(2026, 7, 5, 4, 15), dt.datetime(2026, 7, 5, 8, 15), fdp_hours=4.0,
               operating_position="COMMANDER")
    # Second Pilot seat deliberately left unassigned.

    request = _resolved_request(
        "roster_coverage", date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.roster_coverage(request)
    row = dict(zip(ds.headers, ds.rows[0]))
    assert row["Commander"] == cpt
    assert row["Second Pilot"] == "UNCOVERED"
    assert row["POB"] == 3  # 1 cockpit crew (CPT only) + 2x AME
    assert any("uncovered cockpit seat" in note for note in ds.notes)


def test_roster_coverage_reports_a_cpt_cpt_pair_by_seat_not_grade(_patch_engine):
    """The production defect, reproduced (2026-08-31, flights 15/16):
    a fully-crewed flight whose Second Pilot happens to be CPT-graded
    rendered as TWO Commanders and an UNCOVERED Second Pilot, because
    the split was on role_assigned.

    The database makes the wrong reading impossible on its own terms —
    uq_roster_flight_operating_position_active (migrations/016) forbids
    two active COMMANDER rows on one flight — so a report showing two
    is reporting something that cannot exist. Under the flight-deck
    pair model a CPT in the Second Pilot seat is ordinary, not an
    anomaly."""
    engine = _patch_engine
    commander = _add_crew("CPT")
    second_pilot = _add_crew("CPT")
    flight_id = _add_flight(
        dt.datetime(2026, 7, 5, 5, 0), dt.datetime(2026, 7, 5, 8, 0),
        origin="LHE", destination="KHI", flight_no="EPE 787",
    )
    _seed_duty(engine, commander, flight_id, "CPT",
               dt.datetime(2026, 7, 5, 4, 15), dt.datetime(2026, 7, 5, 8, 15), fdp_hours=4.0,
               operating_position="COMMANDER")
    _seed_duty(engine, second_pilot, flight_id, "CPT",
               dt.datetime(2026, 7, 5, 4, 15), dt.datetime(2026, 7, 5, 8, 15), fdp_hours=4.0,
               operating_position="SECOND_PILOT")

    request = _resolved_request(
        "roster_coverage", date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.roster_coverage(request)
    row = dict(zip(ds.headers, ds.rows[0]))

    assert row["Commander"] == commander
    assert row["Second Pilot"] == second_pilot
    # The whole point: a fully-crewed flight must not read as half-empty.
    assert "UNCOVERED" not in (row["Commander"], row["Second Pilot"])
    assert not any("uncovered cockpit seat" in note for note in ds.notes)
    assert row["POB"] == 2


def test_roster_coverage_names_cockpit_crew_whose_seat_is_not_recorded(_patch_engine):
    """A cockpit row with no operating_position belongs to no seat, and
    must not silently disappear — that would under-report who is
    aboard, which is worse than the bug this replaced.

    Impossible to create through any code path since migration 016, and
    there are zero such rows in production (checked 2026-08-28), so
    this is defensive: pre-016 rows would otherwise vanish."""
    engine = _patch_engine
    commander = _add_crew("CPT")
    stranded = _add_crew("FO")
    flight_id = _add_flight(
        dt.datetime(2026, 7, 5, 5, 0), dt.datetime(2026, 7, 5, 8, 0),
        flight_no="EPE 786",
    )
    _seed_duty(engine, commander, flight_id, "CPT",
               dt.datetime(2026, 7, 5, 4, 15), dt.datetime(2026, 7, 5, 8, 15), fdp_hours=4.0,
               operating_position="COMMANDER")
    _seed_duty(engine, stranded, flight_id, "FO",
               dt.datetime(2026, 7, 5, 4, 15), dt.datetime(2026, 7, 5, 8, 15), fdp_hours=4.0,
               operating_position=None)

    request = _resolved_request(
        "roster_coverage", date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.roster_coverage(request)
    row = dict(zip(ds.headers, ds.rows[0]))

    # Named, not placed: claiming a seat the data does not record would
    # be a different kind of wrong.
    assert stranded not in row["Commander"]
    assert stranded not in row["Second Pilot"]
    assert any(reports.SEAT_NOT_RECORDED in note and stranded in note for note in ds.notes)
    # Aboard, so counted — and the seat they did not fill is still
    # reported as uncovered rather than quietly covered by them.
    assert row["POB"] == 2
    assert row["Second Pilot"] == "UNCOVERED"
    assert any("uncovered cockpit seat" in note for note in ds.notes)


def test_roster_coverage_excludes_cancelled_flights(_patch_engine):
    _add_flight(dt.datetime(2026, 7, 5, 5, 0), dt.datetime(2026, 7, 5, 8, 0), status="CANCELLED")
    request = _resolved_request(
        "roster_coverage", date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 31),
    )
    ds = reports.roster_coverage(request)
    assert ds.rows == ()


# ------------------------------------------------------------------
# audit_compliance
# ------------------------------------------------------------------

def test_audit_compliance_includes_only_compliance_action_types(_patch_engine):
    crew_id = _add_crew("CPT")
    audit_service.log_audit(
        action_type="ASSIGNMENT_REJECTED", affected_crew=crew_id,
        warning_or_failure_reason="D9.1.3 breach",
    )
    audit_service.log_audit(action_type="CREW_ADDED", affected_crew=crew_id)

    request = _resolved_request(
        "audit_compliance",
        date_from=dt.date.today() - dt.timedelta(days=1),
        date_to=dt.date.today() + dt.timedelta(days=1),
    )
    ds = reports.audit_compliance(request)
    action_idx = ds.headers.index("action_type")
    actions = {row[action_idx] for row in ds.rows}
    assert actions == {"ASSIGNMENT_REJECTED"}


def test_audit_compliance_filters_by_crew_id(_patch_engine):
    crew_a = _add_crew("CPT")
    crew_b = _add_crew("FO")
    audit_service.log_audit(action_type="ASSIGNMENT_REJECTED", affected_crew=crew_a)
    audit_service.log_audit(action_type="ASSIGNMENT_REJECTED", affected_crew=crew_b)

    request = _resolved_request("audit_compliance", crew_ids=[crew_a])
    ds = reports.audit_compliance(request)
    assert len(ds.rows) == 1
    assert ds.rows[0][ds.headers.index("affected_crew")] == crew_a


# ------------------------------------------------------------------
# regulation
# ------------------------------------------------------------------

def test_regulation_looks_up_section_from_question_text():
    request = _resolved_request("regulation")
    ds = reports.regulation(request, question="What does D21.1 say about rest?")
    assert ds.rows[0][0] == "D21.1"


def test_regulation_unknown_section_returns_explanatory_note():
    request = _resolved_request("regulation")
    ds = reports.regulation(request, question="What about D99.9?")
    assert ds.rows == ()
    assert ds.notes and "D99.9" in ds.notes[0]


def test_regulation_no_section_in_question_returns_explanatory_note():
    request = _resolved_request("regulation")
    ds = reports.regulation(request, question="what does the regulation say about rest?")
    assert ds.rows == ()
    assert "No D-section reference" in ds.notes[0]


# ------------------------------------------------------------------
# run_report() dispatcher
# ------------------------------------------------------------------

def test_run_report_rejects_unresolved_request():
    request = ReportRequest(resolved=False, reason="ambiguous between templates")
    with pytest.raises(ValueError):
        reports.run_report(request)


def test_run_report_rejects_unknown_template():
    request = ReportRequest(template="not_a_real_template", resolved=True, reason="ok")
    with pytest.raises(ValueError):
        reports.run_report(request)


def test_run_report_routes_regulation_with_question(_patch_engine):
    request = _resolved_request("regulation")
    ds = reports.run_report(request, question="D9.1.3 limit?")
    assert ds.rows[0][0] == "D9.1.3"


def test_run_report_routes_every_non_regulation_template_without_error(_patch_engine):
    """Every template other than 'regulation' must run end-to-end on
    an empty database without raising — an empty Dataset, not an
    exception, is the correct response to 'no matching records'."""
    for template in TEMPLATES:
        if template == "regulation":
            continue
        request = _resolved_request(template)
        ds = reports.run_report(request)
        assert ds.rows == ()


def test_report_functions_cover_every_template():
    """Direct regression test for the SSOT assertion at import time in
    services/assistant/reports.py — if this ever fails, that assertion
    should already have failed first, but this pins the expectation as
    an explicit test rather than only an import-time side effect."""
    assert set(reports.REPORT_FUNCTIONS) | {"regulation"} == set(TEMPLATES)


# ------------------------------------------------------------------
# Boundary tests: services/assistant/regulation_reference.py's stated
# numbers vs core/legality/pcaa_ano012_core.py's ACTUAL enforced
# behavior. Pure logic (validator methods called directly) — included
# in this DB-integration file rather than a separate one because they
# exist specifically to backstop this feature's regulation() report,
# not as general pcaa_ano012_core.py coverage (that's test_pcaa_ano012_core.py's job).
# ------------------------------------------------------------------

def _isolated_duty(duty_span: dt.timedelta, flight_span: dt.timedelta) -> Duty:
    """One Duty for exercising _check_cumulative_limits() directly
    (bypassing validate_schedule()'s other checks entirely, which is
    deliberate — these tests isolate exactly one numeric threshold at
    a time, not full-schedule realism)."""
    start = dt.datetime(2026, 1, 1, 0, 0)
    return Duty(
        duty_type=DutyType.FDP, start_utc=start, end_utc=start + duty_span,
        crew_id="CPT-01", duty_id="D-1",
        sectors=[Sector(departure_utc=start, arrival_utc=start + flight_span)],
    )


@pytest.mark.parametrize("rule_code, threshold_hours, kind", [
    ("D9.1.1_7_DAY_DUTY_LIMIT", 60, "duty"),
    ("D9.1.2_14_DAY_DUTY_LIMIT", 110, "duty"),
    ("D9.1.3_28_DAY_DUTY_LIMIT", 190, "duty"),
    ("D9.2.1_7_DAY_FLIGHT_TIME_LIMIT", 35, "flight"),
    ("D9.2.2_30_DAY_FLIGHT_TIME_LIMIT", 100, "flight"),
    ("D9.2.3_12_MONTH_FLIGHT_TIME_LIMIT", 1000, "flight"),
])
def test_d9_cumulative_limit_boundary_matches_regulation_reference(rule_code, threshold_hours, kind):
    section = rule_code.split("_", 1)[0]
    entry = regulation_reference.lookup(section)
    assert entry is not None
    assert str(threshold_hours) in entry.summary

    small = dt.timedelta(hours=1)
    at_limit_span = dt.timedelta(hours=threshold_hours)
    over_limit_span = dt.timedelta(hours=threshold_hours, minutes=1)

    if kind == "duty":
        # duty_span drives duty_7/14/28 directly; flight_span kept
        # small so no D9.2.x check is incidentally exercised.
        at_limit_duty = _isolated_duty(at_limit_span, small)
        over_limit_duty = _isolated_duty(over_limit_span, small)
    else:
        # flight_span must equal duty_span here: the cumulative-limit
        # window is anchored at duty.end_utc, and _overlap_minutes()
        # clips a sector's contribution to that window — a short duty
        # wrapped around a long sector would silently truncate the
        # flight-time total instead of measuring the real threshold.
        at_limit_duty = _isolated_duty(at_limit_span, at_limit_span)
        over_limit_duty = _isolated_duty(over_limit_span, over_limit_span)

    validator = ANO012CoreValidator()

    at_result = ValidationResult()
    validator._check_cumulative_limits([at_limit_duty], at_result)
    assert not any(a.rule_code == rule_code for a in at_result.alerts), (
        f"{rule_code} fired exactly AT the limit ({threshold_hours}h) — "
        "the check must be strictly-greater-than, not at-or-over."
    )

    over_result = ValidationResult()
    validator._check_cumulative_limits([over_limit_duty], over_result)
    matching = [a for a in over_result.alerts if a.rule_code == rule_code]
    assert len(matching) == 1
    assert matching[0].status == AlertStatus.ILLEGAL


def test_d21_1_charter_rest_matches_regulation_reference():
    entry = regulation_reference.lookup("D21.1")
    assert "12" in entry.summary and "twice" in entry.summary.lower()

    validator = ANO012CoreValidator()
    crew = CrewMember(crew_id="CPT-01", name="Test Captain", home_base="KHI")
    start = dt.datetime(2026, 1, 1, 0, 0)
    prev_duty = Duty(duty_type=DutyType.FDP, start_utc=start,
                      end_utc=start + dt.timedelta(hours=8), crew_id="CPT-01", duty_id="D-1")
    next_start = prev_duty.end_utc + dt.timedelta(hours=16)  # exactly 2x the 8h prior FDP
    next_duty = Duty(duty_type=DutyType.FDP, start_utc=next_start,
                      end_utc=next_start + dt.timedelta(hours=4), crew_id="CPT-01", duty_id="D-2")

    required_minutes, rule_code = validator.required_rest_minutes(prev_duty, next_duty, crew)
    assert rule_code == "D21.1_CHARTER_AERIAL_WORK_ABOVE_5700KG_REST"
    assert required_minutes == 16 * 60  # 2x8h prior FDP, above the 12h floor


def test_d8_2_1_fdp_table_bands_match_regulation_reference():
    entry = regulation_reference.lookup("D8.2.1")
    for band in ("13h00", "12h00", "11h00"):
        assert band in entry.summary

    validator = ANO012CoreValidator()
    from core.legality.pcaa_ano012_core import AcclimatizationState

    minutes_0600, _ = validator.get_max_fdp_minutes(
        duty_start_utc=dt.datetime(2026, 7, 20, 5, 0), report_tz_offset=5.0,
        sectors=1, acclimatization_state=AcclimatizationState.B,
    )
    assert minutes_0600 == 13 * 60

    minutes_overnight, _ = validator.get_max_fdp_minutes(
        duty_start_utc=dt.datetime(2026, 7, 20, 20, 0), report_tz_offset=5.0,
        sectors=1, acclimatization_state=AcclimatizationState.B,
    )
    assert minutes_overnight == 11 * 60
