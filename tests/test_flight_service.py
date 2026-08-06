"""
tests/test_flight_service.py
"""
import sys
from pathlib import Path
import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pandas as pd
from sqlalchemy import text

import services.flight_service as flight_service
import services.audit_service as audit_service


@pytest.fixture(autouse=True)
def _patch_engine(migrated_db, monkeypatch):
    monkeypatch.setattr(flight_service, "get_engine", lambda: migrated_db)
    monkeypatch.setattr(audit_service, "get_engine", lambda: migrated_db)
    return migrated_db


def _audit_rows(engine, action_type=None):
    q = "SELECT * FROM audit_log"
    params = {}
    if action_type:
        q += " WHERE action_type = :at"
        params["at"] = action_type
    return pd.read_sql(text(q), engine, params=params)


def _valid_flight(**overrides):
    base = {
        "origin": "KHI",
        "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 0),
        "arr_time_planned": dt.datetime(2026, 7, 20, 7, 0),
        "domestic": True,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------
# add_flight
# ------------------------------------------------------------------

def test_add_flight_success_returns_id(_patch_engine):
    flight_id = flight_service.add_flight(_valid_flight())
    assert isinstance(flight_id, int)

    row = flight_service.get_flight(flight_id)
    assert row is not None
    assert row["origin"] == "KHI"
    assert row["status"] == "PLANNED"  # DB default


def test_add_flight_missing_required_field_raises(_patch_engine):
    with pytest.raises(ValueError):
        flight_service.add_flight({"origin": "KHI", "destination": "LHE"})


def test_add_flight_domestic_false_is_not_treated_as_missing(_patch_engine):
    """Regression test: the required-field check originally used
    truthiness (`not value`), which incorrectly treats domestic=False
    as missing since `not False` is True. A domestic=False flight is
    completely valid and must not be rejected."""
    flight_id = flight_service.add_flight(_valid_flight(domestic=False))
    row = flight_service.get_flight(flight_id)
    assert row["domestic"] == False


def test_add_flight_empty_string_origin_is_still_treated_as_missing(_patch_engine):
    """The fix for the domestic=False case must not overcorrect and
    let an empty-string origin through as 'present'."""
    with pytest.raises(ValueError):
        flight_service.add_flight(_valid_flight(origin=""))


def test_add_flight_arrival_before_departure_raises_at_service_layer(_patch_engine):
    """Service layer should catch this with a clean ValueError, not
    let a raw SQL CHECK-constraint error bubble up."""
    with pytest.raises(ValueError):
        flight_service.add_flight(_valid_flight(
            dep_time_planned=dt.datetime(2026, 7, 20, 9, 0),
            arr_time_planned=dt.datetime(2026, 7, 20, 5, 0),
        ))


def test_add_flight_writes_audit_record(_patch_engine):
    flight_id = flight_service.add_flight(_valid_flight(), app_user="tester")
    audit = _audit_rows(_patch_engine, "FLIGHT_ADDED")
    assert len(audit) == 1
    assert audit.iloc[0]["affected_flight"] == flight_id
    assert audit.iloc[0]["app_user"] == "tester"


# ------------------------------------------------------------------
# add_flight's conn parameter (2026-08-04, Phase 7 approval workflow)
# -- same contract as audit_service.log_audit()'s own conn parameter
# (Step 6, 2026-08-02), same three things need proving: it writes on
# the normal success path, it shares the caller's rollback, and the
# default (no conn) path is unaffected.
# ------------------------------------------------------------------

def test_add_flight_with_conn_writes_on_success_path(_patch_engine):
    engine = _patch_engine
    with engine.begin() as conn:
        flight_id = flight_service.add_flight(_valid_flight(), app_user="tester", conn=conn)

    flight = flight_service.get_flight(flight_id)
    assert flight is not None
    assert flight["origin"] == "KHI"

    audit = _audit_rows(engine, "FLIGHT_ADDED")
    assert len(audit) == 1
    assert audit.iloc[0]["affected_flight"] == flight_id


def test_add_flight_with_conn_shares_callers_rollback(_patch_engine):
    engine = _patch_engine
    try:
        with engine.begin() as conn:
            flight_service.add_flight(_valid_flight(), conn=conn)
            raise RuntimeError("simulated failure after the insert, before commit")
    except RuntimeError:
        pass

    assert len(flight_service.get_all_flights()) == 0
    assert _audit_rows(engine, "FLIGHT_ADDED").empty


def test_add_flight_without_conn_still_commits_independently(_patch_engine):
    flight_id = flight_service.add_flight(_valid_flight())
    assert flight_service.get_flight(flight_id) is not None


# ------------------------------------------------------------------
# update_flight
# ------------------------------------------------------------------

def test_update_flight_records_actual_times(_patch_engine):
    flight_id = flight_service.add_flight(_valid_flight())
    flight_service.update_flight(flight_id, {
        "dep_time_actual": dt.datetime(2026, 7, 20, 5, 30),
        "arr_time_actual": dt.datetime(2026, 7, 20, 7, 45),
    })
    row = flight_service.get_flight(flight_id)
    assert row["dep_time_actual"] == dt.datetime(2026, 7, 20, 5, 30)
    assert row["arr_time_actual"] == dt.datetime(2026, 7, 20, 7, 45)


def test_update_flight_nonexistent_raises(_patch_engine):
    with pytest.raises(ValueError):
        flight_service.update_flight(999999, {"status": "OPERATED"})


def test_update_flight_writes_audit_with_before_and_after(_patch_engine):
    flight_id = flight_service.add_flight(_valid_flight())
    flight_service.update_flight(flight_id, {"status": "OPERATED"}, app_user="tester")

    audit = _audit_rows(_patch_engine, "FLIGHT_UPDATED")
    assert len(audit) == 1
    assert "PLANNED" in audit.iloc[0]["original_state"]
    assert "OPERATED" in audit.iloc[0]["changed_state"]


# ------------------------------------------------------------------
# cancel_flight
# ------------------------------------------------------------------

def test_cancel_flight_marks_status_not_deletes(_patch_engine):
    flight_id = flight_service.add_flight(_valid_flight())
    flight_service.cancel_flight(flight_id, reason="Aircraft AOG")

    row = flight_service.get_flight(flight_id)
    assert row is not None                # row still exists — permanent log
    assert row["status"] == "CANCELLED"


def test_cancel_flight_nonexistent_raises(_patch_engine):
    with pytest.raises(ValueError):
        flight_service.cancel_flight(999999)


def test_cancel_flight_writes_audit_with_reason(_patch_engine):
    flight_id = flight_service.add_flight(_valid_flight())
    flight_service.cancel_flight(flight_id, reason="Aircraft AOG", app_user="tester")

    audit = _audit_rows(_patch_engine, "FLIGHT_CANCELLED")
    assert len(audit) == 1
    assert audit.iloc[0]["reason"] == "Aircraft AOG"


# ------------------------------------------------------------------
# get_all_flights — permanent log behavior
# ------------------------------------------------------------------

def test_get_all_flights_includes_cancelled_by_default(_patch_engine):
    """This IS the permanent-log requirement, verified directly:
    get_all_flights() with no filter must show cancelled flights too,
    not hide them."""
    id1 = flight_service.add_flight(_valid_flight())
    id2 = flight_service.add_flight(_valid_flight(origin="LHE", destination="KHI"))
    flight_service.cancel_flight(id2, reason="test cancel")

    all_flights = flight_service.get_all_flights()
    assert set(all_flights["flight_id"]) == {id1, id2}


def test_get_all_flights_status_filter_narrows_correctly(_patch_engine):
    id1 = flight_service.add_flight(_valid_flight())
    id2 = flight_service.add_flight(_valid_flight(origin="LHE", destination="KHI"))
    flight_service.cancel_flight(id2, reason="test cancel")

    cancelled_only = flight_service.get_all_flights(status_filter="CANCELLED")
    assert list(cancelled_only["flight_id"]) == [id2]


def test_get_flight_returns_none_for_missing(_patch_engine):
    assert flight_service.get_flight(999999) is None
