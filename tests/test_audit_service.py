"""
tests/test_audit_service.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from sqlalchemy import text
from services.audit_service import log_audit


def test_log_audit_writes_a_row(migrated_db, monkeypatch):
    import services.audit_service as audit_mod
    monkeypatch.setattr(audit_mod, "get_engine", lambda: migrated_db)

    log_audit(action_type="CREW_ADDED", affected_crew="CPT-01", app_user="tester")

    df = pd.read_sql(text("SELECT * FROM audit_log"), migrated_db)
    assert len(df) == 1
    assert df.iloc[0]["action_type"] == "CREW_ADDED"
    assert df.iloc[0]["affected_crew"] == "CPT-01"
    assert df.iloc[0]["app_user"] == "tester"


def test_log_audit_minimal_call_leaves_others_null(migrated_db, monkeypatch):
    import services.audit_service as audit_mod
    monkeypatch.setattr(audit_mod, "get_engine", lambda: migrated_db)

    log_audit(action_type="SYSTEM_CHECK")

    df = pd.read_sql(text("SELECT * FROM audit_log"), migrated_db)
    assert len(df) == 1
    assert df.iloc[0]["action_type"] == "SYSTEM_CHECK"
    assert pd.isna(df.iloc[0]["affected_crew"])


def test_log_audit_is_append_only(migrated_db, monkeypatch):
    """Multiple calls accumulate rows, never overwrite."""
    import services.audit_service as audit_mod
    monkeypatch.setattr(audit_mod, "get_engine", lambda: migrated_db)

    log_audit(action_type="CREW_ADDED", affected_crew="CPT-01")
    log_audit(action_type="CREW_UPDATED", affected_crew="CPT-01")
    log_audit(action_type="CREW_DEACTIVATED", affected_crew="CPT-01")

    df = pd.read_sql(text("SELECT action_type FROM audit_log ORDER BY audit_id"), migrated_db)
    assert list(df["action_type"]) == ["CREW_ADDED", "CREW_UPDATED", "CREW_DEACTIVATED"]
