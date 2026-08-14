"""
tests/test_flight_deck_seat_constraints.py

DB-level guarantees migrations/016_operating_position.sql and
migrations/017_uncovered_seats.sql add — tested directly against real
Postgres via raw SQL, not assumed from the migration source, same
discipline as tests/test_rotation_template_service.py's own
constraint tests (migrations/011/012/005 precedents).

These are the actual defect this piece closes, proven at the database
level rather than just the service layer: today's schema has NO CHECK
on role_assigned's values and no uniqueness guarantee beyond "the same
person can't double-book" — five Captains on one flight, all ALLOWED,
was exactly what it permitted. Service-layer tests (test_assignment_
service.py) prove the application never TRIES to violate these
invariants; these tests prove the database itself would refuse it even
if something did.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import services.assignment_service as assignment_service
import services.crew_service as crew_service
import services.flight_service as flight_service
import services.rotation_template_service as rts

_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY, "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY, "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY, "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY, "dg_expiry": _FAR_FUTURE_EXPIRY,
    "date_of_birth": dt.date(1985, 1, 1),
}


@pytest.fixture(autouse=True)
def _patch_engine(_patch_all_service_engines):
    """Thin per-file wrapper — the actual patching logic lives once in
    conftest.py's _patch_all_service_engines, so no module here can be
    forgotten (see that fixture's docstring for why this matters)."""
    return _patch_all_service_engines


def _add_crew(role, **overrides):
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _add_flight(dep=dt.datetime(2026, 7, 20, 5, 0), arr=dt.datetime(2026, 7, 20, 7, 0)):
    return flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dep, "arr_time_planned": arr, "domestic": True,
    })


