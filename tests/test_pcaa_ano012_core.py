"""
tests/test_pcaa_ano012_core.py

Pure logic — no database needed. Focused on the rules confirmed
applicable to Air Eagle right now: D21.1 charter rest (confirmed
2026-07-19 — Air Eagle's cargo ops are classified as charter, not
scheduled home/away-base), the D8.2.1 FDP table (the exact area of
the historical "2.2h instead of 13h" bug), and D9 cumulative limits
(the highest-risk area for a small crew pool).

D20 (home/away base rest) and D17/D18/D19/D24 (split duty, standby,
reserve, LR/ELR/ULR) are exercised only lightly here since they're
not the currently-confirmed operating model — extend this file when
the route network clarifies whether/how they apply.
"""
import sys
from pathlib import Path
import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.legality.pcaa_ano012_core import (
    ANO012CoreValidator,
    CrewMember,
    Duty,
    Sector,
    DutyType,
    AcclimatizationState,
    AlertStatus,
)

validator = ANO012CoreValidator()


def _crew():
    return CrewMember(crew_id="CPT-01", name="Test Captain", home_base="KHI")


def _duty(start_hour, start_minute, duration_hours, sectors=1, operation_type="cargo_charter"):
    """Build a minimal single-sector FDP duty starting at a given
    local time (KHI, UTC+5), for a given duration."""
    start_utc = dt.datetime(2026, 7, 20, start_hour, start_minute) - dt.timedelta(hours=5)
    end_utc = start_utc + dt.timedelta(hours=duration_hours)
    return Duty(
        duty_type=DutyType.FDP,
        start_utc=start_utc,
        end_utc=end_utc,
        crew_id="CPT-01",
        duty_id="D-1",
        report_location="KHI",
        home_base="KHI",
        operation_type=operation_type,
        sectors=[Sector(
            departure_utc=start_utc + dt.timedelta(minutes=45),
            arrival_utc=end_utc - dt.timedelta(minutes=15),
            origin="KHI", destination="LHE",
        )] * sectors,
    )


# ------------------------------------------------------------------
# D8.2.1 FDP TABLE - the exact area of the historical "2.2h instead
# of 13h" bug (block time used instead of report-to-debrief FDP)
# ------------------------------------------------------------------

def test_fdp_table_0600_1459_single_sector():
    minutes, rule_code = validator.get_max_fdp_minutes(
        duty_start_utc=dt.datetime(2026, 7, 20, 5, 0),  # 10:00 KHI local (UTC+5)
        report_tz_offset=5.0,
        sectors=1,
        acclimatization_state=AcclimatizationState.B,
    )
    assert minutes == 13 * 60
    assert rule_code == "D8.2.1_TABLE_2_0600_1459"


def test_fdp_table_overnight_band_wraps_midnight_correctly():
    """1630-0459 is the overnight band and must correctly wrap past
    midnight - this is the kind of edge case that's easy to get
    wrong with naive hour comparisons."""
    minutes, rule_code = validator.get_max_fdp_minutes(
        duty_start_utc=dt.datetime(2026, 7, 20, 20, 0),  # 01:00 KHI local next day
        report_tz_offset=5.0,
        sectors=1,
        acclimatization_state=AcclimatizationState.B,
    )
    assert minutes == 11 * 60
    assert rule_code == "D8.2.1_TABLE_2_1630_0459"


def test_fdp_table_more_sectors_reduces_limit():
    minutes_1, _ = validator.get_max_fdp_minutes(
        dt.datetime(2026, 7, 20, 5, 0), 5.0, sectors=1,
        acclimatization_state=AcclimatizationState.B)
    minutes_4, _ = validator.get_max_fdp_minutes(
        dt.datetime(2026, 7, 20, 5, 0), 5.0, sectors=4,
        acclimatization_state=AcclimatizationState.B)
    assert minutes_4 < minutes_1


def test_fdp_table_unknown_acclimatization_uses_table_3():
    minutes, rule_code = validator.get_max_fdp_minutes(
        duty_start_utc=dt.datetime(2026, 7, 20, 5, 0),
        report_tz_offset=5.0,
        sectors=1,
        acclimatization_state=AcclimatizationState.X,
    )
    assert rule_code == "D8.2.2_TABLE_3_UNKNOWN"
    assert minutes == 11 * 60


# ------------------------------------------------------------------
# D21.1 CHARTER REST - confirmed applicable to Air Eagle 2026-07-19
# required rest = max(12h, 2 x previous FDP)
# ------------------------------------------------------------------

def test_charter_rest_is_max_12h_or_2x_fdp_short_duty():
    """A short previous duty (e.g. 4h FDP) -> 2x FDP = 8h, which is
    less than the 12h floor. Required rest must be 12h, not 8h."""
    crew = _crew()
    prev_duty = _duty(5, 0, duration_hours=4)   # 4h FDP
    next_duty = _duty(5, 0, duration_hours=6)
    required_minutes, rule_code = validator.required_rest_minutes(prev_duty, next_duty, crew)
    assert rule_code == "D21.1_CHARTER_AERIAL_WORK_ABOVE_5700KG_REST"
    assert required_minutes == 12 * 60


