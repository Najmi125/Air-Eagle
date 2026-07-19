"""
tests/test_schema.py

Tests the actual applied schema against real Postgres — not just
that migrations run without SQL errors, but that the constraints
they define actually do what they claim: FKs reject orphan
references, CHECK constraints reject bad enum values, the UNIQUE
constraint on (crew_id, flight_id, role_assigned) actually prevents
duplicate assignment.

The last test in this file is the most important one: it inserts a
genuine 2-sector duty into the roster table exactly as the app will,
then runs it through core/duty_summary.py's already-tested (Phase 2)
grouping logic, proving the schema and the dedup logic actually fit
together — not just that each is independently correct.
"""
import sys
from pathlib import Path
import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pandas as pd
from sqlalchemy import text
from scripts import run_migrations as rm
from core.duty_summary import calculate_crew_duty_summary


@pytest.fixture
def migrated_db(db_engine):
    """db_engine (conftest.py) already gives a wiped-clean schema.
    Apply all migrations on top of it for schema tests."""
    rm.run(engine=db_engine)
    return db_engine


def _insert_crew(engine, crew_id="CPT-01", role="CPT"):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO crew (crew_id, name, role, base) VALUES (:cid, :name, :role, 'KHI')"
        ), {"cid": crew_id, "name": "Test Crew", "role": role})


def _insert_flight(engine, dep=None, arr=None):
    dep = dep or dt.datetime(2026, 7, 20, 5, 0)
    arr = arr or dt.datetime(2026, 7, 20, 9, 0)
    with engine.begin() as conn:
        result = conn.execute(text(
            "INSERT INTO flights (origin, destination, dep_time_planned, arr_time_planned) "
            "VALUES ('KHI', 'LHE', :dep, :arr) RETURNING flight_id"
        ), {"dep": dep, "arr": arr})
        return result.scalar()


# ------------------------------------------------------------------
# Migrations apply and produce the expected tables
# ------------------------------------------------------------------

def test_all_three_tables_exist_after_migration(migrated_db):
    with migrated_db.connect() as conn:
        tables = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )).fetchall()
    table_names = {row[0] for row in tables}
    assert {"crew", "flights", "roster", "schema_migrations"} <= table_names


def test_crew_table_has_all_template_columns(migrated_db):
    """Cross-checks against the exact template sent to the operator
    — if this drifts, the columns won't match what comes back Monday."""
    with migrated_db.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'crew'"
        )).fetchall()
    col_names = {row[0] for row in cols}
    expected = {
        "crew_id", "operator_staff_id", "name", "role", "date_of_birth",
        "nationality", "base", "phone", "email", "license_no",
        "license_expiry", "medical_expiry", "type_rating_expiry",
        "lpc_opc_expiry", "line_check_expiry", "sep_expiry", "crm_expiry",
        "dg_expiry", "contract_expiry", "remarks",
    }
    assert expected <= col_names


def test_base_column_has_no_hardcoded_default(migrated_db):
    """Regression test for a bug that was fixed twice already in the
    old repo (utils/schema.py, utils/schema_v2.py both hardcoded
    DEFAULT 'KHI'). If this ever comes back, this test catches it."""
    with migrated_db.connect() as conn:
        default = conn.execute(text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'crew' AND column_name = 'base'"
        )).scalar()
    assert default is None


# ------------------------------------------------------------------
# Constraints actually enforce what they claim to
# ------------------------------------------------------------------

def test_roster_rejects_unknown_crew_id(migrated_db):
    flight_id = _insert_flight(migrated_db)
    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
                "report_time, debrief_time, role_assigned) "
                "VALUES ('NO-SUCH-CREW', :fid, 'D-1', '2026-07-20', "
                "'2026-07-20 05:00', '2026-07-20 09:00', 'CPT')"
            ), {"fid": flight_id})


def test_roster_rejects_unknown_flight_id(migrated_db):
    _insert_crew(migrated_db)
    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
                "report_time, debrief_time, role_assigned) "
                "VALUES ('CPT-01', 999999, 'D-1', '2026-07-20', "
                "'2026-07-20 05:00', '2026-07-20 09:00', 'CPT')"
            ))


def test_flights_rejects_invalid_status(migrated_db):
    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO flights (origin, destination, dep_time_planned, "
                "arr_time_planned, status) VALUES ('KHI', 'LHE', "
                "'2026-07-20 05:00', '2026-07-20 09:00', 'NOT_A_REAL_STATUS')"
            ))


