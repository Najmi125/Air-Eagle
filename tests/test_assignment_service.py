"""
tests/test_assignment_service.py

Every "illegal" scenario here is built from the actual D21 charter
rest formula (max(12h, 2xFDP)), already verified independently in
tests/test_pcaa_ano012_core.py — not just asserted to be illegal by
fiat. If D21's numbers ever change, these scenarios need re-deriving,
which is the point: they're tied to the real rule, not a guess.

Rebuilt for the flight-deck crew package (2026-08-13): CPT/FO can no
longer be assigned solo — assign_crew_to_duty()/assign_crew_to_
new_flights() now raise ValueError for pilots (see their own
docstrings), and a fresh pair must go through assign_pair_to_duty()/
assign_pair_to_new_flights() instead, validated and committed
together. Most of the FDP/rest/qualification/downstream-conflict
tests in this file were never really ABOUT pairing — "CPT" was just a
convenient FTL-subject role — so _assign_pilot()/_assign_pilot_adhoc()
below auto-pair the subject with a disposable, always-otherwise-legal
partner and expose that subject's own slice of the pair result in the
same shape the old single-pilot AssignmentResult had, so the bulk of
these tests keep reading exactly as before. Tests actually ABOUT the
pairing/composition rules themselves call assign_pair_to_duty()/
validate_pair() directly. LM/ENGR are unaffected by any of this (no
seat, no partner requirement) and are untouched throughout.
"""
import sys
from pathlib import Path
import datetime as dt
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pandas as pd
from sqlalchemy import text

import services.assignment_service as assignment_service
import services.crew_service as crew_service
import services.flight_service as flight_service
import services.audit_service as audit_service


@pytest.fixture(autouse=True)
def _patch_engine(_patch_all_service_engines):
    """Thin per-file wrapper — the actual patching logic lives once in
    conftest.py's _patch_all_service_engines, so no module here can be
    forgotten (see that fixture's docstring for why this matters)."""
    return _patch_all_service_engines


# A safely-future date for every qualification expiry field, so a
# crew member created via _add_crew() is fully qualified by default.
# Existing tests in this file are about FDP/rest/role logic, not
# qualifications — without this default, EVERY one of them would
# start failing with NEEDS_REVIEW (missing expiry date) the moment
# the qualification gate (2026-07-31) was added, which would silently
# swap what these tests are actually verifying. Tests that need to
# exercise the qualification gate itself override individual fields.
_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
# date_of_birth default added 2026-08-02 for the same reason, when
# Step 7's age-pairing rule (AE-CREW-PAIR-AGE-001) landed: without
# this, every _add_crew()'d pilot has a NULL DOB, so any test pairing
# a CPT and an FO via the real assignment API gets AE-CREW-PAIR-AGE-
# 001_DOB_MISSING instead of whatever that test actually meant to
# exercise. 1980-01-01 is comfortably under 65 for every date used
# anywhere in this file's flight scenarios. Tests that need to
# exercise the age-pairing rule itself override date_of_birth
# explicitly, same pattern as the qualification gate above.
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY,
    "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY,
    "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY,
    "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY,
    "dg_expiry": _FAR_FUTURE_EXPIRY,
    "date_of_birth": dt.date(1980, 1, 1),
}


def _add_crew(role="CPT", crew_id_hint=None, **overrides):
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    cid = crew_service.add_crew(crew_data)
    return cid


def _add_flight(dep, arr, domestic=True, origin="KHI", destination="LHE", rotation_instance_id=None):
    data = {
        "origin": origin, "destination": destination,
        "dep_time_planned": dep, "arr_time_planned": arr,
        "domestic": domestic,
    }
    if rotation_instance_id is not None:
        data["rotation_instance_id"] = rotation_instance_id
    return flight_service.add_flight(data)


def _audit_rows(engine, action_type=None):
    q = "SELECT * FROM audit_log"
    params = {}
    if action_type:
        q += " WHERE action_type = :at"
        params["at"] = action_type
    return pd.read_sql(text(q), engine, params=params)


def _seed_duty(engine, crew_id, flight_id, role_assigned, report_time, debrief_time, fdp_hours,
                operating_position=None):
    """
    Insert a roster row directly via SQL, bypassing the real
    assignment API entirely.

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

    operating_position (2026-08-13, flight-deck crew package): NULL by
    default — the FDP engine only cares about crew_id/duty timing,
    never the seat, so most callers don't need it. A few
    find_legal_candidates_for_seat()/age-pairing tests DO need a real
    seat occupant seeded directly rather than through assign_pair_
    to_duty() — e.g. a pair where one pilot has a missing date_of_birth
    can never be constructed through the real pair-assignment API at
    all (it's held NEEDS_MANUAL_REVIEW and writes nothing for either
    seat), but the age-pairing DB lookup (_find_paired_pilot()) just
    reads an ACTIVE roster row's operating_position directly and
    doesn't care how it got there.
    """
    import uuid
    duty_id = f"SEEDED-{uuid.uuid4().hex[:8]}"
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


def _seed_many_duties(engine, crew_id, role, start_date, count, duty_hours=24, spacing_days=3):
    """
    Seeds `count` historical duties via _add_flight() + _seed_duty()
    (both existing helpers above, untouched) — bypasses the
    assignment API entirely, same reason as every other _seed_duty()
    use in this file. Each duty needs its own distinct flight_id: the
    active partial unique index on (crew_id, flight_id, role_assigned)
    (migrations/005_roster_partial_unique_index.sql) only allows one
    ACTIVE assignment per (flight, role) at a time.

    Duties are spaced `spacing_days` apart (default 3, not 1) and
    each duty's report_time/debrief_time span is exactly duty_hours.
    Deliberately NOT consecutive calendar dates: with spacing_days=3,
    no duty-date streak ever exceeds 1, so D23.2_SEVENTH_DAY_OFF never
    fires, and duty-days per calendar month stay well under 20, so
    D23.1_MANDATORY_5_DAYS_OFF's `represented_days < 20` skip also
    keeps it from firing — both are schedule-level checks unrelated
    to what a caller seeding "many historical duties" is usually
    trying to set up, and either firing would land an unwanted
    ILLEGAL alert in schedule_level_alerts, contaminating a
    blocked_by_history_only=True scenario. The underlying flight's
    dep/arr exactly matches report/debrief (no buffer), so
    flight_time == duty_time for D9.2.x purposes too — callers
    choosing count/duty_hours should keep the total under 1000h to
    avoid also tripping D9.2.3 (1000h/365-day) alongside D9.1.3
    (190h/28-day).
    """
    duty_ids = []
    for i in range(count):
        report = start_date + dt.timedelta(days=i * spacing_days)
        debrief = report + dt.timedelta(hours=duty_hours)
        flight_id = _add_flight(report, debrief)
        duty_ids.append(_seed_duty(engine, crew_id, flight_id, role, report, debrief,
                                    fdp_hours=float(duty_hours)))
    return duty_ids


# ------------------------------------------------------------------
# Pair-assignment helpers — pilots can no longer be assigned solo
# (see this file's module docstring). These wrap assign_pair_to_duty()
# / assign_pair_to_new_flights() with a disposable, always-otherwise-
# legal partner and re-expose the SUBJECT pilot's own slice of the
# result in the shape the old single-pilot AssignmentResult had, so
# tests that were never really about pairing (FDP/rest, qualification
# gate, downstream conflicts, delay recompute, alert summarization)
# don't need hand-rewriting one at a time. `status`/`legality_status`
# below intentionally come from the OVERALL pair outcome — correct
# here because the disposable partner is always otherwise-legal, so
# the pair's overall outcome and the subject seat's own outcome
# coincide in every test that uses these helpers. A test that
# genuinely needs to tell the two apart calls assign_pair_to_duty()/
# validate_pair() directly instead (see the dedicated pairing section
# below).
# ------------------------------------------------------------------

def _fresh_partner(role, dob=None):
    return _add_crew(role, date_of_birth=dob or dt.date(1985, 1, 1))


def _subject_slice(pair_result, is_commander):
    v = pair_result.validation
    if is_commander:
        return SimpleNamespace(
            status=pair_result.status,
            legality_status=v.commander_status,
            alerts=v.commander_alerts,
            alert_summary=v.commander_alert_summary,
            roster_ids=pair_result.commander_roster_ids,
            duty_id=pair_result.commander_duty_id,
            downstream_conflicts=pair_result.commander_downstream_conflicts,
            computed_report_time=v.commander_computed_report_time,
            computed_debrief_time=v.commander_computed_debrief_time,
            computed_fdp_hours=v.commander_computed_fdp_hours,
        )
    return SimpleNamespace(
        status=pair_result.status,
        legality_status=v.second_pilot_status,
        alerts=v.second_pilot_alerts,
        alert_summary=v.second_pilot_alert_summary,
        roster_ids=pair_result.second_pilot_roster_ids,
        duty_id=pair_result.second_pilot_duty_id,
        downstream_conflicts=pair_result.second_pilot_downstream_conflicts,
        computed_report_time=v.second_pilot_computed_report_time,
        computed_debrief_time=v.second_pilot_computed_debrief_time,
        computed_fdp_hours=v.second_pilot_computed_fdp_hours,
    )


def _assign_pilot(role, flight_ids, crew_id=None, partner_id=None, dob=None,
                   partner_role=None, app_user=None, roster_status="PLANNED"):
    """Assigns ONE pilot (role 'CPT' or 'FO') to flight_ids through
    assign_pair_to_duty(), auto-creating a disposable partner for the
    other seat unless partner_id is given. Returns a SimpleNamespace
    (see _subject_slice()) for the subject's own outcome, plus
    .crew_id/.partner_id/.pair_result for tests that need the raw
    pair result too."""
    if crew_id is None:
        crew_id = _add_crew(role, date_of_birth=dob or dt.date(1980, 1, 1))
    if partner_role is None:
        partner_role = "FO" if role == "CPT" else "CPT"
    if partner_id is None:
        partner_id = _fresh_partner(partner_role)

    is_commander = role == "CPT"
    commander_id = crew_id if is_commander else partner_id
    second_pilot_id = partner_id if is_commander else crew_id

    pair_result = assignment_service.assign_pair_to_duty(
        commander_id, second_pilot_id, flight_ids, app_user=app_user, roster_status=roster_status)
    ns = _subject_slice(pair_result, is_commander)
    ns.crew_id = crew_id
    ns.partner_id = partner_id
    ns.pair_result = pair_result
    return ns


