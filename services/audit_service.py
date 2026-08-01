"""
services/audit_service.py

Single owner of audit_log writes. Every other service function that
writes to the database calls log_audit() after the write succeeds —
see crew_service.py for the pattern.

This is NOT the same file as the old repo's utils/audit_service.py
(which wrote to a smaller override_audit table with fewer fields).
Rebuilt here against the full audit_log schema from Section 16.
"""
import datetime as dt
from typing import List, Optional
import pandas as pd
from sqlalchemy import text

from db.db import get_engine

# Action types the audit_compliance report template treats as
# "blocked, held" — the actual, currently-implemented subset of that
# template's description. Nothing in this codebase writes an
# "override" action_type yet (override_reason exists as a column,
# per Section 16's required field list, but no write path populates
# it — Section 14's downstream-conflict handling is "alert + suggest
# candidates, human confirms," not an auto-override mechanism). This
# list reflects what's actually queryable today; extend it here, in
# one place, if/when an override write path is ever built, rather
# than each caller guessing its own action_type list.
COMPLIANCE_ACTION_TYPES = [
    "ASSIGNMENT_REJECTED", "ASSIGNMENT_HELD_FOR_REVIEW",
    "ADHOC_FLIGHT_REJECTED", "ADHOC_FLIGHT_HELD_FOR_REVIEW",
    "DUTY_FLAGGED_FOR_REVIEW_AFTER_DELAY",
]


def log_audit(
    action_type: str,
    affected_crew: Optional[str] = None,
    affected_flight: Optional[int] = None,
    affected_duty: Optional[str] = None,
    affected_aircraft: Optional[str] = None,
    original_state: Optional[str] = None,
    changed_state: Optional[str] = None,
    reason: Optional[str] = None,
    rule_applied: Optional[str] = None,
    legality_result: Optional[str] = None,
    warning_or_failure_reason: Optional[str] = None,
    override_reason: Optional[str] = None,
    approver: Optional[str] = None,
    app_user: Optional[str] = None,
    airline_code: str = "AEAGLE",
    transaction_id: Optional[str] = None,
    linked_disruption_event: Optional[str] = None,
    data_source: Optional[str] = None,
) -> None:
    """
    Write one audit record. Append-only — this function never
    updates or deletes an existing row.

    action_type is the only required field beyond what SQL itself
    requires, since every audit record needs to say what happened
    even if every other detail is unknown at the point of logging.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO audit_log (
                transaction_id, app_user, airline_code, action_type,
                original_state, changed_state, reason, rule_applied,
                legality_result, warning_or_failure_reason, override_reason,
                approver, affected_crew, affected_flight, affected_duty,
                affected_aircraft, linked_disruption_event, data_source
            ) VALUES (
                :transaction_id, :app_user, :airline_code, :action_type,
                :original_state, :changed_state, :reason, :rule_applied,
                :legality_result, :warning_or_failure_reason, :override_reason,
                :approver, :affected_crew, :affected_flight, :affected_duty,
                :affected_aircraft, :linked_disruption_event, :data_source
            )
        """), {
            "transaction_id": transaction_id,
            "app_user": app_user,
            "airline_code": airline_code,
            "action_type": action_type,
            "original_state": original_state,
            "changed_state": changed_state,
            "reason": reason,
            "rule_applied": rule_applied,
            "legality_result": legality_result,
            "warning_or_failure_reason": warning_or_failure_reason,
            "override_reason": override_reason,
            "approver": approver,
            "affected_crew": affected_crew,
            "affected_flight": affected_flight,
            "affected_duty": affected_duty,
            "affected_aircraft": affected_aircraft,
            "linked_disruption_event": linked_disruption_event,
            "data_source": data_source,
        })


def get_audit_log(date_from=None, date_to=None,
                   action_types: Optional[List[str]] = None) -> pd.DataFrame:
    """
    This file's first read function — audit_service.py was write-only
    (log_audit() only) until services/assistant/reports.py's
    audit_compliance template needed a query surface (2026-08-01).

    Filters on "timestamp" (when the row was written), not on any
    duty/flight date of its own — an audit event for a duty scheduled
    next month is logged NOW, when the assignment attempt happened,
    not on the duty's own future date.
    """
    engine = get_engine()
    query = "SELECT * FROM audit_log"
    conditions = []
    params: dict = {}
    if date_from is not None:
        conditions.append('"timestamp" >= :date_from')
        params["date_from"] = date_from
    if date_to is not None:
        # Same inclusive-calendar-date widening as
        # flight_service.get_all_flights()/assignment_service.
        # search_roster() — a bare date must mean "through the end of
        # that day," not "before midnight at its start."
        if isinstance(date_to, dt.date) and not isinstance(date_to, dt.datetime):
            date_to = dt.datetime.combine(date_to, dt.time.max)
        conditions.append('"timestamp" <= :date_to')
        params["date_to"] = date_to
    if action_types:
        conditions.append("action_type = ANY(:action_types)")
        params["action_types"] = list(action_types)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += ' ORDER BY "timestamp" DESC'
    return pd.read_sql(text(query), engine, params=params)
