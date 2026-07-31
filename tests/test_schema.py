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
from core.duty_summary import calculate_crew_duty_summary


def _insert_crew(engine, crew_id="CPT-01", role="CPT"):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO crew (crew_id, name, role, base) VALUES (:cid, :name, :role, 'KHI')"
        ), {"cid": crew_id, "name": "Test Crew", "role": role})


def _insert_flight(engine, dep=None, arr=None, domestic=True):
    dep = dep or dt.datetime(2026, 7, 20, 5, 0)
    arr = arr or dt.datetime(2026, 7, 20, 9, 0)
    with engine.begin() as conn:
        result = conn.execute(text(
            "INSERT INTO flights (origin, destination, dep_time_planned, arr_time_planned, domestic) "
            "VALUES ('KHI', 'LHE', :dep, :arr, :domestic) RETURNING flight_id"
        ), {"dep": dep, "arr": arr, "domestic": domestic})
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
    """Cross-checks against the reconciled schema (template + the real
    operator terminology from migration 007: SIM/Route Check/IR) — if
    this drifts, the columns won't match what the operator's data
    actually uses."""
    with migrated_db.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'crew'"
        )).fetchall()
    col_names = {row[0] for row in cols}
    expected = {
        "crew_id", "operator_staff_id", "name", "role", "date_of_birth",
        "nationality", "base", "phone", "email", "license_no",
        "license_expiry", "medical_expiry",
        "sim_expiry", "route_check_expiry", "ir_expiry",
        "sep_expiry", "crm_expiry", "dg_expiry", "remarks",
    }
    assert expected <= col_names


def test_crew_table_old_column_names_are_gone(migrated_db):
    """Migration 007 renamed lpc_opc_expiry -> sim_expiry and
    line_check_expiry -> route_check_expiry to match the operator's
    real terminology. The old names must not linger as leftover
    columns — that would mean the rename silently failed to apply."""
    with migrated_db.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'crew'"
        )).fetchall()
    col_names = {row[0] for row in cols}
    assert "lpc_opc_expiry" not in col_names
    assert "line_check_expiry" not in col_names


def test_type_rating_and_contract_expiry_columns_removed(migrated_db):
    """Migration 008 drops type_rating_expiry and contract_expiry
    entirely — both were empty for every real crew row, and the
    qualification gate holding every real crew member for review over
    two fields the operator never populates was a dead end. Confirms
    the drop actually applied, not just that the migration ran without
    a SQL error."""
    with migrated_db.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'crew'"
        )).fetchall()
    col_names = {row[0] for row in cols}
    assert "type_rating_expiry" not in col_names
    assert "contract_expiry" not in col_names


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


def test_flights_domestic_is_required_not_defaulted(migrated_db):
    """domestic must be NOT NULL with no default — Air Eagle's route
    mix was never confirmed, so this must be an explicit decision at
    flight-creation time, never silently assumed."""
    with migrated_db.connect() as conn:
        row = conn.execute(text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'flights' AND column_name = 'domestic'"
        )).fetchone()
    assert row[0] == "NO"
    assert row[1] is None


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
                "arr_time_planned, status, domestic) VALUES ('KHI', 'LHE', "
                "'2026-07-20 05:00', '2026-07-20 09:00', 'NOT_A_REAL_STATUS', TRUE)"
            ))


def test_flights_rejects_arrival_before_departure(migrated_db):
    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO flights (origin, destination, dep_time_planned, arr_time_planned, domestic) "
                "VALUES ('KHI', 'LHE', '2026-07-20 09:00', '2026-07-20 05:00', TRUE)"
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


def test_roster_status_check_allows_needs_review(migrated_db):
    """Migration 009 (roster_needs_review_status) added NEEDS_REVIEW
    to roster.status's allowed values, for
    assignment_service.update_flight_actual_times_and_revalidate() to
    flag a duty a delay recompute made no longer LEGAL/WARNING.
    Confirms the CHECK constraint actually accepts it, not just that
    the migration ran without a SQL error."""
    _insert_crew(migrated_db)
    flight_id = _insert_flight(migrated_db)
    with migrated_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
            "report_time, debrief_time, role_assigned, status) "
            "VALUES ('CPT-01', :fid, 'D-1', '2026-07-20', "
            "'2026-07-20 05:00', '2026-07-20 08:00', 'CPT', 'NEEDS_REVIEW')"
        ), {"fid": flight_id})


def test_roster_status_check_still_rejects_invalid_values(migrated_db):
    """Sanity check the other direction — adding NEEDS_REVIEW must not
    have accidentally loosened the constraint to accept anything."""
    _insert_crew(migrated_db)
    flight_id = _insert_flight(migrated_db)
    with pytest.raises(Exception):
        with migrated_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
                "report_time, debrief_time, role_assigned, status) "
                "VALUES ('CPT-01', :fid, 'D-1', '2026-07-20', "
                "'2026-07-20 05:00', '2026-07-20 08:00', 'CPT', 'BOGUS_STATUS')"
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


def test_cancelled_assignment_does_not_block_reassignment(migrated_db):
    """Regression test for the partial unique index (005): cancelling
    an assignment then re-assigning the same (crew, flight, role)
    must succeed — the old unconditional UNIQUE constraint would have
    permanently blocked this."""
    _insert_crew(migrated_db)
    flight_id = _insert_flight(migrated_db)

    with migrated_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
            "report_time, debrief_time, role_assigned, status) "
            "VALUES ('CPT-01', :fid, 'D-1', '2026-07-20', "
            "'2026-07-20 05:00', '2026-07-20 09:00', 'CPT', 'CANCELLED')"
        ), {"fid": flight_id})

    # Re-assigning the exact same (crew, flight, role) after the
    # first one was cancelled must succeed, not raise.
    with migrated_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO roster (crew_id, flight_id, duty_id, duty_date, "
            "report_time, debrief_time, role_assigned) "
            "VALUES ('CPT-01', :fid, 'D-2', '2026-07-20', "
            "'2026-07-20 05:00', '2026-07-20 09:00', 'CPT')"
        ), {"fid": flight_id})

    with migrated_db.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM roster WHERE flight_id = :fid"
        ), {"fid": flight_id}).scalar()
    assert count == 2  # the cancelled one + the new active one, both present


def test_two_simultaneously_active_duplicates_still_blocked(migrated_db):
    """The partial index must still block a genuine duplicate ACTIVE
    assignment — it only exempts cancelled rows, not all rows."""
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
                "VALUES ('CPT-01', :fid, 'D-2', '2026-07-20', "
                "'2026-07-20 05:00', '2026-07-20 09:00', 'CPT')"
            ), {"fid": flight_id})


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