def _flight_data(dep, arr, domestic=True, origin="KHI", destination="LHE"):
    return {
        "origin": origin, "destination": destination,
        "dep_time_planned": dep, "arr_time_planned": arr,
        "domestic": domestic,
    }


def _assign_pilot_adhoc(role, flights_data, crew_id=None, partner_id=None,
                         partner_role=None, app_user=None):
    """Ad-hoc (Control Room) counterpart to _assign_pilot(), via
    assign_pair_to_new_flights(). Returns (namespace, flight_ids)."""
    if crew_id is None:
        crew_id = _add_crew(role, date_of_birth=dt.date(1980, 1, 1))
    if partner_role is None:
        partner_role = "FO" if role == "CPT" else "CPT"
    if partner_id is None:
        partner_id = _fresh_partner(partner_role)

    is_commander = role == "CPT"
    commander_id = crew_id if is_commander else partner_id
    second_pilot_id = partner_id if is_commander else crew_id

    pair_result, flight_ids = assignment_service.assign_pair_to_new_flights(
        commander_id, second_pilot_id, flights_data, app_user=app_user)
    ns = _subject_slice(pair_result, is_commander)
    ns.crew_id = crew_id
    ns.partner_id = partner_id
    ns.pair_result = pair_result
    return ns, flight_ids


def _seed_seat_occupant(engine, crew_id, flight_ids, role_assigned, operating_position):
    """Seeds a real, ACTIVE occupant of one seat directly via SQL (one
    row per flight_id, sharing one duty_id) — the only reliable way to
    put a "one seat real, other empty" state on the roster for a
    candidate-search test. assign_pair_to_duty() can't build this
    state: it only ever writes both seats together or neither, and a
    pair including a pilot with a missing date_of_birth is held
    NEEDS_MANUAL_REVIEW (writes nothing at all) — so a real occupant
    with a DOB gap, specifically, can ONLY be constructed this way.
    report_time/debrief_time are set to the flights' own span exactly
    (no buffer) — fine here since these tests assert on the SEARCHED
    candidate's outcome, never on this seeded occupant's own FDP
    numbers."""
    import uuid
    duty_id = f"SEEDED-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        for fid in flight_ids:
            flight = flight_service.get_flight(fid)
            conn.execute(text("""
                INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                    report_time, debrief_time, fdp_hours, role_assigned, operating_position)
                VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                    :report_time, :debrief_time, :fdp_hours, :role_assigned, :operating_position)
            """), {
                "crew_id": crew_id, "flight_id": fid, "duty_id": duty_id,
                "duty_date": flight["dep_time_planned"].date(),
                "report_time": flight["dep_time_planned"], "debrief_time": flight["arr_time_planned"],
                "fdp_hours": 2.0, "role_assigned": role_assigned, "operating_position": operating_position,
            })
    return duty_id


def _seed_heavy_history_and_assign_far_future(engine, crew_id):
    """
    Shared setup for the alert-summarization tests below: 40 historical
    duties (spacing_days=3, duty_hours=24 -> ~216h in any dense 28-day
    window, comfortably over D9.1.3's 190h limit; 960h total, safely
    under D9.2.3's 1000h/365-day limit) followed by one new short
    domestic assignment 45 days after the last seeded duty ends — far
    enough that the new duty's OWN 7/14/28-day windows are clean, but
    still inside LOOKBACK_DAYS=370 so the historical breaches are
    still loaded and re-evaluated (the actual mechanism behind the
    2,215-alert scenario this feature fixes).
    """
    start = dt.datetime(2026, 1, 1, 6, 0)
    _seed_many_duties(engine, crew_id, "CPT", start, count=40, duty_hours=24, spacing_days=3)

    last_debrief = start + dt.timedelta(days=39 * 3, hours=24)
    new_dep = last_debrief + dt.timedelta(days=45)
    new_arr = new_dep + dt.timedelta(hours=2)
    flight_id = _add_flight(new_dep, new_arr)
    return _assign_pilot("CPT", [flight_id], crew_id=crew_id)


def _seed_rotation_instance(engine, rotation_code="TEST-ROT"):
    """A minimal real rotation_instances row (migrations/011) for the
    one test below that needs a genuinely rotation-linked flight
    (uncovered_seats is scoped to rotation_instance_id) — raw SQL,
    same "seed given state directly" pattern as _seed_duty()."""
    with engine.begin() as conn:
        template_id = conn.execute(text("""
            INSERT INTO rotation_templates (rotation_code, days_of_week, effective_from)
            VALUES (:code, ARRAY[1,2,3,4,5,6,7]::smallint[], :eff)
            RETURNING id
        """), {"code": rotation_code, "eff": dt.date(2026, 1, 1)}).scalar()
        instance_id = conn.execute(text("""
            INSERT INTO rotation_instances (template_id, rotation_code, version, rotation_date, status)
            VALUES (:template_id, :code, 1, :rdate, 'APPROVED')
            RETURNING id
        """), {"template_id": template_id, "code": rotation_code, "rdate": dt.date(2026, 7, 20)}).scalar()
    return instance_id


# ------------------------------------------------------------------
# Age-pairing rule (AE-CREW-PAIR-AGE-001) — pure math, no DB needed
# ------------------------------------------------------------------

def test_age_on_boundary_turning_65_today_counts_as_65():
    """Exactly 65 does not count as below 65 either way (settled
    wording) — turning 65 ON the reference date already counts."""
    assert assignment_service.age_on(dt.date(1961, 8, 2), dt.date(2026, 8, 2)) == 65


def test_age_on_day_before_65th_birthday_is_still_64():
    assert assignment_service.age_on(dt.date(1961, 8, 2), dt.date(2026, 8, 1)) == 64


def test_age_on_day_after_reference_before_birthday_is_64():
    assert assignment_service.age_on(dt.date(1961, 8, 3), dt.date(2026, 8, 2)) == 64


@pytest.mark.parametrize("age_a, age_b, domestic, expected_illegal", [
    (65, 70, True, True),    # domestic: both 65+ -> illegal
    (65, 64, True, False),   # domestic: one under 65 -> legal
    (60, 60, True, False),   # domestic: both under 65 -> legal
    (64, 64, False, False),  # international: both under 65 -> legal
    (65, 40, False, True),   # international: one 65+ -> illegal
    (70, 70, False, True),   # international: both 65+ -> illegal
])
def test_evaluate_pair_age_matches_settled_wording(age_a, age_b, domestic, expected_illegal):
    assert assignment_service._evaluate_pair_age(age_a, age_b, domestic) == expected_illegal


def test_pairing_constraint_message_domestic_says_other_seat_must_be_under_65():
    message = assignment_service._pairing_constraint_message("CPT-01", 67, domestic=True)
    assert "CPT-01" in message and "67" in message
    assert "under 65" in message.lower()


def test_pairing_constraint_message_international_says_no_valid_partner_exists():
    """The real, sharper insight for international: once this pilot is
    65+, the pair is already illegal by 'illegal if EITHER is 65+'
    regardless of who's assigned to the other seat — there is no
    partner age that fixes it, unlike domestic."""
    message = assignment_service._pairing_constraint_message("CPT-01", 67, domestic=False)
    assert "no" in message.lower() and "second pilot" in message.lower()


# ------------------------------------------------------------------
# Flight-deck pair composition and validation — validate_pair() /
# assign_pair_to_duty() directly. This is the section that actually
# replaced the old "first pilot alone, pairing_pending" model: a fresh
# pair can no longer be built one seat at a time, so every scenario
# here is expressed as two candidates validated/committed together.
# ------------------------------------------------------------------

def _dob_for_age(age, reference_date=dt.date(2026, 7, 20)):
    """A date_of_birth that makes the crew member exactly `age` on
    reference_date — matches the reference_date used by the flights
    below (2026-07-20)."""
    return dt.date(reference_date.year - age, reference_date.month, reference_date.day)


def test_validate_pair_writes_nothing(_patch_engine):
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(50))
    fo = _add_crew("FO", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.validate_pair(cpt, fo, [flight_id])

    assert result.status == "LEGAL"
    assert len(assignment_service.get_roster_for_crew(cpt)) == 0
    assert len(assignment_service.get_roster_for_crew(fo)) == 0


def test_validate_pair_on_an_already_written_pair_does_not_double_count_itself_as_history(_patch_engine):
    """Real bug, found via the operator's own real-Postgres run of
    publish_window()'s per-rotation re-validation (2026-08-14): calling
    validate_pair() again for a pair that's ALREADY been written (the
    exact scenario publish_window() needs — re-validate a PROPOSED
    pair fresh before flipping it to PLANNED) used to see each pilot's
    own already-committed row for these same flight_ids as "existing
    history" alongside the freshly-built candidate duty being validated
    — two duties covering the identical report/debrief window, which
    the FDP/rest validator correctly (from ITS perspective) flagged as
    a zero-rest violation. _validate_new_duty() now excludes any
    existing duty whose flight_ids exactly match the one being
    validated — real history stays intact, but a duty can't collide
    with its own already-written self."""
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(50))
    fo = _add_crew("FO", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    written = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])
    assert written.status == "ALLOWED"

    revalidated = assignment_service.validate_pair(cpt, fo, [flight_id])

    assert revalidated.status == "LEGAL"
    assert not any(a.rule_code.startswith("D21") for a in revalidated.commander_alerts)
    assert not any(a.rule_code.startswith("D21") for a in revalidated.second_pilot_alerts)


def test_validate_pair_domestic_both_65_plus_is_illegal(_patch_engine):
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(70))
    fo = _add_crew("FO", date_of_birth=_dob_for_age(65))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45), domestic=True)

    result = assignment_service.validate_pair(cpt, fo, [flight_id])

    assert result.status == "ILLEGAL"
    assert any(a.rule_code == "AE-CREW-PAIR-AGE-001_AGE_LIMIT" for a in result.pair_alerts)