def test_flights_rejects_arrival_before_departure(migrated_db):
    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO flights (origin, destination, dep_time_planned, arr_time_planned) "
                "VALUES ('KHI', 'LHE', '2026-07-20 09:00', '2026-07-20 05:00')"
            ))


def test_roster_rejects_debrief_before_report(migrated_db):
    _insert_crew(migrated_db)
    flight_id = _insert_flight(migrated_db)
    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
                "report_time, debrief_time, role_assigned) "
                "VALUES ('CPT-01', :fid, 'D-1', '2026-07-20', "
                "'2026-07-20 09:00', '2026-07-20 05:00', 'CPT')"
            ), {"fid": flight_id})


def test_roster_unique_constraint_blocks_duplicate_assignment(migrated_db):
    """Same crew, same flight, same role assigned twice must be
    rejected — that's a data-entry error, not a legitimate case."""
    _insert_crew(migrated_db)
    flight_id = _insert_flight(migrated_db)
    with migrated_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
            "report_time, debrief_time, role_assigned) "
            "VALUES ('CPT-01', :fid, 'D-1', '2026-07-20', "
            "'2026-07-20 05:00', '2026-07-20 09:00', 'CPT')"
        ), {"fid": flight_id})

    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
                "report_time, debrief_time, role_assigned) "
                "VALUES ('CPT-01', :fid, 'D-1', '2026-07-20', "
                "'2026-07-20 05:00', '2026-07-20 09:00', 'CPT')"
            ), {"fid": flight_id})


def test_two_different_crew_can_hold_same_role_same_flight(migrated_db):
    """The unique constraint must NOT block two different loadmasters
    (or any role) on the same flight — only duplicate (crew, flight,
    role) triples."""
    _insert_crew(migrated_db, crew_id="LM-01", role="LM")
    _insert_crew(migrated_db, crew_id="LM-02", role="LM")
    flight_id = _insert_flight(migrated_db)
    with migrated_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
            "report_time, debrief_time, role_assigned) "
            "VALUES ('LM-01', :fid, 'D-1', '2026-07-20', "
            "'2026-07-20 05:00', '2026-07-20 09:00', 'LM')"
        ), {"fid": flight_id})
        conn.execute(text(
            "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
            "report_time, debrief_time, role_assigned) "
            "VALUES ('LM-02', :fid, 'D-1', '2026-07-20', "
            "'2026-07-20 05:00', '2026-07-20 09:00', 'LM')"
        ), {"fid": flight_id})
    with migrated_db.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM roster WHERE flight_id = :fid"
        ), {"fid": flight_id}).scalar()
    assert count == 2


# ------------------------------------------------------------------
# The connecting test: does the actual schema shape work correctly
# with the already-tested Phase 2 duty_summary logic?
# ------------------------------------------------------------------

def test_two_sector_duty_from_real_schema_dedupes_correctly(migrated_db):
    """
    Inserts a genuine 2-sector duty (two flights, one duty_id,
    shared report/debrief/fdp_hours — exactly how the app will
    write it) into the real, constrained roster table, reads it
    back, and confirms core/duty_summary.py's dedup logic (tested
    independently in Phase 2) produces the correct result against
    data that actually came from the database, not a hand-built
    DataFrame.
    """
    _insert_crew(migrated_db)
    flight_1 = _insert_flight(
        migrated_db,
        dep=dt.datetime(2026, 7, 20, 5, 0),
        arr=dt.datetime(2026, 7, 20, 7, 0),
    )
    flight_2 = _insert_flight(
        migrated_db,
        dep=dt.datetime(2026, 7, 20, 8, 0),
        arr=dt.datetime(2026, 7, 20, 9, 0),
    )

    shared_duty_fields = {
        "duty_id": "D-100",
        "duty_date": "2026-07-20",
        "report_time": "2026-07-20 05:00",
        "debrief_time": "2026-07-20 09:00",
    }

    with migrated_db.begin() as conn:
        for flight_id in (flight_1, flight_2):
            conn.execute(text(
                "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
                "report_time, debrief_time, fdp_hours, role_assigned) "
                "VALUES ('CPT-01', :fid, :duty_id, :duty_date, :report_time, "
                ":debrief_time, 4.0, 'CPT')"
            ), {"fid": flight_id, **shared_duty_fields})

    df = pd.read_sql(text("SELECT * FROM roster"), migrated_db)
    assert len(df) == 2  # two sector rows, confirmed at the DB level

    summary = calculate_crew_duty_summary(df)
    assert summary["sector_rows"] == 2
    assert summary["unique_duties"] == 1
    assert summary["total_fdp_hours"] == 4.0  # NOT 8.0 — the exact bug class this whole system exists to prevent