def _insert_roster_row(engine, crew_id, flight_id, role_assigned, operating_position,
                        status="PLANNED", suffix=""):
    import uuid
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                report_time, debrief_time, fdp_hours, role_assigned, operating_position, status)
            VALUES (:crew_id, :flight_id, :duty_id, '2026-07-20',
                '2026-07-20 04:15', '2026-07-20 07:15', 3.0, :role_assigned, :operating_position, :status)
        """), {
            "crew_id": crew_id, "flight_id": flight_id,
            "duty_id": f"DUTY-{uuid.uuid4().hex[:8]}{suffix}",
            "role_assigned": role_assigned, "operating_position": operating_position, "status": status,
        })


# ------------------------------------------------------------------
# migrations/016_operating_position.sql — CHECK constraints
# ------------------------------------------------------------------

def test_invalid_operating_position_value_rejected(_patch_engine):
    engine = _patch_engine
    crew_id = _add_crew("CPT")
    flight_id = _add_flight()
    with pytest.raises(IntegrityError):
        _insert_roster_row(engine, crew_id, flight_id, "CPT", "COPILOT")  # not a real value


def test_null_operating_position_is_allowed(_patch_engine):
    """LM/ENGR — the common, unaffected case."""
    engine = _patch_engine
    crew_id = _add_crew("LM")
    flight_id = _add_flight()
    _insert_roster_row(engine, crew_id, flight_id, "LM", None)  # must not raise


def test_commander_must_be_cpt_graded_at_the_database_level(_patch_engine):
    """chk_roster_commander_is_cpt — an FO recorded as Commander must
    be rejected even via a raw INSERT, not just by the service layer's
    own SEAT_ELIGIBLE_GRADES check."""
    engine = _patch_engine
    fo_id = _add_crew("FO")
    flight_id = _add_flight()
    with pytest.raises(IntegrityError):
        _insert_roster_row(engine, fo_id, flight_id, "FO", "COMMANDER")


def test_second_pilot_may_be_fo_graded_at_the_database_level(_patch_engine):
    """No equivalent restriction for SECOND_PILOT — FO is a legitimate
    Second Pilot grade, must not raise."""
    engine = _patch_engine
    fo_id = _add_crew("FO")
    flight_id = _add_flight()
    _insert_roster_row(engine, fo_id, flight_id, "FO", "SECOND_PILOT")  # must not raise


# ------------------------------------------------------------------
# migrations/016_operating_position.sql —
# uq_roster_flight_operating_position_active: at most one ACTIVE
# Commander and one ACTIVE Second Pilot per flight_id. THE actual
# defect this piece closes at the database level — five Captains on
# one flight, all ALLOWED, was exactly what the schema permitted
# before this index existed.
# ------------------------------------------------------------------

def test_two_different_commanders_on_the_same_flight_rejected(_patch_engine):
    engine = _patch_engine
    cpt_a = _add_crew("CPT")
    cpt_b = _add_crew("CPT")
    flight_id = _add_flight()
    _insert_roster_row(engine, cpt_a, flight_id, "CPT", "COMMANDER", suffix="A")
    with pytest.raises(IntegrityError):
        _insert_roster_row(engine, cpt_b, flight_id, "CPT", "COMMANDER", suffix="B")


def test_two_different_second_pilots_on_the_same_flight_rejected(_patch_engine):
    engine = _patch_engine
    fo_a = _add_crew("FO")
    fo_b = _add_crew("FO")
    flight_id = _add_flight()
    _insert_roster_row(engine, fo_a, flight_id, "FO", "SECOND_PILOT", suffix="A")
    with pytest.raises(IntegrityError):
        _insert_roster_row(engine, fo_b, flight_id, "FO", "SECOND_PILOT", suffix="B")


def test_commander_and_second_pilot_together_on_one_flight_allowed(_patch_engine):
    """The normal, intended case — one of each, same flight_id."""
    engine = _patch_engine
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")
    flight_id = _add_flight()
    _insert_roster_row(engine, cpt_id, flight_id, "CPT", "COMMANDER")
    _insert_roster_row(engine, fo_id, flight_id, "FO", "SECOND_PILOT")  # must not raise


def test_multiple_null_operating_position_rows_on_one_flight_allowed(_patch_engine):
    """The partial index is scoped to operating_position IS NOT NULL —
    several LM/ENGR on one flight (a real, legitimate crew complement)
    must not collide with each other or with the pilot seats."""
    engine = _patch_engine
    lm_a = _add_crew("LM")
    lm_b = _add_crew("ENGR")
    flight_id = _add_flight()
    _insert_roster_row(engine, lm_a, flight_id, "LM", None)
    _insert_roster_row(engine, lm_b, flight_id, "ENGR", None)  # must not raise


def test_cancelled_commander_row_does_not_block_a_new_one(_patch_engine):
    """The index is scoped to status != 'CANCELLED' — a cancelled
    Commander row must not block a replacement from taking the seat,
    mirroring the existing uq_roster_crew_flight_role_active precedent
    (migration 005) this new index deliberately follows."""
    engine = _patch_engine
    cpt_a = _add_crew("CPT")
    cpt_b = _add_crew("CPT")
    flight_id = _add_flight()
    _insert_roster_row(engine, cpt_a, flight_id, "CPT", "COMMANDER", status="CANCELLED", suffix="A")
    _insert_roster_row(engine, cpt_b, flight_id, "CPT", "COMMANDER", suffix="B")  # must not raise


def test_same_pilot_commander_on_two_different_flights_allowed(_patch_engine):
    """A multi-sector duty's own two rows for the SAME pilot, sharing
    the same operating_position but DIFFERENT flight_id — confirms the
    index is genuinely flight_id-scoped, not accidentally global to the
    crew_id, and does not collide with a pilot's own multi-sector duty
    (verified against real Postgres before this migration was written
    — see migrations/016's own header)."""
    engine = _patch_engine
    cpt_id = _add_crew("CPT")
    f1 = _add_flight(dt.datetime(2026, 7, 20, 5, 0), dt.datetime(2026, 7, 20, 7, 0))
    f2 = _add_flight(dt.datetime(2026, 7, 20, 8, 0), dt.datetime(2026, 7, 20, 10, 0))
    duty_id = "DUTY-SAME-SEAT-MULTI-SECTOR"
    with engine.begin() as conn:
        for fid, suffix in ((f1, "1"), (f2, "2")):
            conn.execute(text("""
                INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                    report_time, debrief_time, fdp_hours, role_assigned, operating_position, status)
                VALUES (:crew_id, :flight_id, :duty_id, '2026-07-20',
                    '2026-07-20 04:15', '2026-07-20 10:15', 6.0, 'CPT', 'COMMANDER', 'PLANNED')
            """), {"crew_id": cpt_id, "flight_id": fid, "duty_id": duty_id})


def test_same_person_cannot_hold_both_seats_via_existing_migration_005_index(_patch_engine):
    """No NEW constraint needed for this — the pre-existing
    uq_roster_crew_flight_role_active (migration 005, scoped to crew_id
    + flight_id + role_assigned) already blocks a second row for the
    same person on the same flight, since role_assigned is fixed to
    their one real grade regardless of which operating_position is
    attempted."""
    engine = _patch_engine
    cpt_id = _add_crew("CPT")
    flight_id = _add_flight()
    _insert_roster_row(engine, cpt_id, flight_id, "CPT", "COMMANDER", suffix="A")
    with pytest.raises(IntegrityError):
        _insert_roster_row(engine, cpt_id, flight_id, "CPT", "SECOND_PILOT", suffix="B")


# ------------------------------------------------------------------
# migrations/017_uncovered_seats.sql
# ------------------------------------------------------------------

def _one_rotation_instance_id(engine):
    rts.create_template(
        rotation_code="EPE-786-787", days_of_week=[1, 2, 3, 4, 5],
        legs=[
            {"leg_order": 1, "origin": "KHI", "destination": "LHE",
             "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
             "flight_no": "EPE 786", "domestic": True},
        ],
        effective_from=dt.date(2026, 1, 1), meal_provided=True, snack_provided=True,
    )
    created = rts.expand_and_persist("EPE-786-787", dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    return created[0]


def test_uncovered_seats_operating_position_check(_patch_engine):
    engine = _patch_engine
    instance_id = _one_rotation_instance_id(engine)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO uncovered_seats (rotation_instance_id, operating_position, reason)
                VALUES (:iid, 'COPILOT', 'test')
            """), {"iid": instance_id})


def test_uncovered_seats_foreign_key_to_rotation_instances(_patch_engine):
    engine = _patch_engine
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO uncovered_seats (rotation_instance_id, operating_position, reason)
                VALUES (999999, 'COMMANDER', 'test')
            """))