def test_validate_pair_domestic_one_under_65_is_legal(_patch_engine):
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(70))
    fo = _add_crew("FO", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45), domestic=True)

    result = assignment_service.validate_pair(cpt, fo, [flight_id])

    assert result.status == "LEGAL"
    assert not any(a.rule_code.startswith("AE-CREW-PAIR-AGE-001") for a in result.pair_alerts)


def test_validate_pair_international_one_65_plus_is_illegal(_patch_engine):
    """International is stricter: EITHER pilot 65+ is illegal, even
    though the same composition would be fine domestically (only one
    under 65)."""
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(70))
    fo = _add_crew("FO", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45), domestic=False)

    result = assignment_service.validate_pair(cpt, fo, [flight_id])

    assert result.status == "ILLEGAL"
    assert any(a.rule_code == "AE-CREW-PAIR-AGE-001_AGE_LIMIT" for a in result.pair_alerts)


def test_validate_pair_missing_dob_needs_review(_patch_engine):
    cpt = _add_crew("CPT", date_of_birth=None)
    fo = _add_crew("FO", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.validate_pair(cpt, fo, [flight_id])

    assert result.status == "NEEDS_MANUAL_REVIEW"
    assert any(a.rule_code == "AE-CREW-PAIR-AGE-001_DOB_MISSING" for a in result.pair_alerts)


def test_assign_pair_to_duty_illegal_writes_neither_seat(_patch_engine):
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(70))
    fo = _add_crew("FO", date_of_birth=_dob_for_age(65))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45), domestic=True)

    result = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])

    assert result.status == "REJECTED"
    assert len(assignment_service.get_roster_for_crew(cpt)) == 0
    assert len(assignment_service.get_roster_for_crew(fo)) == 0


def test_assign_pair_to_duty_needs_review_writes_neither_seat(_patch_engine):
    cpt = _add_crew("CPT", date_of_birth=None)
    fo = _add_crew("FO", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])

    assert result.status == "NEEDS_REVIEW"
    assert len(assignment_service.get_roster_for_crew(cpt)) == 0
    assert len(assignment_service.get_roster_for_crew(fo)) == 0


def test_assign_pair_to_duty_allowed_writes_both_seats(_patch_engine):
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(50))
    fo = _add_crew("FO", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])

    assert result.status == "ALLOWED"
    cpt_roster = assignment_service.get_roster_for_crew(cpt)
    fo_roster = assignment_service.get_roster_for_crew(fo)
    assert len(cpt_roster) == 1 and cpt_roster.iloc[0]["operating_position"] == "COMMANDER"
    assert len(fo_roster) == 1 and fo_roster.iloc[0]["operating_position"] == "SECOND_PILOT"


def test_commander_must_be_cpt_graded(_patch_engine):
    fo_as_commander = _add_crew("FO")
    other_fo = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    with pytest.raises(ValueError):
        assignment_service.assign_pair_to_duty(fo_as_commander, other_fo, [flight_id])


def test_second_pilot_may_be_cpt_graded_not_only_fo(_patch_engine):
    """The operator-confirmed composition rule: any current Captain
    may fly Second Pilot. A CPT+CPT pair must pass through exactly the
    same age-pairing math a CPT+FO pair always did — not a special
    case, not an exemption."""
    commander = _add_crew("CPT", date_of_birth=_dob_for_age(50))
    second_pilot = _add_crew("CPT", date_of_birth=_dob_for_age(40))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_pair_to_duty(commander, second_pilot, [flight_id])

    assert result.status == "ALLOWED"
    second_pilot_roster = assignment_service.get_roster_for_crew(second_pilot)
    assert second_pilot_roster.iloc[0]["operating_position"] == "SECOND_PILOT"
    assert second_pilot_roster.iloc[0]["role_assigned"] == "CPT"


def test_cpt_cpt_pair_subject_to_same_age_rule_as_cpt_fo(_patch_engine):
    commander = _add_crew("CPT", date_of_birth=_dob_for_age(70))
    second_pilot = _add_crew("CPT", date_of_birth=_dob_for_age(65))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45), domestic=True)

    result = assignment_service.validate_pair(commander, second_pilot, [flight_id])

    assert result.status == "ILLEGAL"
    assert any(a.rule_code == "AE-CREW-PAIR-AGE-001_AGE_LIMIT" for a in result.pair_alerts)


def test_same_crew_id_for_both_seats_raises(_patch_engine):
    cpt = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    with pytest.raises(ValueError):
        assignment_service.assign_pair_to_duty(cpt, cpt, [flight_id])


def test_unknown_commander_crew_id_raises(_patch_engine):
    fo = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    with pytest.raises(ValueError):
        assignment_service.assign_pair_to_duty("NO-SUCH-CREW", fo, [flight_id])


def test_unknown_second_pilot_crew_id_raises(_patch_engine):
    cpt = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    with pytest.raises(ValueError):
        assignment_service.assign_pair_to_duty(cpt, "NO-SUCH-CREW", [flight_id])


def test_lm_ame_assignment_never_triggers_pairing_check(_patch_engine):
    """LM/AME are irrelevant to this rule entirely — assigning one
    (via the unaffected assign_crew_to_duty() path, operating_position
    stays None) must never touch age-pairing at all."""
    lm = _add_crew("LM", date_of_birth=_dob_for_age(70))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = assignment_service.assign_crew_to_duty(lm, [flight_id], "LM")

    assert result.status == "ALLOWED"
    assert not any(a.rule_code.startswith("AE-CREW-PAIR-AGE-001") for a in result.alerts)


def test_fill_remaining_seat_after_manual_unassign_uses_current_partner(_patch_engine):
    """A real pair, then one seat manually vacated (remove_assignment_
    from_duty()) and refilled with a NEW pilot via assign_crew_to_duty()
    (the "fill the remaining seat of an already-real pair" case) — the
    pairing check must evaluate against whoever is CURRENTLY active on
    the other seat, using their REAL age, not skip the check or use a
    stale/cached value.

    cpt is 65+ throughout. fo_first (under 65) makes the INITIAL pair
    legal, so it can actually get written. After fo_first is removed
    and fo_second (also 65+) is refilled against the still-active cpt,
    the pair must be REJECTED (domestic: illegal once BOTH are 65+) —
    proving the refill's pairing check found the real, current cpt
    (not e.g. silently treating the seat as still-unpaired and letting
    a genuinely illegal pairing through)."""
    cpt = _add_crew("CPT", date_of_birth=_dob_for_age(70))
    fo_first = _add_crew("FO", date_of_birth=_dob_for_age(40))
    fo_second = _add_crew("FO", date_of_birth=_dob_for_age(67))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45), domestic=True)

    first = assignment_service.assign_pair_to_duty(cpt, fo_first, [flight_id])
    assert first.status == "ALLOWED"
    assignment_service.remove_assignment_from_duty(fo_first, first.second_pilot_duty_id)

    second = assignment_service.assign_crew_to_duty(fo_second, [flight_id], "FO", operating_position="SECOND_PILOT")

    assert second.status == "REJECTED"
    assert second.paired_crew_id == cpt
    assert len(assignment_service.get_roster_for_crew(fo_second)) == 0


def test_assign_crew_to_duty_rejects_pilot_with_no_operating_position(_patch_engine):
    cpt = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_duty(cpt, [flight_id], "CPT")


def test_assign_crew_to_duty_rejects_pilot_with_no_real_partner(_patch_engine):
    """operating_position given, but nobody real holds the other seat
    yet — must NOT silently proceed as a solo commit; that's exactly
    the defect this piece closes."""
    cpt = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_duty(cpt, [flight_id], "CPT", operating_position="COMMANDER")
    assert len(assignment_service.get_roster_for_crew(cpt)) == 0


def test_assign_crew_to_new_flights_rejects_pilots_outright(_patch_engine):
    """No 'fill the remaining seat' exception here — the flights don't
    exist yet, so a real partner could never already be on the other
    seat."""
    cpt = _add_crew("CPT")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]
    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_new_flights(cpt, flights_data, "CPT")


# ------------------------------------------------------------------
# Immediate legality gate
# ------------------------------------------------------------------

def test_legal_assignment_is_allowed_and_written(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

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

    result = _assign_pilot("CPT", [f1, f2], crew_id=crew_id)

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
    # correctly triggers NEEDS_MANUAL_REVIEW through the real
    # assignment path (meal/snack data is never populated), which has
    # its own dedicated tests. This test is about what a SECOND,
    # shorter assignment does given such a duty already exists in
    # history — not about re-testing that gate.
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, f1, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    # Only 5h after debrief (12:15) — needs 16h. Should reject.
    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    result2 = _assign_pilot("CPT", [f2], crew_id=crew_id)

    assert result2.status == "REJECTED"
    assert result2.legality_status == "ILLEGAL"

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1  # only the seeded prior duty, nothing for the rejected one


def test_rejected_pair_still_writes_audit_record(_patch_engine):
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, f1, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    _assign_pilot("CPT", [f2], crew_id=crew_id, app_user="tester")

    audit = _audit_rows(_patch_engine, "PAIR_ASSIGNMENT_REJECTED")
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

    This particular duty is 9.5h — under D25's 6h threshold it would
    have needed real meal data to avoid NEEDS_MANUAL_REVIEW; since
    migrations/014 (2026-08-08), flights.meal_provided defaults TRUE,
    so this is correctly ALLOWED and written. The buffer calculation
    is the real point of this test, checked against the written roster
    row directly."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                      origin="KHI", destination="LHE", domestic=True)
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0),
                      origin="LHE", destination="DWC", domestic=False)
    f3 = _add_flight(dt.datetime(2026, 7, 20, 11, 0), dt.datetime(2026, 7, 20, 13, 0),
                      origin="DWC", destination="KHI", domestic=False)

    result = _assign_pilot("CPT", [f1, f2, f3], crew_id=crew_id)

    assert result.status == "ALLOWED"
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 3  # one row per sector (003_roster_table.sql)
    assert roster_df["duty_id"].nunique() == 1  # all 3 sector rows are ONE duty
    # report_time = first dep (05:00) - 60min (international buffer,
    # NOT domestic's 45min, since one sector is international)
    assert set(roster_df["report_time"]) == {dt.datetime(2026, 7, 20, 4, 0)}
    # debrief_time = last arr (13:00) + 30min (international, not 15)
    assert set(roster_df["debrief_time"]) == {dt.datetime(2026, 7, 20, 13, 30)}


