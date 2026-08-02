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


# ------------------------------------------------------------------
# conn parameter (Step 6, 2026-08-02) — lets a caller fold an audit
# write into its OWN already-open transaction, rather than always
# committing independently. Three things need proving: it actually
# writes on the normal/success path (not just "doesn't crash"), it
# genuinely shares the caller's transaction fate on rollback, and the
# no-conn default is completely unaffected by conn's existence.
# ------------------------------------------------------------------

def test_log_audit_with_conn_writes_on_success_path(migrated_db):
    """The success case, proven directly: inside a transaction that
    commits normally, passing conn= must still result in a real,
    queryable audit row afterward — not just 'no exception raised.'
    A conn-passing bug (e.g. building the statement but never
    executing it on the passed connection) would only show up as
    silently missing audit rows in production; this is the test that
    would have caught it."""
    with migrated_db.begin() as conn:
        log_audit(action_type="ASSIGNMENT_CREATED", affected_crew="CPT-01", conn=conn)

    df = pd.read_sql(text("SELECT * FROM audit_log"), migrated_db)
    assert len(df) == 1
    assert df.iloc[0]["action_type"] == "ASSIGNMENT_CREATED"
    assert df.iloc[0]["affected_crew"] == "CPT-01"


def test_log_audit_with_conn_shares_callers_rollback(migrated_db):
    """The behavioral difference the conn parameter's docstring calls
    out explicitly: if the caller's transaction rolls back, the audit
    record written via conn= must disappear with it — it was never
    an independent commit. This is deliberately correct (an audit
    record for a write that never happened would be worse than no
    record), but it's the opposite of every other call to this
    function, so it needs its own direct proof."""
    try:
        with migrated_db.begin() as conn:
            log_audit(action_type="ASSIGNMENT_CREATED", affected_crew="CPT-01", conn=conn)
            raise RuntimeError("simulated failure after the audit write, before commit")
    except RuntimeError:
        pass

    df = pd.read_sql(text("SELECT * FROM audit_log"), migrated_db)
    assert df.empty


def test_log_audit_without_conn_still_commits_independently(migrated_db, monkeypatch):
    """The conn parameter must not change the default (no-conn) path
    at all — every pre-existing call site across the codebase relies
    on log_audit() committing its own transaction regardless of what
    happens around it."""
    import services.audit_service as audit_mod
    monkeypatch.setattr(audit_mod, "get_engine", lambda: migrated_db)

    log_audit(action_type="CREW_ADDED", affected_crew="CPT-01")

    df = pd.read_sql(text("SELECT * FROM audit_log"), migrated_db)
    assert len(df) == 1
    assert df.iloc[0]["action_type"] == "CREW_ADDED"
