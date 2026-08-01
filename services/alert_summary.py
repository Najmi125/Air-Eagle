"""
services/alert_summary.py

Pure alert-bucketing/formatting for ValidationResult output — no
Streamlit import, no get_engine()/SQLAlchemy. Lives in services/, not
core/legality/, on the same principle FTL_EXEMPT_ROLES and the
crew-qualification gate already follow in assignment_service.py: how
to present a ValidationResult's alerts to a human is an orchestration
decision, not regulatory math, so it stays out of
core/legality/pcaa_ano012_core.py, which stays a pure, airline/UI-
agnostic rule engine. Mirrors core/duty_summary.py's shape (a
separate, pure, presentation-oriented module tested without a DB
fixture).

The problem this exists to fix (2026-08-01): core/legality/
pcaa_ano012_core.py's _check_cumulative_limits() emits one RuleAlert
per breached rule PER historical duty in a crew member's rolling
window, not one per breach overall. After LOOKBACK_DAYS widened
35 -> 370 (Step 5, to let the 365-day/1000h cumulative check actually
see enough history to fire), a crew member who has been over a limit
for months generates one alert per historical duty in that stretch —
measured against a real ~300-duty scenario: 2,215 alerts across 11
rule codes for a single assignment attempt. This module collapses
that into: the alerts actually about the duty being assessed, the
alerts about the crew member's current qualifications, genuinely
schedule-wide findings, and a per-rule-code COUNT for everything else
(the historical repetition) — without ever touching or re-deriving
ValidationResult.status itself. An assignment resting on a real
historical breach must still report ILLEGAL; this module only
reshapes what gets displayed/logged, never what determines legality.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from core.legality.pcaa_ano012_core import RuleAlert, ValidationResult, AlertStatus

# services/assignment_service.py::_check_crew_qualifications() builds
# RuleAlerts with this prefix and no duty_id (they're about the crew
# member's CURRENT license/medical/currency, not duty history) —
# confirmed as a real, distinct category during planning: without
# separating these from the two genuinely schedule-level checks below
# (D23.1/D23.2, also duty_id-less), an expired medical could get
# reported as "blocked by pre-existing history, this duty is not the
# cause" — backwards, and the single most misleading thing this
# module could say.
QUALIFICATION_RULE_PREFIX = "AE-CREW-QUAL-001"

_STATUS_PRECEDENCE = {
    AlertStatus.ILLEGAL: 3,
    AlertStatus.NEEDS_MANUAL_REVIEW: 2,
    AlertStatus.WARNING: 1,
    AlertStatus.LEGAL: 0,
}


@dataclass
class HistoricalAlertCount:
    rule_code: str
    status: AlertStatus
    severity: str
    count: int
    example_message: str


@dataclass
class AlertSummary:
    overall_status: AlertStatus
    target_duty_alerts: List[RuleAlert] = field(default_factory=list)
    qualification_alerts: List[RuleAlert] = field(default_factory=list)
    schedule_level_alerts: List[RuleAlert] = field(default_factory=list)
    historical_counts: List[HistoricalAlertCount] = field(default_factory=list)
    blocked_by_history_only: bool = False


def summarize_alerts(validation_result: ValidationResult,
                      target_duty_id: Optional[str]) -> AlertSummary:
    """
    Buckets validation_result.alerts (the full, un-summarized list —
    never mutates validation_result or reads/writes its .status other
    than copying it through) into:

      - target_duty_alerts: alert.duty_id == target_duty_id. The duty
        this call is actually about — the newly-assigned duty for
        assign_crew_to_duty()/assign_crew_to_new_flights(), or the
        recomputed duty for _recompute_one_duty_after_delay().
      - qualification_alerts: alert.duty_id is None and rule_code
        starts with QUALIFICATION_RULE_PREFIX — the crew member's
        current documents, not a duty-history pattern.
      - schedule_level_alerts: alert.duty_id is None and NOT a
        qualification alert — currently only D23.1_MANDATORY_5_DAYS_OFF
        and D23.2_SEVENTH_DAY_OFF, genuinely whole-schedule patterns
        rather than something tied to one duty.
      - historical_counts: everything else (duty_id set, and not
        target_duty_id) — grouped by rule_code into one
        HistoricalAlertCount each. This is what actually collapses
        "one alert per historical duty" into "one line per rule."

    Note: any alert with duty_id is None and a rule_code NOT prefixed
    QUALIFICATION_RULE_PREFIX falls into schedule_level_alerts by
    construction — including any future check this validator gains
    that forgets to set duty_id on what should be a duty-specific
    alert. That misclassification pushes blocked_by_history_only
    toward False (the conservative direction), never toward an
    incorrect True. Documented here so that failure mode is a
    deliberate consequence of the bucketing rule, not an accidental
    side effect discovered later.

    blocked_by_history_only is True only when overall_status is
    ILLEGAL AND target_duty_alerts + qualification_alerts +
    schedule_level_alerts contain zero ILLEGAL alerts between them —
    i.e. every ILLEGAL alert present is in historical_counts.
    Deliberately conservative, confirmed explicitly during planning: a
    genuinely ambiguous case (the new duty is what pushes a 6-day
    streak to a 7th, or the crew member's medical happens to have
    expired) must report False, not guess True.
    """
    target_duty_alerts: List[RuleAlert] = []
    qualification_alerts: List[RuleAlert] = []
    schedule_level_alerts: List[RuleAlert] = []
    historical_by_code: dict = {}

    for alert in validation_result.alerts:
        if target_duty_id is not None and alert.duty_id == target_duty_id:
            target_duty_alerts.append(alert)
        elif alert.duty_id is None and alert.rule_code.startswith(QUALIFICATION_RULE_PREFIX):
            qualification_alerts.append(alert)
        elif alert.duty_id is None:
            schedule_level_alerts.append(alert)
        else:
            historical_by_code.setdefault(alert.rule_code, []).append(alert)

    historical_counts = [
        HistoricalAlertCount(
            rule_code=code,
            status=alerts[0].status,
            severity=alerts[0].severity,
            count=len(alerts),
            example_message=alerts[0].message,
        )
        for code, alerts in historical_by_code.items()
    ]
    historical_counts.sort(
        key=lambda hc: (-_STATUS_PRECEDENCE[hc.status], -hc.count, hc.rule_code)
    )

    non_historical_illegal = any(
        a.status == AlertStatus.ILLEGAL
        for a in target_duty_alerts + qualification_alerts + schedule_level_alerts
    )
    blocked_by_history_only = (
        validation_result.status == AlertStatus.ILLEGAL and not non_historical_illegal
    )

    return AlertSummary(
        overall_status=validation_result.status,
        target_duty_alerts=target_duty_alerts,
        qualification_alerts=qualification_alerts,
        schedule_level_alerts=schedule_level_alerts,
        historical_counts=historical_counts,
        blocked_by_history_only=blocked_by_history_only,
    )


def build_audit_reason(summary: AlertSummary, statuses: frozenset) -> Optional[str]:
    """
    Builds the "; "-joined string used for audit_log.warning_or_
    failure_reason from an AlertSummary instead of the raw alert
    list. Replaces 5 previously-duplicated inline joins in
    services/assignment_service.py — 2 REJECTED sites filtering on
    ILLEGAL, 2 NEEDS_REVIEW sites filtering on NEEDS_MANUAL_REVIEW,
    and 1 site in _recompute_one_duty_after_delay() that had NO status
    filter at all (joined every alert regardless of status whenever
    now_needs_review was true) — that last one was a real correctness
    bug independent of alert volume, not just an oversized row: it
    could log WARNING/LEGAL-tier alert text into a NEEDS_REVIEW/
    ILLEGAL audit entry. Also the direct fix for the measured ~150KB
    single-row audit entry against the 2,215-alert seed scenario —
    historical alerts are summarized to one line per rule_code with a
    count here, not one line per historical duty.

    Non-historical alerts (target_duty + qualification + schedule_
    level) matching `statuses` are included in full, one message
    each — same fidelity as before for those. Historical alerts
    matching `statuses` are summarized. Returns None (matching
    log_audit's existing warning_or_failure_reason=None behavior) when
    nothing matches.
    """
    parts = [
        a.message
        for a in (summary.target_duty_alerts + summary.qualification_alerts
                  + summary.schedule_level_alerts)
        if a.status in statuses
    ]
    parts += [
        f'{hc.rule_code} x{hc.count} across historical duties in rolling window '
        f'(e.g. "{hc.example_message}")'
        for hc in summary.historical_counts
        if hc.status in statuses
    ]
    return "; ".join(parts) if parts else None


def format_alert_lines(summary: AlertSummary) -> List[str]:
    """
    Ready-to-render markdown bullet lines for the page render loops in
    pages/4_Roster.py, pages/1_Control_Room.py, and
    pages/3_Flight_Log.py — replaces the identical `for alert in
    result.alerts: st.write(f"- **{alert.rule_code}**: {alert.message}")`
    loop duplicated across all three. Pure string building, no
    `import streamlit` — callers do:
        for line in format_alert_lines(summary):
            st.write(line)
    Sections with nothing in them produce no header line.
    """
    lines: List[str] = []

    if summary.target_duty_alerts:
        lines.append("**This duty:**")
        lines += [f"- **{a.rule_code}**: {a.message}" for a in summary.target_duty_alerts]

    if summary.qualification_alerts:
        lines.append("**Crew qualification:**")
        lines += [f"- **{a.rule_code}**: {a.message}" for a in summary.qualification_alerts]

    if summary.schedule_level_alerts:
        lines.append("**Duty pattern (7th-day-off / mandatory days off):**")
        lines += [f"- **{a.rule_code}**: {a.message}" for a in summary.schedule_level_alerts]

    if summary.blocked_by_history_only:
        lines.append(
            "⚠️ Blocked by pre-existing breaches in this crew member's "
            "history — this duty itself is not the cause."
        )

    if summary.historical_counts:
        lines.append("**Historical (pre-existing) breaches in rolling history window:**")
        lines += [
            f'- **{hc.rule_code}** ({hc.status.value}): breached in {hc.count} '
            f'historical duties — e.g. "{hc.example_message}"'
            for hc in summary.historical_counts
        ]

    return lines