def test_all_domestic_duty_still_uses_domestic_buffer(_patch_engine):
    """Sanity check the other direction — a duty where every sector
    is genuinely domestic must still get the domestic (45/15) buffer,
    not be pushed to international by the fix above."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                      origin="KHI", destination="LHE", domestic=True)
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0),
                      origin="LHE", destination="KHI", domestic=True)

    _assign_pilot("CPT", [f1, f2], crew_id=crew_id)
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
        _assign_pilot("CPT", [f1, f2], crew_id=crew_id)


def test_unknown_flight_id_raises(_patch_engine):
    with pytest.raises(ValueError):
        _assign_pilot("CPT", [999999])


def test_unknown_crew_id_raises(_patch_engine):
    fo = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0))
    with pytest.raises(ValueError):
        assignment_service.assign_pair_to_duty("NO-SUCH-CREW", fo, [flight_id])


# ------------------------------------------------------------------
# Role-match enforcement — the actual fix for a confirmed critical
# bypass: role_assigned was never cross-checked against the crew
# member's real registered role, while the FTL exemption decision
# correctly used the real role. Exercised here via LM/ENGR (and a CPT
# attempting to be filed as LM) rather than CPT-as-role_assigned
# scenarios, since assign_crew_to_duty()/assign_crew_to_new_flights()
# now reject CPT/FO outright before ever reaching the role-match
# check — a real crew-graded-CPT-filed-as-something-else scenario
# exercises the identical role_assigned != crew_row['role'] check in
# _validate_new_duty() without tripping that separate, earlier gate.
# ------------------------------------------------------------------

def test_role_assigned_must_match_crew_actual_role(_patch_engine):
    """A CPT-graded crew member must NOT be assignable with
    role_assigned='LM' — role_assigned is cross-checked against the
    crew member's real registered role, not taken on faith."""
    cpt_crew = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_duty(cpt_crew, [flight_id], "LM")


def test_role_assigned_matching_real_role_succeeds(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)
    assert result.status == "ALLOWED"


def test_role_match_recognizes_ame_engr_synonym(_patch_engine):
    """A crew member registered with role 'AME' (stored as canonical
    'ENGR') must still be assignable with role_assigned='AME' — the
    synonym must be recognized on the comparison side too, not just
    at storage time."""
    crew_id = _add_crew("AME")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "AME")
    assert result.status == "ALLOWED"


def test_role_match_is_case_insensitive(_patch_engine):
    """Exercised via LM, not CPT — a pilot's role_assigned is no
    longer a caller-supplied string on the pair-assignment path (it's
    hardcoded 'CPT'/the crew's own real grade in assign_pair_to_duty()),
    so case-insensitivity for a pilot grade isn't a reachable scenario
    through the pair API any more. LM/ENGR still take role_assigned as
    a caller-supplied string via assign_crew_to_duty(), so this is
    still real, live-tested behavior."""
    crew_id = _add_crew("LM")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = assignment_service.assign_crew_to_duty(crew_id, [flight_id], "lm")
    assert result.status == "ALLOWED"


def test_role_mismatch_via_control_room_path_also_rejected(_patch_engine):
    """The same enforcement must apply through
    assign_crew_to_new_flights() (Control Room) — both paths share
    _validate_new_duty(), so this is really confirming they stayed
    in sync."""
    cpt_crew = _add_crew("CPT")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]

    with pytest.raises(ValueError):
        assignment_service.assign_crew_to_new_flights(cpt_crew, flights_data, "LM")


# ------------------------------------------------------------------
# NEEDS_MANUAL_REVIEW gate — confirmed bug, now fixed: this status
# previously fell through to the same write path as LEGAL/WARNING
# and was silently treated as ALLOWED, directly contradicting its
# own defined meaning ("cannot be determined deterministically,
# requires authorized review"). These tests exercise it with a real
# rule firing, not a synthetic/mocked one — a crew member missing a
# qualification-expiry field (AE-CREW-QUAL-001_LICENSE_EXPIRY_MISSING,
# _check_crew_qualifications()), not a duty's own duration.
# ------------------------------------------------------------------

def test_needs_manual_review_does_not_write_and_returns_needs_review_status(_patch_engine):
    # Missing license_expiry, no other violation — the ONLY thing
    # flagged should be the qualification-data-missing gate.
    crew_id = _add_crew("CPT", license_expiry=None)
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))
    result = _assign_pilot("CPT", [f1], crew_id=crew_id)

    assert result.status == "NEEDS_REVIEW"
    assert result.legality_status == "NEEDS_MANUAL_REVIEW"
    assert any(a.rule_code == "AE-CREW-QUAL-001_LICENSE_EXPIRY_MISSING" for a in result.alerts)
    assert result.roster_ids == []

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 0  # nothing written — held, not silently allowed


def test_needs_manual_review_still_reports_computed_duty_times(_patch_engine):
    """A human reviewing a held assignment needs to see what WAS
    computed, even though nothing was saved."""
    crew_id = _add_crew("CPT", license_expiry=None)
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))
    result = _assign_pilot("CPT", [f1], crew_id=crew_id)

    assert result.status == "NEEDS_REVIEW"
    assert result.computed_report_time == dt.datetime(2026, 7, 20, 4, 15)
    assert result.computed_debrief_time == dt.datetime(2026, 7, 20, 11, 15)
    assert result.computed_fdp_hours == 7.0


def test_needs_manual_review_writes_audit_record_with_held_action_type(_patch_engine):
    crew_id = _add_crew("CPT", license_expiry=None)
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))
    _assign_pilot("CPT", [f1], crew_id=crew_id, app_user="tester")

    audit = _audit_rows(_patch_engine, "PAIR_ASSIGNMENT_HELD_FOR_REVIEW")
    assert len(audit) == 1
    assert audit.iloc[0]["legality_result"] == "NEEDS_MANUAL_REVIEW"
    assert audit.iloc[0]["app_user"] == "tester"

    # Must NOT also appear as a normal creation or rejection record.
    assert len(_audit_rows(_patch_engine, "ASSIGNMENT_CREATED")) == 0
    assert len(_audit_rows(_patch_engine, "PAIR_ASSIGNMENT_REJECTED")) == 0


def test_needs_manual_review_via_control_room_saves_neither_flight_nor_assignment(_patch_engine):
    """Same fix, Control Room path — consistent with the existing
    'no orphan flight' guarantee for ILLEGAL, now extended to
    NEEDS_MANUAL_REVIEW too, and to neither seat of the pair."""
    crew_id = _add_crew("CPT", license_expiry=None)
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 11, 0))]

    result, flight_ids = _assign_pilot_adhoc("CPT", flights_data, crew_id=crew_id)

    assert result.status == "NEEDS_REVIEW"
    assert flight_ids == []
    assert len(flight_service.get_all_flights()) == 0

    audit = _audit_rows(_patch_engine, "ADHOC_PAIR_HELD_FOR_REVIEW")
    assert len(audit) == 1


