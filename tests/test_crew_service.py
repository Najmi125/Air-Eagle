"""
tests/test_crew_service.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pandas as pd
from sqlalchemy import text

import services.crew_service as crew_service
import services.audit_service as audit_service


@pytest.fixture(autouse=True)
def _patch_engine(migrated_db, monkeypatch):
    """crew_service and audit_service both call get_engine() internally
    (imported into each module's own namespace) — patch both so every
    test in this file transparently uses the real, migrated test DB."""
    monkeypatch.setattr(crew_service, "get_engine", lambda: migrated_db)
    monkeypatch.setattr(audit_service, "get_engine", lambda: migrated_db)
    return migrated_db


def _audit_rows(engine, action_type=None):
    q = "SELECT * FROM audit_log"
    params = {}
    if action_type:
        q += " WHERE action_type = :at"
        params["at"] = action_type
    return pd.read_sql(text(q), engine, params=params)


# ------------------------------------------------------------------
# add_crew
# ------------------------------------------------------------------

def test_add_crew_success_returns_generated_id(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Test Captain", "role": "CPT", "base": "KHI"})
    assert crew_id == "CPT-01"

    row = crew_service.get_crew(crew_id)
    assert row is not None
    assert row["name"] == "Test Captain"
    assert row["base"] == "KHI"


def test_add_crew_missing_name_raises(_patch_engine):
    with pytest.raises(ValueError):
        crew_service.add_crew({"role": "CPT"})


def test_add_crew_missing_role_raises(_patch_engine):
    with pytest.raises(ValueError):
        crew_service.add_crew({"name": "Test Captain"})


def test_add_crew_ignores_caller_supplied_crew_id(_patch_engine):
    """crew_id must always be system-generated, never taken from the
    operator's own data — this is what the crew data template already
    promised them."""
    crew_id = crew_service.add_crew({
        "name": "Test Captain", "role": "CPT", "crew_id": "SOMETHING-ELSE",
    })
    assert crew_id == "CPT-01"
    assert crew_service.get_crew("SOMETHING-ELSE") is None


def test_add_crew_generates_sequential_ids_per_role(_patch_engine):
    id1 = crew_service.add_crew({"name": "Captain One", "role": "CPT"})
    id2 = crew_service.add_crew({"name": "Captain Two", "role": "CPT"})
    id3 = crew_service.add_crew({"name": "First Officer One", "role": "FO"})
    assert id1 == "CPT-01"
    assert id2 == "CPT-02"
    assert id3 == "FO-01"  # separate sequence per role


def test_add_crew_unknown_role_uses_generic_prefix(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Someone", "role": "Other"})
    assert crew_id.startswith("CREW-")


def test_add_crew_writes_audit_record(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Test Captain", "role": "CPT"}, app_user="tester")
    audit = _audit_rows(_patch_engine, "CREW_ADDED")
    assert len(audit) == 1
    assert audit.iloc[0]["affected_crew"] == crew_id
    assert audit.iloc[0]["app_user"] == "tester"


def test_add_crew_operator_staff_id_preserved_separately(_patch_engine):
    crew_id = crew_service.add_crew({
        "name": "Test Captain", "role": "CPT", "operator_staff_id": "AE-1001",
    })
    row = crew_service.get_crew(crew_id)
    assert row["operator_staff_id"] == "AE-1001"


# ------------------------------------------------------------------
# update_crew
# ------------------------------------------------------------------

def test_update_crew_changes_only_specified_fields(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Test Captain", "role": "CPT", "base": "KHI"})
    crew_service.update_crew(crew_id, {"phone": "+92-300-0000000"})

    row = crew_service.get_crew(crew_id)
    assert row["phone"] == "+92-300-0000000"
    assert row["name"] == "Test Captain"  # untouched
    assert row["base"] == "KHI"           # untouched


def test_update_crew_nonexistent_raises(_patch_engine):
    with pytest.raises(ValueError):
        crew_service.update_crew("NO-SUCH-ID", {"phone": "123"})


def test_update_crew_no_valid_fields_raises(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Test Captain", "role": "CPT"})
    with pytest.raises(ValueError):
        crew_service.update_crew(crew_id, {"crew_id": "TRY-TO-CHANGE-PK"})


def test_update_crew_writes_audit_record_with_before_and_after(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Test Captain", "role": "CPT", "phone": "111"})
    crew_service.update_crew(crew_id, {"phone": "222"}, app_user="tester")

    audit = _audit_rows(_patch_engine, "CREW_UPDATED")
    assert len(audit) == 1
    assert "111" in audit.iloc[0]["original_state"]
    assert "222" in audit.iloc[0]["changed_state"]


# ------------------------------------------------------------------
# deactivate_crew
# ------------------------------------------------------------------

def test_deactivate_crew_soft_deletes_not_hard_deletes(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Test Captain", "role": "CPT"})
    crew_service.deactivate_crew(crew_id, reason="left the company")

    row = crew_service.get_crew(crew_id)
    assert row is not None            # row still exists
    assert row["is_active"] == False  # just marked inactive


def test_deactivate_crew_nonexistent_raises(_patch_engine):
    with pytest.raises(ValueError):
        crew_service.deactivate_crew("NO-SUCH-ID")


def test_deactivate_crew_writes_audit_record(_patch_engine):
    crew_id = crew_service.add_crew({"name": "Test Captain", "role": "CPT"})
    crew_service.deactivate_crew(crew_id, reason="left the company", app_user="tester")

    audit = _audit_rows(_patch_engine, "CREW_DEACTIVATED")
    assert len(audit) == 1
    assert audit.iloc[0]["reason"] == "left the company"


# ------------------------------------------------------------------
# get_crew / get_all_crew
# ------------------------------------------------------------------

def test_get_crew_returns_none_for_missing(_patch_engine):
    assert crew_service.get_crew("NO-SUCH-ID") is None


def test_get_all_crew_active_only_excludes_deactivated(_patch_engine):
    id1 = crew_service.add_crew({"name": "Active One", "role": "CPT"})
    id2 = crew_service.add_crew({"name": "Inactive One", "role": "CPT"})
    crew_service.deactivate_crew(id2)

    active = crew_service.get_all_crew(active_only=True)
    assert list(active["crew_id"]) == [id1]

    everyone = crew_service.get_all_crew(active_only=False)
    assert set(everyone["crew_id"]) == {id1, id2}