def test_uncovered_seats_duplicate_open_row_for_same_seat_rejected(_patch_engine):
    """uq_uncovered_seats_open — a second bare INSERT for the same
    (rotation_instance_id, operating_position) must be rejected; the
    real write path (services/roster_generator_service.py's own
    _record_uncovered(), services/assignment_service.py's
    _remove_assignment_from_duty()) uses ON CONFLICT DO UPDATE instead
    of a bare INSERT specifically because of this — proven separately
    below."""
    engine = _patch_engine
    instance_id = _one_rotation_instance_id(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO uncovered_seats (rotation_instance_id, operating_position, reason)
            VALUES (:iid, 'COMMANDER', 'first reason')
        """), {"iid": instance_id})

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO uncovered_seats (rotation_instance_id, operating_position, reason)
                VALUES (:iid, 'COMMANDER', 'second reason')
            """), {"iid": instance_id})


def test_uncovered_seats_upsert_updates_existing_row_instead_of_erroring(_patch_engine):
    """The real write pattern both writers use — ON CONFLICT (rotation_
    instance_id, operating_position) DO UPDATE — refreshes the same row
    (new reason, generated_at, resolved_at cleared) rather than
    accumulating duplicates or raising."""
    engine = _patch_engine
    instance_id = _one_rotation_instance_id(engine)
    upsert_sql = text("""
        INSERT INTO uncovered_seats (rotation_instance_id, operating_position, reason)
        VALUES (:iid, 'COMMANDER', :reason)
        ON CONFLICT (rotation_instance_id, operating_position)
        DO UPDATE SET reason = EXCLUDED.reason, generated_at = NOW(), resolved_at = NULL
    """)
    with engine.begin() as conn:
        conn.execute(upsert_sql, {"iid": instance_id, "reason": "first reason"})
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE uncovered_seats SET resolved_at = NOW()
            WHERE rotation_instance_id = :iid AND operating_position = 'COMMANDER'
        """), {"iid": instance_id})
    with engine.begin() as conn:
        conn.execute(upsert_sql, {"iid": instance_id, "reason": "second reason"})

    import pandas as pd
    rows = pd.read_sql(text("SELECT * FROM uncovered_seats WHERE rotation_instance_id = :iid"),
                        engine, params={"iid": instance_id})
    assert len(rows) == 1
    assert rows.iloc[0]["reason"] == "second reason"
    assert pd.isna(rows.iloc[0]["resolved_at"])  # reopened, not left resolved


def test_uncovered_seats_written_by_generator_matches_schema(_patch_engine):
    """End-to-end through the real writer, not just raw SQL against the
    schema directly — services/roster_generator_service.py's
    _record_uncovered() must actually satisfy every constraint tested
    above."""
    engine = _patch_engine
    import services.roster_generator_service as rgs
    instance_id = _one_rotation_instance_id(engine)
    rts.approve_instance(instance_id)
    # No crew at all -- guarantees an uncovered outcome for both seats.
    summary = rgs.generate_for_window(dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert len(summary.uncovered) == 2

    import pandas as pd
    rows = pd.read_sql(text("SELECT * FROM uncovered_seats WHERE rotation_instance_id = :iid"),
                        engine, params={"iid": instance_id})
    assert len(rows) == 2
    assert set(rows["operating_position"]) == {"COMMANDER", "SECOND_PILOT"}