def test_warning_only_status_still_allowed_and_written(_patch_engine):
    """Regression guard: WARNING must NOT be swept up into the same
    hold-for-review treatment as NEEDS_MANUAL_REVIEW — only genuine
    uncertainty gets held, not a legal-but-flagged duty. This test
    confirms a duty with NO alerts at all (comfortably under every
    threshold) writes normally, as the plainest possible regression
    guard that the new branch didn't accidentally start blocking LEGAL
    too. See test_snack_not_provided_produces_warning_but_still_allowed
    below for the real D2.18_D25_SNACK_REQUIRED WARNING firing and
    still writing — the case this test deliberately stays clear of."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = _assign_pilot("CPT", [f1], crew_id=crew_id)

    assert result.status == "ALLOWED"
    assert result.legality_status == "LEGAL"
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1


def test_snack_not_provided_produces_warning_but_still_allowed(_patch_engine):
    """The real D2.18_D25_SNACK_REQUIRED rule, fired for real
    (migrations/015, 2026-08-08 — snack_provided is now real data, not
    a permanent None): a domestic duty just over 4h but at or under 6h
    (so D25's meal-nutrition threshold doesn't also fire) with
    snack_provided=False on its flight must produce a WARNING, not
    block the write — WARNING is a legal-but-flagged duty, not genuine
    uncertainty, so it must NOT get swept into the NEEDS_MANUAL_REVIEW
    hold-for-review branch."""
    crew_id = _add_crew("CPT")
    # 05:00-09:15 -> report 04:15, debrief 09:30 (domestic +15min),
    # FDP 5.25h -- over D2.18's 4h snack threshold, under D25's 6h
    # meal threshold.
    flight_id = flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 0),
        "arr_time_planned": dt.datetime(2026, 7, 20, 9, 15),
        "domestic": True, "snack_provided": False,
    })

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assert result.status == "ALLOWED"
    assert result.legality_status == "WARNING"
    assert any(a.rule_code == "D2.18_D25_SNACK_REQUIRED" for a in result.alerts)
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1  # WARNING still writes, unlike NEEDS_MANUAL_REVIEW


def test_pair_crash_before_audit_write_rolls_back_both_seats(_patch_engine, monkeypatch):
    """Step 6 (2026-08-02) regression test, now proven at pair scale:
    both pilots' roster rows and their audit records are written in
    ONE transaction. Forces the SECOND pilot's own audit write to fail
    and confirms BOTH pilots' roster inserts (not just the one that
    failed) are rolled back — the stronger, pair-specific version of
    the same "no orphan committed row with no audit trail" guarantee."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    def _failing_log_audit(*args, **kwargs):
        raise RuntimeError("simulated crash before the second pilot's audit write")

    monkeypatch.setattr(assignment_service, "log_audit", _failing_log_audit)

    with pytest.raises(RuntimeError):
        _assign_pilot("CPT", [f1], crew_id=crew_id)

    assert len(assignment_service.get_roster_for_crew(crew_id)) == 0


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

    Kept at 5h FDP (not 8h) so this stays LEGAL/ALLOWED on its own,
    since the 12h rest floor applies regardless of duty length anyway.
    """
    crew_id = _add_crew("CPT")

    # Future scheduled duty: Day 3, 05:00 report, 3h FDP. Legal in isolation.
    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    future_result = _assign_pilot("CPT", [future_flight], crew_id=crew_id)
    assert future_result.status == "ALLOWED"
    assert future_result.downstream_conflicts == []  # nothing before it yet

    # New ad-hoc duty: Day 2, 5h FDP (14:00-19:00), needs the 12h floor.
    # Gap to future duty's 05:00 Day 3 report = only 10h. Should conflict.
    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 18, 45))
    adhoc_result = _assign_pilot("CPT", [adhoc_flight], crew_id=crew_id)

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
    _assign_pilot("CPT", [future_flight], crew_id=crew_id)

    # 2h FDP (14:00-16:00 Day 2), needs 12h floor. Gap to Day3 05:00 = 13h. OK.
    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 15, 45))
    adhoc_result = _assign_pilot("CPT", [adhoc_flight], crew_id=crew_id)

    assert adhoc_result.status == "ALLOWED"
    assert adhoc_result.downstream_conflicts == []


def test_downstream_conflict_includes_legal_candidates(_patch_engine):
    """When a downstream conflict is flagged, the suggested candidates
    must actually be legal for that future duty — not just any crew
    with the right grade. Candidates come back as CandidateStatus
    objects filtered to LEGAL/WARNING crew_ids (2026-08-12,
    find_legal_candidates_for_seat())."""
    crew_a = _add_crew("CPT")
    crew_b = _add_crew("CPT")  # a second captain, free of conflicts, should qualify

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    _assign_pilot("CPT", [future_flight], crew_id=crew_a)

    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 18, 45))
    result = _assign_pilot("CPT", [adhoc_flight], crew_id=crew_a)

    assert len(result.downstream_conflicts) == 1
    assert crew_b in result.downstream_conflicts[0].candidates
    assert crew_a not in result.downstream_conflicts[0].candidates  # excluded — they're the conflicted one


# ------------------------------------------------------------------
# find_legal_candidates_for_seat (replaces find_legal_candidates_
# for_duty, 2026-08-12) — returns CandidateStatus objects now, not a
# bare List[str]. _legal_ids() below extracts the "selectable" subset
# (LEGAL + WARNING) matching the old function's inclusion bar, for
# tests whose intent hasn't changed; tests specifically about the
# NEEDS_MANUAL_REVIEW-vs-legal distinction this piece fixed check
# .status directly instead.
# ------------------------------------------------------------------

def _legal_ids(candidates):
    return [c.crew_id for c in candidates if c.status in ("LEGAL", "WARNING")]


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

    candidates = assignment_service.find_legal_candidates_for_seat(
        [target_flight], "CPT", operating_position="COMMANDER")
    ids = _legal_ids(candidates)

    assert legal_crew in ids
    assert illegal_crew not in ids
    illegal_status = next(c for c in candidates if c.crew_id == illegal_crew)
    assert illegal_status.status == "ILLEGAL"


def test_find_legal_candidates_only_matches_seat_eligible_grades(_patch_engine):
    lm_crew = _add_crew("LM")
    cpt_crew = _add_crew("CPT")

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    candidates = assignment_service.find_legal_candidates_for_seat([target_flight], "LM")

    ids = [c.crew_id for c in candidates]
    assert lm_crew in ids
    assert cpt_crew not in ids


def test_find_legal_candidates_second_pilot_seat_includes_both_cpt_and_fo(_patch_engine):
    """The real composition rule as data: SEAT_ELIGIBLE_GRADES['SECOND_
    PILOT'] is {'CPT', 'FO'} — a Second Pilot seat search must return
    BOTH grades as real candidates, not just FO."""
    cpt_candidate = _add_crew("CPT")
    fo_candidate = _add_crew("FO")
    lm_crew = _add_crew("LM")

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    candidates = assignment_service.find_legal_candidates_for_seat(
        [target_flight], "CPT", operating_position="SECOND_PILOT")
    ids = _legal_ids(candidates)

    assert cpt_candidate in ids
    assert fo_candidate in ids
    assert lm_crew not in ids


# ------------------------------------------------------------------
# find_legal_candidates_for_seat x age-pairing (AE-CREW-PAIR-AGE-001)
# — a candidate is only illegal RELATIVE to whoever really holds the
# other seat of the target duty. The real occupant is seeded directly
# via _seed_seat_occupant() (SQL, bypassing the assignment API) rather
# than through assign_pair_to_duty() — a genuinely age- or DOB-
# incomplete pair would itself be rejected/held by that API and write
# nothing, which is the opposite of what these tests need to set up.
# ------------------------------------------------------------------

def test_find_legal_candidates_excludes_domestic_age_illegal_pairing(_patch_engine):
    """Real Second Pilot occupant is 67. A 67yo Commander candidate
    would pair BOTH 65+ (domestic: illegal only if both are) --
    excluded. A young Commander candidate pairs fine -- included."""
    old_candidate = _add_crew("CPT", date_of_birth=dt.date(1959, 1, 1))
    young_candidate = _add_crew("CPT", date_of_birth=dt.date(1986, 1, 1))  # 40

    target_flight = _add_flight(dt.datetime(2026, 8, 3, 5, 45), dt.datetime(2026, 8, 3, 7, 45))
    fo_old = _add_crew("FO", date_of_birth=dt.date(1959, 1, 1))  # 67 in Aug 2026
    _seed_seat_occupant(_patch_engine, fo_old, [target_flight], "FO", "SECOND_PILOT")

    candidates = assignment_service.find_legal_candidates_for_seat(
        [target_flight], "CPT", operating_position="COMMANDER")
    ids = _legal_ids(candidates)

    assert old_candidate not in ids
    assert young_candidate in ids


def test_find_legal_candidates_excludes_international_age_illegal_pairing(_patch_engine):
    """International: illegal if EITHER pilot is 65+ -- even a YOUNG
    real Second Pilot occupant doesn't save a 65+ Commander candidate,
    unlike domestic."""
    old_candidate = _add_crew("CPT", date_of_birth=dt.date(1959, 1, 1))  # 67
    young_candidate = _add_crew("CPT", date_of_birth=dt.date(1990, 1, 1))

    target_flight = _add_flight(dt.datetime(2026, 8, 3, 1, 45), dt.datetime(2026, 8, 3, 3, 30),
                                 origin="KHI", destination="LHE", domestic=False)
    fo_young = _add_crew("FO", date_of_birth=dt.date(1986, 1, 1))  # 40
    _seed_seat_occupant(_patch_engine, fo_young, [target_flight], "FO", "SECOND_PILOT")

    candidates = assignment_service.find_legal_candidates_for_seat(
        [target_flight], "CPT", operating_position="COMMANDER")
    ids = _legal_ids(candidates)

    assert old_candidate not in ids
    assert young_candidate in ids


def test_find_legal_candidates_includes_65_plus_when_other_seat_uncovered(_patch_engine):
    """No real occupant on the other seat at all -- _check_crew_pairing_age()
    returns 'pending' with no alert (the same false-alarm avoidance the
    real assignment gate already gets), so a 65+ candidate is NOT
    excluded just because nobody else is assigned yet."""
    old_candidate = _add_crew("CPT", date_of_birth=dt.date(1959, 1, 1))  # 67

    target_flight = _add_flight(dt.datetime(2026, 8, 3, 5, 45), dt.datetime(2026, 8, 3, 7, 45))
    candidates = assignment_service.find_legal_candidates_for_seat(
        [target_flight], "CPT", operating_position="COMMANDER")
    ids = _legal_ids(candidates)

    assert old_candidate in ids


def test_find_legal_candidates_excludes_candidate_when_other_seats_dob_missing(_patch_engine):
    """The real other-seat occupant has no recorded date_of_birth --
    AE-CREW-PAIR-AGE-001_DOB_MISSING (NEEDS_MANUAL_REVIEW). This is
    the exact defect find_legal_candidates_for_seat() (2026-08-12)
    fixes over the old bare-List[str] find_legal_candidates_for_duty():
    a NEEDS_MANUAL_REVIEW candidate must NOT be indistinguishable from
    a genuinely LEGAL one — it's correctly excluded from the
    LEGAL/WARNING selectable set, but still returned (not silently
    dropped), carrying its real reason so a caller can show it
    separately rather than mislabel it "legal"."""
    candidate = _add_crew("CPT", date_of_birth=dt.date(1986, 1, 1))

    target_flight = _add_flight(dt.datetime(2026, 8, 3, 5, 45), dt.datetime(2026, 8, 3, 7, 45))
    fo_unknown_dob = _add_crew("FO", date_of_birth=None)
    _seed_seat_occupant(_patch_engine, fo_unknown_dob, [target_flight], "FO", "SECOND_PILOT")

    candidates = assignment_service.find_legal_candidates_for_seat(
        [target_flight], "CPT", operating_position="COMMANDER")
    ids = _legal_ids(candidates)

    assert candidate not in ids  # excluded from the selectable set

    candidate_status = next(c for c in candidates if c.crew_id == candidate)
    assert candidate_status.status == "NEEDS_MANUAL_REVIEW"
    assert candidate_status.blocking_reasons  # a real reason, not silently empty
    assert "DOB" in candidate_status.blocking_reasons[0] or "date of birth" in candidate_status.blocking_reasons[0].lower()


def test_downstream_conflict_candidates_exclude_age_illegal_pairing(_patch_engine):
    """End-to-end through the real caller (_check_downstream_impact()),
    not just find_legal_candidates_for_seat() in isolation: a
    downstream-conflict suggestion list must not include a candidate
    who'd be age-illegal with the future duty's real other-seat
    occupant."""
    crew_a = _add_crew("CPT", date_of_birth=dt.date(1980, 1, 1))  # the one whose ad-hoc duty causes the conflict
    fo_old = _add_crew("FO", date_of_birth=dt.date(1959, 1, 1))  # 67
    legal_candidate = _add_crew("CPT", date_of_birth=dt.date(1986, 1, 1))  # 40
    age_illegal_candidate = _add_crew("CPT", date_of_birth=dt.date(1959, 1, 1))  # 67

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    future_pair = assignment_service.assign_pair_to_duty(crew_a, fo_old, [future_flight])
    assert future_pair.status == "ALLOWED"

    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 18, 45))
    result = _assign_pilot("CPT", [adhoc_flight], crew_id=crew_a)

    assert len(result.downstream_conflicts) == 1
    candidates = result.downstream_conflicts[0].candidates
    assert legal_candidate in candidates
    assert age_illegal_candidate not in candidates  # both 65+ with fo_old, domestic
    assert crew_a not in candidates  # excluded — they're the conflicted one


# ------------------------------------------------------------------
# FTL exemption for LM / Engr (confirmed 2026-07-21: Loadmasters and
# Engr — line-maintenance AME, not flight-deck — are not subject to
# ANO-012's FTL/rest rules at all). Untouched by the pair model — no
# seat, no partner requirement, no change from before this piece.
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
    result2 = _assign_pilot("CPT", [f2], crew_id=crew_id)

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
    candidates = assignment_service.find_legal_candidates_for_seat([target_flight], "LM")
    ids = [c.crew_id for c in candidates]

    assert crew_a in ids
    assert crew_b in ids


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

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assert result.status == "REJECTED"
    assert result.legality_status == "ILLEGAL"
    assert any(a.rule_code == "AE-CREW-QUAL-001_LICENSE_EXPIRED" for a in result.alerts)
    assert len(assignment_service.get_roster_for_crew(crew_id)) == 0


def test_expired_medical_is_illegal_and_blocks_save(_patch_engine):
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2020, 1, 1))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_MEDICAL_EXPIRED" for a in result.alerts)


def test_missing_expiry_date_is_needs_review_not_silently_allowed(_patch_engine):
    """A NULL expiry is neither a silent pass nor a silent reject —
    it's an unresolved data gap that needs a human to look at it,
    same principle as the NEEDS_MANUAL_REVIEW gate this reuses."""
    crew_id = _add_crew("CPT", sim_expiry=None)
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assert result.status == "NEEDS_REVIEW"
    assert any(a.rule_code == "AE-CREW-QUAL-001_SIM_EXPIRY_MISSING" for a in result.alerts)
    assert len(assignment_service.get_roster_for_crew(crew_id)) == 0


def test_inactive_crew_is_illegal_and_blocks_save(_patch_engine):
    crew_id = _add_crew("CPT")
    crew_service.deactivate_crew(crew_id)
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

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

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

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

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assert result.status == "REJECTED"
    assert any(a.rule_code == "AE-CREW-QUAL-001_MEDICAL_EXPIRED" for a in result.alerts)


def test_qualification_valid_on_duty_date_is_not_flagged(_patch_engine):
    """Sanity check the other direction: a document valid well past
    the duty date must not be flagged."""
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2026, 12, 31))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assert result.status == "ALLOWED"


def test_expiry_exactly_on_duty_date_is_expired(_patch_engine):
    """Confirmed boundary convention: a document expiring ON the duty
    date itself counts as already expired, not valid through it."""
    crew_id = _add_crew("CPT", medical_expiry=dt.date(2026, 7, 20))
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

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

    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

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
    candidates = assignment_service.find_legal_candidates_for_seat(
        [target_flight], "CPT", operating_position="COMMANDER")
    ids = _legal_ids(candidates)

    assert qualified in ids
    assert inactive not in ids
    assert expired_license not in ids


def test_find_legal_candidates_for_ftl_exempt_role_still_excludes_unqualified(_patch_engine):
    """The FTL-exempt trivial branch of find_legal_candidates_for_seat
    must not bypass the qualification gate either — otherwise a
    deactivated/expired-document LM or ENGR could still be suggested
    as a downstream-swap candidate."""
    qualified_lm = _add_crew("LM")
    unqualified_lm = _add_crew("LM", medical_expiry=dt.date(2020, 1, 1))

    target_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    candidates = assignment_service.find_legal_candidates_for_seat([target_flight], "LM")
    ids = _legal_ids(candidates)

    assert qualified_lm in ids
    assert unqualified_lm not in ids


def test_control_room_path_also_enforces_qualification_gate(_patch_engine):
    """Same underlying validation core (_validate_new_duty) as
    assign_pair_to_duty() — confirming the two entry points stayed in
    sync for this gate too, same reasoning as the role-match and
    NEEDS_MANUAL_REVIEW parity tests above."""
    crew_id = _add_crew("CPT", license_expiry=dt.date(2020, 1, 1))
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]

    result, flight_ids = _assign_pilot_adhoc("CPT", flights_data, crew_id=crew_id)

    assert result.status == "REJECTED"
    assert flight_ids == []


# ------------------------------------------------------------------
# remove_assignment_from_duty (replaces remove_assignment(), which
# operated on a single (crew_id, flight_id, role_assigned) row and
# left every OTHER sector of a multi-sector duty still active —
# 2026-08-12, flight-deck crew package)
# ------------------------------------------------------------------

def test_remove_assignment_from_duty_cancels_not_deletes(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assignment_service.remove_assignment_from_duty(crew_id, result.duty_id, reason="test removal")

    active = assignment_service.get_roster_for_crew(crew_id, include_cancelled=False)
    assert len(active) == 0

    everyone = assignment_service.get_roster_for_crew(crew_id, include_cancelled=True)
    assert len(everyone) == 1
    assert everyone.iloc[0]["status"] == "CANCELLED"


def test_remove_assignment_from_duty_cancels_every_sector_of_multi_sector_duty(_patch_engine):
    """The actual fix this replacement makes: a multi-sector duty is
    cancelled as a WHOLE, not one leg at a time — no sector left
    active with a now-stale duty-level report/debrief/fdp_hours."""
    crew_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0),
                      origin="KHI", destination="LHE")
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0),
                      origin="LHE", destination="KHI")
    result = _assign_pilot("CPT", [f1, f2], crew_id=crew_id)
    assert len(result.roster_ids) == 2

    assignment_service.remove_assignment_from_duty(crew_id, result.duty_id)

    active = assignment_service.get_roster_for_crew(crew_id, include_cancelled=False)
    assert len(active) == 0
    everyone = assignment_service.get_roster_for_crew(crew_id, include_cancelled=True)
    assert len(everyone) == 2
    assert set(everyone["status"]) == {"CANCELLED"}


def test_remove_assignment_from_duty_does_not_affect_the_other_pilot(_patch_engine):
    """Duty-scoped means scoped to (crew_id, duty_id) — removing one
    pilot's duty must not touch the partner's own, separate duty_id
    for the same flight."""
    cpt = _add_crew("CPT")
    fo = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])
    assert result.status == "ALLOWED"

    assignment_service.remove_assignment_from_duty(cpt, result.commander_duty_id)

    assert len(assignment_service.get_roster_for_crew(cpt, include_cancelled=False)) == 0
    fo_active = assignment_service.get_roster_for_crew(fo, include_cancelled=False)
    assert len(fo_active) == 1
    assert fo_active.iloc[0]["status"] != "CANCELLED"


def test_remove_then_reassign_same_crew_flight_role_succeeds(_patch_engine):
    """Proves the Phase 3 partial-unique-index fix actually gets used
    correctly end-to-end through the service layer, not just at the
    raw SQL level — and, under the pair model, that re-adding a
    removed pilot against their still-real partner uses the
    'fill the remaining seat' path correctly."""
    cpt = _add_crew("CPT")
    fo = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    first = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])
    assert first.status == "ALLOWED"

    assignment_service.remove_assignment_from_duty(cpt, first.commander_duty_id)

    result = assignment_service.assign_crew_to_duty(cpt, [flight_id], "CPT", operating_position="COMMANDER")
    assert result.status == "ALLOWED"


def test_remove_nonexistent_assignment_raises(_patch_engine):
    crew_id = _add_crew("CPT")
    with pytest.raises(ValueError):
        assignment_service.remove_assignment_from_duty(crew_id, "DUTY-DOES-NOT-EXIST")


def test_remove_assignment_from_duty_opens_uncovered_seats_row_for_rotation_linked_seat(_patch_engine):
    """The design decision flagged explicitly during plan review,
    decided deliberately: a manually-unassigned pilot leaving a
    rotation-linked seat must open (or reopen) an uncovered_seats row
    — uncovered_seats is the single durable source of truth for
    'which seats are currently empty,' not just a generator failure
    log. A non-rotation (ad-hoc) unassign must NOT write here at all
    — there's no rotation_instance_id to key it on, and Control Room's
    own path always resolves synchronously anyway."""
    engine = _patch_engine
    instance_id = _seed_rotation_instance(engine)
    cpt = _add_crew("CPT")
    fo = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45),
                             rotation_instance_id=instance_id)
    result = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])
    assert result.status == "ALLOWED"

    assignment_service.remove_assignment_from_duty(cpt, result.commander_duty_id, reason="sick")

    open_rows = pd.read_sql(text("""
        SELECT * FROM uncovered_seats
        WHERE rotation_instance_id = :iid AND resolved_at IS NULL
    """), engine, params={"iid": instance_id})
    assert len(open_rows) == 1
    assert open_rows.iloc[0]["operating_position"] == "COMMANDER"
    assert "sick" in open_rows.iloc[0]["reason"]

    # Ad-hoc (non-rotation) removal must NOT write here at all.
    adhoc_flight = _add_flight(dt.datetime(2026, 7, 21, 5, 45), dt.datetime(2026, 7, 21, 7, 45))
    cpt2 = _add_crew("CPT")
    fo2 = _add_crew("FO")
    adhoc_result = assignment_service.assign_pair_to_duty(cpt2, fo2, [adhoc_flight])
    assignment_service.remove_assignment_from_duty(cpt2, adhoc_result.commander_duty_id)
    all_open = pd.read_sql(text("SELECT * FROM uncovered_seats WHERE resolved_at IS NULL"), engine)
    assert len(all_open) == 1  # only the rotation-linked one from above


def test_refilling_seat_resolves_open_uncovered_seats_row(_patch_engine):
    """_resolve_uncovered_seat(), called from both assign_crew_to_duty()
    and assign_pair_to_duty()'s write paths: a seat that gets genuinely
    refilled must not keep showing as uncovered."""
    engine = _patch_engine
    instance_id = _seed_rotation_instance(engine)
    cpt = _add_crew("CPT")
    fo = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45),
                             rotation_instance_id=instance_id)
    result = assignment_service.assign_pair_to_duty(cpt, fo, [flight_id])
    assignment_service.remove_assignment_from_duty(cpt, result.commander_duty_id)

    new_cpt = _add_crew("CPT")
    refill = assignment_service.assign_crew_to_duty(
        new_cpt, [flight_id], "CPT", operating_position="COMMANDER")
    assert refill.status == "ALLOWED"

    open_rows = pd.read_sql(text("""
        SELECT * FROM uncovered_seats WHERE rotation_instance_id = :iid AND resolved_at IS NULL
    """), engine, params={"iid": instance_id})
    assert len(open_rows) == 0


# ------------------------------------------------------------------
# assign_crew_to_new_flights — Control Room's atomic flight+assignment
# (LM/ENGR only, unaffected by the pair model). Pilot ad-hoc scenarios
# moved to _assign_pilot_adhoc()/assign_pair_to_new_flights() tests
# throughout this file.
# ------------------------------------------------------------------

def test_legal_adhoc_assignment_creates_both_flight_and_roster_row(_patch_engine):
    crew_id = _add_crew("CPT")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]

    result, flight_ids = _assign_pilot_adhoc("CPT", flights_data, crew_id=crew_id)

    assert result.status == "ALLOWED"
    assert len(flight_ids) == 1
    assert flight_service.get_flight(flight_ids[0]) is not None
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert len(roster_df) == 1
    assert roster_df.iloc[0]["flight_id"] == flight_ids[0]


def test_control_room_pair_crash_before_final_audit_write_rolls_back_everything(_patch_engine, monkeypatch):
    """Step 6 (2026-08-02) regression test, now proven at pair scale.
    assign_pair_to_new_flights()'s ALLOWED path makes 3 log_audit
    calls: FLIGHT_ADDED, then one ASSIGNMENT_CREATED per pilot. This
    forces a failure on the LAST one (the second pilot's own audit
    write) — by then, under a bug, the flight and BOTH roster rows
    would already be committed. Under the fix (one engine.begin()
    covering every write), everything must roll back together,
    including the FIRST pilot's own already-inserted row."""
    crew_id = _add_crew("CPT")
    flights_data = [_flight_data(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))]

    real_log_audit = assignment_service.log_audit
    calls = {"n": 0}

    def _flaky_log_audit(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash before the second pilot's audit write")
        return real_log_audit(*args, **kwargs)

    monkeypatch.setattr(assignment_service, "log_audit", _flaky_log_audit)

    with pytest.raises(RuntimeError):
        _assign_pilot_adhoc("CPT", flights_data, crew_id=crew_id)

    assert len(flight_service.get_all_flights()) == 0
    assert len(assignment_service.get_roster_for_crew(crew_id)) == 0
    assert calls["n"] == 3  # confirms the failure actually happened where intended


def test_illegal_adhoc_assignment_creates_no_flight_at_all(_patch_engine):
    """The actual Control Room guarantee: an illegal ad-hoc assignment
    must not leave an orphan, uncrewed flight sitting in Flight Log.
    Nothing gets saved to either table, for either seat."""
    crew_id = _add_crew("CPT")

    # Prior duty requiring 16h rest after (8h FDP, D21) — seeded
    # directly (flight created normally, roster row seeded via raw
    # SQL) since an 8h duty now correctly triggers NEEDS_MANUAL_REVIEW
    # through the real assignment API, which has its own dedicated
    # tests.
    prior_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, prior_flight, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    flights_before = len(flight_service.get_all_flights())

    # Only 5h after prior debrief — illegal.
    illegal_flights = [_flight_data(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))]
    result, flight_ids = _assign_pilot_adhoc("CPT", illegal_flights, crew_id=crew_id)

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
    _assign_pilot_adhoc("CPT", illegal_flights, crew_id=crew_id, app_user="tester")

    audit = _audit_rows(_patch_engine, "ADHOC_PAIR_REJECTED")
    assert len(audit) == 1
    assert audit.iloc[0]["legality_result"] == "ILLEGAL"


