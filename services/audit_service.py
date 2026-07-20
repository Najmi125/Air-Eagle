"""
services/audit_service.py

Single owner of audit_log writes. Every other service function that
writes to the database calls log_audit() after the write succeeds —
see crew_service.py for the pattern.

This is NOT the same file as the old repo's utils/audit_service.py
(which wrote to a smaller override_audit table with fewer fields).
Rebuilt here against the full audit_log schema from Section 16.
"""
from typing import Optional
from sqlalchemy import text

from db.db import get_engine


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