def test_charter_rest_scales_above_12h_floor_for_long_duty():
    """A long previous duty (8h FDP) -> 2x FDP = 16h, which exceeds
    the 12h floor. Required rest must be 16h, not capped at 12h."""
    crew = _crew()
    prev_duty = _duty(5, 0, duration_hours=8)   # 8h FDP
    next_duty = _duty(5, 0, duration_hours=6)
    required_minutes, rule_code = validator.required_rest_minutes(prev_duty, next_duty, crew)
    assert required_minutes == 16 * 60


def test_insufficient_charter_rest_flagged_illegal():
    crew = _crew()
    prev_duty = _duty(5, 0, duration_hours=8)  # requires 16h rest after
    prev_duty.duty_id = "D-1"
    next_duty = _duty(5, 0, duration_hours=6)
    next_duty.duty_id = "D-2"
    # Only 10h rest between duties - insufficient (needs 16h)
    next_duty.start_utc = prev_duty.end_utc + dt.timedelta(hours=10)
    next_duty.end_utc = next_duty.start_utc + dt.timedelta(hours=6)

    result = validator.validate_schedule(crew, [prev_duty, next_duty])
    rest_alerts = [a for a in result.alerts if "REST" in a.rule_code]
    assert len(rest_alerts) == 1
    assert rest_alerts[0].status == AlertStatus.ILLEGAL


def test_sufficient_charter_rest_passes():
    crew = _crew()
    prev_duty = _duty(5, 0, duration_hours=8)  # requires 16h rest after
    prev_duty.duty_id = "D-1"
    next_duty = _duty(5, 0, duration_hours=6)
    next_duty.duty_id = "D-2"
    next_duty.start_utc = prev_duty.end_utc + dt.timedelta(hours=17)
    next_duty.end_utc = next_duty.start_utc + dt.timedelta(hours=6)

    result = validator.validate_schedule(crew, [prev_duty, next_duty])
    rest_alerts = [a for a in result.alerts if "REST" in a.rule_code]
    assert len(rest_alerts) == 0


# ------------------------------------------------------------------
# D9 CUMULATIVE LIMITS - highest-risk area for a small crew pool
# ------------------------------------------------------------------

def test_28_day_duty_limit_breach_detected():
    """190h limit over 28 days. Stack enough duties to exceed it and
    confirm the engine actually catches it (not just defines the
    number, but applies it)."""
    crew = _crew()
    duties = []
    base_start = dt.datetime(2026, 7, 1, 0, 0)
    for i in range(20):  # 20 duties x 10h = 200h, over the 190h/28d limit
        start = base_start + dt.timedelta(days=i)
        d = Duty(
            duty_type=DutyType.FDP,
            start_utc=start,
            end_utc=start + dt.timedelta(hours=10),
            crew_id="CPT-01",
            duty_id=f"D-{i}",
            report_location="KHI",
            home_base="KHI",
            sectors=[Sector(
                departure_utc=start + dt.timedelta(minutes=45),
                arrival_utc=start + dt.timedelta(hours=10) - dt.timedelta(minutes=15),
                origin="KHI", destination="LHE",
            )],
        )
        duties.append(d)

    result = validator.validate_schedule(crew, duties)
    breach_alerts = [a for a in result.alerts if "D9.1.3_28_DAY_DUTY_LIMIT" in a.rule_code]
    assert len(breach_alerts) > 0
    assert breach_alerts[0].status == AlertStatus.ILLEGAL


def test_well_within_cumulative_limits_produces_no_d9_alerts():
    crew = _crew()
    duties = []
    base_start = dt.datetime(2026, 7, 1, 0, 0)
    for i in range(3):  # 3 duties x 6h, well within any window
        start = base_start + dt.timedelta(days=i * 3)
        d = Duty(
            duty_type=DutyType.FDP,
            start_utc=start,
            end_utc=start + dt.timedelta(hours=6),
            crew_id="CPT-01",
            duty_id=f"D-{i}",
            report_location="KHI",
            home_base="KHI",
            sectors=[Sector(
                departure_utc=start + dt.timedelta(minutes=45),
                arrival_utc=start + dt.timedelta(hours=6) - dt.timedelta(minutes=15),
                origin="KHI", destination="LHE",
            )],
        )
        duties.append(d)

    result = validator.validate_schedule(crew, duties)
    d9_alerts = [a for a in result.alerts if a.rule_code.startswith("D9.")]
    assert len(d9_alerts) == 0
    assert result.status == AlertStatus.LEGAL


# ------------------------------------------------------------------
# ValidationResult status aggregation
# ------------------------------------------------------------------

def test_validation_result_is_legal_true_for_warning_status():
    """WARNING should still be considered legal (informational), only
    ILLEGAL should not be."""
    crew = _crew()
    duty = _duty(5, 0, duration_hours=6)
    result = validator.validate_schedule(crew, [duty])
    assert result.status in (AlertStatus.LEGAL, AlertStatus.WARNING)
    assert result.is_legal is True


def test_as_dict_roundtrips_cleanly():
    crew = _crew()
    duty = _duty(5, 0, duration_hours=6)
    result = validator.validate_schedule(crew, [duty])
    d = result.as_dict()
    assert "status" in d
    assert "legal" in d
    assert isinstance(d["alerts"], list)