def test_adhoc_assignment_also_detects_downstream_conflicts(_patch_engine):
    """The downstream check must work identically for the ad-hoc path
    — it's the same underlying mechanism, not a separate one."""
    crew_id = _add_crew("CPT")

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    _assign_pilot("CPT", [future_flight], crew_id=crew_id)

    adhoc_flights = [_flight_data(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 18, 45))]
    result, flight_ids = _assign_pilot_adhoc("CPT", adhoc_flights, crew_id=crew_id)

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
    result, flight_ids = _assign_pilot_adhoc("CPT", flights_data, crew_id=crew_id)
    assert result.status == "ALLOWED"
    assert len(flight_ids) == 2


# ------------------------------------------------------------------
# Step 5 (2026-08-01): three "stale data" findings —
# LOOKBACK_DAYS starving D9.2.3, cancel_flight() not excluding
# roster history, update_flight() not recomputing FDP on delay.
# ------------------------------------------------------------------

def test_lookback_days_covers_the_365_day_cumulative_window():
    """D9.2.3 (365-day/1000h cumulative flight time,
    core/legality/pcaa_ano012_core.py) needs a full year of history —
    LOOKBACK_DAYS was 35 (real bug: enough for D9's 7/14/28-day
    windows, but silently starving D9.2.3 of the other 330 days it
    needs, so that rule has never once been able to fire correctly
    for any real assignment)."""
    assert assignment_service.LOOKBACK_DAYS > 365


