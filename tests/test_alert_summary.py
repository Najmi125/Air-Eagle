"""
tests/test_alert_summary.py

Pure logic — no database needed, same principle as
tests/test_pcaa_ano012_core.py and tests/test_duty_summary.py:
RuleAlert/ValidationResult/AlertSummary are plain dataclasses, so
services/alert_summary.py's functions can be tested directly without
a real assignment or a Postgres connection.

The problem this module (and these tests) exist for: after
LOOKBACK_DAYS widened 35 -> 370, a crew member who has been over a
limit for months generates one RuleAlert per historical duty in that
stretch — measured against a real ~300-duty scenario, 2,215 alerts
for a single assignment. summarize_alerts() collapses that into: the
alerts about the duty actually being assessed, the alerts about the
crew member's current qualifications, genuine whole-schedule
findings, and a per-rule-code COUNT for everything else — without
ever touching ValidationResult.status itself (test 4/5/6/7/8 below
are the direct proof of that).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.legality.pcaa_ano012_core import RuleAlert, ValidationResult, AlertStatus
from services.alert_summary import (
    summarize_alerts, build_audit_reason, format_alert_lines,
    AlertSummary, HistoricalAlertCount, QUALIFICATION_RULE_PREFIX,
)


def _alert(rule_code, status, duty_id=None, message=None):
    return RuleAlert(
        rule_code=rule_code, status=status, severity="RED",
        message=message or f"{rule_code} fired", duty_id=duty_id,
    )


def _vr(alerts):
    """Builds a ValidationResult the same way the real validator
    does — via add_alert() for each, so .status is genuinely derived
    from the alert list (ILLEGAL > NEEDS_MANUAL_REVIEW > WARNING >
    LEGAL), not hand-set."""
    vr = ValidationResult()
    for a in alerts:
        vr.add_alert(a)
    return vr


# ------------------------------------------------------------------
# summarize_alerts() — bucketing
# ------------------------------------------------------------------

def test_summarize_alerts_splits_target_duty_from_historical_by_duty_id():
    alerts = [
        _alert("D9.1.3_28_DAY_DUTY_LIMIT", AlertStatus.WARNING, duty_id="NEW"),
        _alert("D9.1.3_28_DAY_DUTY_LIMIT", AlertStatus.ILLEGAL, duty_id="OLD-1"),
        _alert("D9.1.3_28_DAY_DUTY_LIMIT", AlertStatus.ILLEGAL, duty_id="OLD-2"),
    ]
    summary = summarize_alerts(_vr(alerts), target_duty_id="NEW")

    assert len(summary.target_duty_alerts) == 1
    assert summary.target_duty_alerts[0].duty_id == "NEW"
    assert len(summary.historical_counts) == 1
    assert summary.historical_counts[0].count == 2


def test_summarize_alerts_collapses_many_historical_duties_into_one_count_per_rule_code():
    alerts = [_alert("D9.1.3_28_DAY_DUTY_LIMIT", AlertStatus.ILLEGAL, duty_id=f"OLD-A-{i}")
              for i in range(150)]
    alerts += [_alert("D9.2.3_12_MONTH_FLIGHT_TIME_LIMIT", AlertStatus.ILLEGAL, duty_id=f"OLD-B-{i}")
               for i in range(150)]
    alerts.append(_alert("D25_NUTRITION_DATA_MISSING", AlertStatus.NEEDS_MANUAL_REVIEW, duty_id="NEW"))

    summary = summarize_alerts(_vr(alerts), target_duty_id="NEW")

    assert len(summary.target_duty_alerts) == 1
    counts = {hc.rule_code: hc.count for hc in summary.historical_counts}
    assert counts == {"D9.1.3_28_DAY_DUTY_LIMIT": 150, "D9.2.3_12_MONTH_FLIGHT_TIME_LIMIT": 150}


def test_qualification_alerts_are_not_conflated_with_schedule_level_alerts():
    schedule_alert = _alert("D23.2_SEVENTH_DAY_OFF", AlertStatus.ILLEGAL, duty_id=None)
    qualification_alert = _alert(f"{QUALIFICATION_RULE_PREFIX}_MEDICAL_EXPIRED",
                                  AlertStatus.ILLEGAL, duty_id=None)

    summary = summarize_alerts(_vr([schedule_alert, qualification_alert]), target_duty_id="NEW")

    assert summary.schedule_level_alerts == [schedule_alert]
    assert summary.qualification_alerts == [qualification_alert]


# ------------------------------------------------------------------
# blocked_by_history_only — the conservative 4-bucket gate
# ------------------------------------------------------------------

def test_blocked_by_history_only_true_when_all_illegal_is_historical():
    alerts = [
        _alert("D9.1.3_28_DAY_DUTY_LIMIT", AlertStatus.ILLEGAL, duty_id="OLD-1"),
        _alert("D25_NUTRITION_DATA_MISSING", AlertStatus.WARNING, duty_id="NEW"),
    ]
    summary = summarize_alerts(_vr(alerts), target_duty_id="NEW")

    assert summary.overall_status == AlertStatus.ILLEGAL
    assert summary.blocked_by_history_only is True


def test_blocked_by_history_only_false_when_target_duty_has_its_own_illegal():
    alerts = [
        _alert("D9.1.3_28_DAY_DUTY_LIMIT", AlertStatus.ILLEGAL, duty_id="OLD-1"),
        _alert("D21.1_CHARTER_AERIAL_WORK_ABOVE_5700KG_REST", AlertStatus.ILLEGAL, duty_id="NEW"),
    ]
    summary = summarize_alerts(_vr(alerts), target_duty_id="NEW")

    assert summary.blocked_by_history_only is False


def test_blocked_by_history_only_false_for_schedule_level_illegal_alone():
    """The confirmed-conservative-default regression test: this duty
    genuinely could be the one pushing a 6-day streak to a 7th —
    never claim 'blocked by history' for it."""
    alerts = [_alert("D23.2_SEVENTH_DAY_OFF", AlertStatus.ILLEGAL, duty_id=None)]
    summary = summarize_alerts(_vr(alerts), target_duty_id="NEW")

    assert summary.overall_status == AlertStatus.ILLEGAL
    assert summary.blocked_by_history_only is False


def test_blocked_by_history_only_false_for_qualification_illegal_alone():
    """The concrete regression test for the 4th-bucket fix: an
    expired license/medical must never read as 'blocked by
    pre-existing history, this duty is not the cause' — that's
    exactly backwards."""
    alerts = [_alert(f"{QUALIFICATION_RULE_PREFIX}_LICENSE_EXPIRED", AlertStatus.ILLEGAL, duty_id=None)]
    summary = summarize_alerts(_vr(alerts), target_duty_id="NEW")

    assert summary.overall_status == AlertStatus.ILLEGAL
    assert summary.blocked_by_history_only is False


def test_blocked_by_history_only_false_when_overall_status_not_illegal():
    for status in (AlertStatus.WARNING, AlertStatus.NEEDS_MANUAL_REVIEW, AlertStatus.LEGAL):
        alerts = [] if status == AlertStatus.LEGAL else [
            _alert("SOME_RULE", status, duty_id="NEW")
        ]
        summary = summarize_alerts(_vr(alerts), target_duty_id="NEW")
        assert summary.blocked_by_history_only is False, f"failed for {status}"


# ------------------------------------------------------------------
# build_audit_reason()
# ------------------------------------------------------------------

def test_build_audit_reason_filters_by_status_and_summarizes_historical():
    summary = AlertSummary(
        overall_status=AlertStatus.ILLEGAL,
        target_duty_alerts=[
            _alert("D21.1_CHARTER_AERIAL_WORK_ABOVE_5700KG_REST", AlertStatus.ILLEGAL,
                   duty_id="NEW", message="M1 illegal"),
            _alert("D25_NUTRITION_DATA_MISSING", AlertStatus.NEEDS_MANUAL_REVIEW,
                   duty_id="NEW", message="M2 review"),
        ],
        historical_counts=[
            HistoricalAlertCount(rule_code="D9.1.3_28_DAY_DUTY_LIMIT", status=AlertStatus.ILLEGAL,
                                  severity="RED", count=300, example_message="M3 historical"),
        ],
    )

    illegal_reason = build_audit_reason(summary, frozenset({AlertStatus.ILLEGAL}))
    assert "M1 illegal" in illegal_reason
    assert "M3 historical" in illegal_reason
    assert "D9.1.3_28_DAY_DUTY_LIMIT x300" in illegal_reason
    assert "M2 review" not in illegal_reason

    review_reason = build_audit_reason(summary, frozenset({AlertStatus.NEEDS_MANUAL_REVIEW}))
    assert review_reason == "M2 review"


def test_build_audit_reason_returns_none_when_nothing_matches():
    summary = AlertSummary(
        overall_status=AlertStatus.WARNING,
        target_duty_alerts=[_alert("D25_NUTRITION_DATA_MISSING", AlertStatus.WARNING, duty_id="NEW")],
    )
    assert build_audit_reason(summary, frozenset({AlertStatus.ILLEGAL})) is None


# ------------------------------------------------------------------
# format_alert_lines()
# ------------------------------------------------------------------

def test_format_alert_lines_omits_empty_sections_and_includes_blocked_by_history_message():
    summary = AlertSummary(
        overall_status=AlertStatus.ILLEGAL,
        historical_counts=[
            HistoricalAlertCount(rule_code="D9.1.3_28_DAY_DUTY_LIMIT", status=AlertStatus.ILLEGAL,
                                  severity="RED", count=187, example_message="breached"),
        ],
        blocked_by_history_only=True,
    )
    lines = format_alert_lines(summary)

    assert "**This duty:**" not in lines
    assert "**Crew qualification:**" not in lines
    assert "**Duty pattern (7th-day-off / mandatory days off):**" not in lines
    assert any("D9.1.3_28_DAY_DUTY_LIMIT" in line and "187" in line for line in lines)
    assert (
        "⚠️ Blocked by pre-existing breaches in this crew member's "
        "history — this duty itself is not the cause."
    ) in lines


def test_format_alert_lines_no_blocked_message_when_not_blocked_by_history_only():
    summary = AlertSummary(
        overall_status=AlertStatus.ILLEGAL,
        target_duty_alerts=[_alert("D21.1_CHARTER_AERIAL_WORK_ABOVE_5700KG_REST",
                                    AlertStatus.ILLEGAL, duty_id="NEW")],
        blocked_by_history_only=False,
    )
    lines = format_alert_lines(summary)

    assert not any("Blocked by pre-existing breaches" in line for line in lines)
    assert any("D21.1_CHARTER_AERIAL_WORK_ABOVE_5700KG_REST" in line for line in lines)