def test_lookback_window_covers_40_day_old_duty_previously_excluded(_patch_engine):
    """Confirms the fix isn't just the constant on paper — a duty 40
    days before a new assignment (well past the old 35-day lookback,
    within the new one) is now actually returned as history."""
    crew_id = _add_crew("CPT")

    old_flight = _add_flight(dt.datetime(2026, 6, 10, 5, 0), dt.datetime(2026, 6, 10, 7, 0))
    _seed_duty(_patch_engine, crew_id, old_flight, "CPT",
               dt.datetime(2026, 6, 10, 4, 15), dt.datetime(2026, 6, 10, 7, 15), 3.0)

    new_flight = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = _assign_pilot("CPT", [new_flight], crew_id=crew_id)
    assert result.status == "ALLOWED"

    history = assignment_service._load_duty_records_for_crew(
        _patch_engine, crew_id, "KHI",
        start=result.computed_report_time - dt.timedelta(days=assignment_service.LOOKBACK_DAYS),
        end=result.computed_debrief_time,
    )
    assert any(r["duty"].duty_id.startswith("SEEDED-") for r in history)


def test_cancel_flight_and_roster_cascades_cancellation(_patch_engine):
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assignment_service.cancel_flight_and_roster(flight_id, reason="test cancel")

    everyone = assignment_service.get_roster_for_crew(crew_id, include_cancelled=True)
    assert everyone.iloc[0]["status"] == "CANCELLED"

    active = assignment_service.get_roster_for_crew(crew_id, include_cancelled=False)
    assert len(active) == 0

    assert flight_service.get_flight(flight_id)["status"] == "CANCELLED"


def test_cancel_flight_and_roster_with_no_crew_assigned_does_not_error(_patch_engine):
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    assignment_service.cancel_flight_and_roster(flight_id)
    assert flight_service.get_flight(flight_id)["status"] == "CANCELLED"


def test_cancelled_flight_duty_excluded_from_legality_history(_patch_engine):
    """The actual bug this fixes: before cascading cancellation to
    roster, a cancelled flight's duty still counted toward FDP/rest
    history, since _load_duty_records_for_crew() only ever filtered
    on roster.status, never flights.status."""
    crew_id = _add_crew("CPT")

    # 8h FDP prior duty, seeded directly (same reason as every other
    # heavy-duty seed in this file — NEEDS_MANUAL_REVIEW via D25
    # would otherwise write nothing through the real API). Requires
    # max(12h, 2*8)=16h rest after it (D21) if still active.
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 12, 0))
    _seed_duty(_patch_engine, crew_id, f1, "CPT",
               dt.datetime(2026, 7, 20, 4, 15), dt.datetime(2026, 7, 20, 12, 15), 8.0)

    assignment_service.cancel_flight_and_roster(f1)

    # Only 5h after the (now-cancelled) prior duty's debrief — would
    # be REJECTED (needs 16h) if the cancelled duty still counted.
    f2 = _add_flight(dt.datetime(2026, 7, 20, 17, 45), dt.datetime(2026, 7, 20, 19, 45))
    result = _assign_pilot("CPT", [f2], crew_id=crew_id)
    assert result.status == "ALLOWED"


def test_delay_recompute_updates_debrief_and_fdp_report_stays_fixed(_patch_engine):
    """Small delay, stays well under every review-gate threshold (D16.2.2's
    10h included) — proves the mechanical recompute itself (report_time
    fixed, debrief_time/fdp_hours updated) without the review-gate
    noise, matching core/duty_builder.py's own documented
    report-time-never-shifts principle."""
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)
    assert result.status == "ALLOWED"

    assignment_service.update_flight_actual_times_and_revalidate(
        flight_id, arr_time_actual=dt.datetime(2026, 7, 20, 8, 45))

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert roster_df.iloc[0]["report_time"] == dt.datetime(2026, 7, 20, 5, 0)  # unchanged
    assert roster_df.iloc[0]["debrief_time"] == dt.datetime(2026, 7, 20, 9, 0)  # 08:45 + 15min
    assert roster_df.iloc[0]["fdp_hours"] == pytest.approx(4.0)
    assert roster_df.iloc[0]["status"] == "PLANNED"  # not flagged — still legal


def test_delay_recompute_flags_needs_review_when_no_longer_legal(_patch_engine):
    """A delay that pushes FDP to 10.5h, with the duty's interval now
    overlapping local 02:00-04:59, triggers
    D16.2.2_NIGHT_DUTY_OVER_10H_FRM_REQUIRED (has_approved_frm defaults
    False, ANO012CoreValidator() never overrides it) — confirms the
    roster row itself gets flagged NEEDS_REVIEW, not just an audit-log
    alert, per the explicit 'flag the row, don't just log it' decision."""
    crew_id = _add_crew("CPT")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 19, 45), dt.datetime(2026, 7, 20, 20, 0))
    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)
    assert result.status == "ALLOWED"

    outcomes = assignment_service.update_flight_actual_times_and_revalidate(
        flight_id, arr_time_actual=dt.datetime(2026, 7, 21, 5, 15))

    # Two crew hold this flight now (the subject and their disposable
    # partner), so both get recomputed — filter to the subject.
    subject_outcome = next(o for o in outcomes if o["crew_id"] == crew_id)
    assert subject_outcome["validation_result"].status == "NEEDS_MANUAL_REVIEW"
    assert any(a.rule_code == "D16.2.2_NIGHT_DUTY_OVER_10H_FRM_REQUIRED"
               for a in subject_outcome["validation_result"].alerts)

    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert roster_df.iloc[0]["status"] == "NEEDS_REVIEW"
    assert roster_df.iloc[0]["fdp_hours"] == pytest.approx(10.5)

    audit = _audit_rows(_patch_engine, "DUTY_FLAGGED_FOR_REVIEW_AFTER_DELAY")
    assert len(audit) >= 1
    assert audit.iloc[0]["warning_or_failure_reason"]


def test_delay_recompute_handles_multiple_crew_on_same_flight_independently(_patch_engine):
    """A single flight can carry several crew (CPT/FO/LM/AME), each
    with their OWN duty_id (_validate_new_duty() generates a fresh
    one per assignment call) — a delay must recompute EACH one
    independently, not just the first found."""
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))

    assignment_service.assign_pair_to_duty(cpt_id, fo_id, [flight_id])

    outcomes = assignment_service.update_flight_actual_times_and_revalidate(
        flight_id, arr_time_actual=dt.datetime(2026, 7, 20, 8, 45))

    assert len(outcomes) == 2
    assert {o["crew_id"] for o in outcomes} == {cpt_id, fo_id}

    cpt_roster = assignment_service.get_roster_for_crew(cpt_id)
    fo_roster = assignment_service.get_roster_for_crew(fo_id)
    assert cpt_roster.iloc[0]["debrief_time"] == dt.datetime(2026, 7, 20, 9, 0)
    assert fo_roster.iloc[0]["debrief_time"] == dt.datetime(2026, 7, 20, 9, 0)


def test_delay_recompute_for_ftl_exempt_role_updates_times_but_stays_legal(_patch_engine):
    """LM is FTL-exempt — even a large delay must update debrief_time/
    fdp_hours (the recompute itself always applies) but must NOT run
    FDP/rest math (D9/D21) or D25 nutrition checks against it, same
    exemption already enforced at assignment time."""
    crew_id = _add_crew("LM")
    flight_id = _add_flight(dt.datetime(2026, 7, 20, 5, 45), dt.datetime(2026, 7, 20, 7, 45))
    assignment_service.assign_crew_to_duty(crew_id, [flight_id], "LM")

    outcomes = assignment_service.update_flight_actual_times_and_revalidate(
        flight_id, arr_time_actual=dt.datetime(2026, 7, 20, 18, 0))

    assert outcomes[0]["validation_result"].status == "LEGAL"
    roster_df = assignment_service.get_roster_for_crew(crew_id)
    assert roster_df.iloc[0]["debrief_time"] == dt.datetime(2026, 7, 20, 18, 15)
    assert roster_df.iloc[0]["status"] == "PLANNED"


def test_delay_recompute_detects_downstream_conflict_on_other_future_duties(_patch_engine):
    """Same downstream-ripple mechanism already tested for new
    assignments (test_adhoc_assignment_that_breaks_future_scheduled_duty_is_flagged)
    must also apply to a delay: a delay that consumes enough rest can
    break an already-scheduled LATER duty, not just the one delayed."""
    crew_id = _add_crew("CPT")

    future_flight = _add_flight(dt.datetime(2026, 7, 22, 5, 45), dt.datetime(2026, 7, 22, 7, 45))
    _assign_pilot("CPT", [future_flight], crew_id=crew_id)

    day2_flight = _add_flight(dt.datetime(2026, 7, 21, 14, 45), dt.datetime(2026, 7, 21, 15, 45))
    result = _assign_pilot("CPT", [day2_flight], crew_id=crew_id)
    assert result.status == "ALLOWED"
    assert result.downstream_conflicts == []

    # Delay pushes actual arrival late enough that the 12h floor to
    # Day 3's 05:00 report is now violated (debrief moves from 16:00
    # to 19:00 — only a 10h gap, same numbers as the new-assignment
    # downstream-conflict test).
    outcomes = assignment_service.update_flight_actual_times_and_revalidate(
        day2_flight, arr_time_actual=dt.datetime(2026, 7, 21, 18, 45))

    subject_outcome = next(o for o in outcomes if o["crew_id"] == crew_id)
    assert len(subject_outcome["downstream_conflicts"]) == 1
    assert subject_outcome["downstream_conflicts"][0].flight_ids == [future_flight]


# ------------------------------------------------------------------
# Alert summarization (2026-08-01) — see services/alert_summary.py.
# Bucketing/collapsing correctness is proven at the unit level in
# tests/test_alert_summary.py; these confirm the real wiring: many
# historical duties genuinely produce many historical RuleAlerts
# through the real assignment API, the subject pilot's alert_summary
# reflects the correct buckets, status is unaffected by summarization,
# and the audit log actually gets a bounded reason instead of one
# line per historical duty.
# ------------------------------------------------------------------

def test_assign_crew_to_duty_illegal_from_pure_historical_breach_reports_blocked_by_history_true(_patch_engine):
    crew_id = _add_crew("CPT")
    result = _seed_heavy_history_and_assign_far_future(_patch_engine, crew_id)

    assert result.status == "REJECTED"
    assert not any(a.status == "ILLEGAL" for a in result.alert_summary.target_duty_alerts)
    assert any(hc.status == "ILLEGAL" for hc in result.alert_summary.historical_counts)
    assert result.alert_summary.blocked_by_history_only is True


def test_assign_crew_to_duty_status_stays_illegal_even_though_only_historical_data_breaches(_patch_engine):
    """The core correctness guarantee, named explicitly per the
    requirement: summarization must never change what actually
    determines legality — an assignment resting on a genuine
    historical breach must still report ILLEGAL overall."""
    crew_id = _add_crew("CPT")
    result = _seed_heavy_history_and_assign_far_future(_patch_engine, crew_id)

    assert result.status == "REJECTED"
    assert result.legality_status == "ILLEGAL"


def test_assign_crew_to_duty_illegal_from_its_own_breach_reports_blocked_by_history_false(_patch_engine):
    """Same 40-duty seed, but the new duty is the very next day —
    only ~23h after a 24h-FDP duty, well under the D21 rest floor
    (max(12h, 2x24h)=48h) required after it, so the new duty's OWN
    checks breach too (rest, and/or D9.1.3's 28-day window, which is
    still dominated by the recent seeded stretch this close to it).
    blocked_by_history_only must be False either way: this duty
    genuinely contributes to the breach, not just pre-existing
    history."""
    crew_id = _add_crew("CPT")
    start = dt.datetime(2026, 1, 1, 6, 0)
    _seed_many_duties(_patch_engine, crew_id, "CPT", start, count=40, duty_hours=24, spacing_days=3)

    last_debrief = start + dt.timedelta(days=39 * 3, hours=24)
    new_dep = last_debrief + dt.timedelta(days=1)
    new_arr = new_dep + dt.timedelta(hours=2)
    flight_id = _add_flight(new_dep, new_arr)
    result = _assign_pilot("CPT", [flight_id], crew_id=crew_id)

    assert result.status == "REJECTED"
    assert any(a.status == "ILLEGAL" for a in result.alert_summary.target_duty_alerts)
    assert result.alert_summary.blocked_by_history_only is False


def test_seventh_day_off_illegal_alone_does_not_claim_blocked_by_history(_patch_engine):
    """Schedule-level edge case at the integration level: 6 short
    legal daily duties, a 7th that trips only D23.2_SEVENTH_DAY_OFF —
    genuinely could be caused by this duty (it's the 7th consecutive
    day), so blocked_by_history_only must stay False, the confirmed
    conservative default. One fixed partner used across all 7 days
    (not a fresh one per call) so the pair itself stays constant —
    the subject's own 7-day streak is what's under test, not partner
    churn."""
    crew_id = _add_crew("CPT")
    partner_id = _add_crew("FO")
    base = dt.datetime(2026, 3, 1, 5, 45)
    for day in range(6):
        dep = base + dt.timedelta(days=day)
        arr = dep + dt.timedelta(hours=2)
        flight_id = _add_flight(dep, arr)
        result = _assign_pilot("CPT", [flight_id], crew_id=crew_id, partner_id=partner_id)
        assert result.status == "ALLOWED", f"day {day} setup failed: {result.legality_status}"

    seventh_dep = base + dt.timedelta(days=6)
    seventh_arr = seventh_dep + dt.timedelta(hours=2)
    seventh_flight = _add_flight(seventh_dep, seventh_arr)
    result = _assign_pilot("CPT", [seventh_flight], crew_id=crew_id, partner_id=partner_id)

    assert result.status == "REJECTED"
    assert result.legality_status == "ILLEGAL"
    assert any(
        a.rule_code == "D23.2_SEVENTH_DAY_OFF" and a.status == "ILLEGAL"
        for a in result.alert_summary.schedule_level_alerts
    )
    assert result.alert_summary.blocked_by_history_only is False


def test_audit_log_reason_bounded_for_many_historical_breaches(_patch_engine):
    """Direct regression test for the measured ~150KB audit row: the
    historical rule's message must appear exactly once (summarized
    with a count), not once per historical duty."""
    crew_id = _add_crew("CPT")
    result = _seed_heavy_history_and_assign_far_future(_patch_engine, crew_id)
    assert result.status == "REJECTED"

    audit = _audit_rows(_patch_engine, "PAIR_ASSIGNMENT_REJECTED")
    assert len(audit) == 1
    reason = audit.iloc[0]["warning_or_failure_reason"]
    assert reason is not None
    assert len(reason) < 5000

    historical_rule = result.alert_summary.historical_counts[0].rule_code
    assert reason.count(historical_rule) == 1
