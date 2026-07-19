"""
FTLguard / K2 — PCAA ANO-012-FSXX Version 7.0 Core Rule Engine

Purpose:
- Pure deterministic FTL/FDTL rule calculations.
- No Streamlit.
- No database queries.
- No K2 UI logic.
- No silent AI decision-making.

This module is the reusable regulatory core.
The existing utils/ftl_validator.py should later map K2 database rows into these objects.

Rule source:
Pakistan Civil Aviation Authority
ANO-012-FSXX Version 7.0
Fatigue Management – Flight and Cabin Crew
Effective: 01-01-2025
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# ENUMS
# ============================================================

class DutyType(str, Enum):
    FDP = "FDP"
    AIRPORT_STANDBY = "AIRPORT_STANDBY"
    OTHER_STANDBY = "OTHER_STANDBY"
    RESERVE = "RESERVE"
    POSITIONING = "POSITIONING"
    TRAINING_SIM = "TRAINING_SIM"
    TRAINING_AIRCRAFT = "TRAINING_AIRCRAFT"
    ADMIN = "ADMIN"
    REST = "REST"
    DAY_OFF = "DAY_OFF"


class AcclimatizationState(str, Enum):
    B = "B"  # acclimatized to reference/departure time zone
    D = "D"  # acclimatized to local time where next duty starts
    X = "X"  # unknown acclimatization state


class AlertStatus(str, Enum):
    LEGAL = "LEGAL"
    WARNING = "WARNING"
    ILLEGAL = "ILLEGAL"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class RestFacilityClass(str, Enum):
    CLASS_1 = "CLASS_1"
    CLASS_2 = "CLASS_2"
    CLASS_3 = "CLASS_3"


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Sector:
    departure_utc: dt.datetime
    arrival_utc: dt.datetime
    departure_tz_offset: float = 5.0
    arrival_tz_offset: float = 5.0
    origin: str = ""
    destination: str = ""
    is_positioning: bool = False
    counts_as_fdp_sector: bool = False
    is_technical_landing: bool = False
    is_ferry: bool = False
    is_retrieval_after_diversion: bool = False
    is_night_landing: bool = False

    @property
    def flight_time_minutes(self) -> int:
        return max(0, int((self.arrival_utc - self.departure_utc).total_seconds() // 60))


@dataclass
class Duty:
    duty_type: DutyType
    start_utc: dt.datetime
    end_utc: dt.datetime
    sectors: List[Sector] = field(default_factory=list)

    crew_id: Optional[str] = None
    duty_id: Optional[str] = None
    flight_id: Optional[Any] = None

    report_location: str = ""
    report_tz_offset: float = 5.0
    home_base: str = ""

    operation_type: str = "cargo_charter"
    aircraft_weight_above_5700kg: bool = True

    # FDP extension / approval fields
    planned_extension_without_inflight_rest_minutes: int = 0
    pic_discretion_minutes: int = 0
    pic_discretion_reason: str = ""
    accountable_manager_extension_minutes: int = 0
    dfs_authorization_recorded: bool = False

    # In-flight rest / augmented crew
    is_augmented: bool = False
    additional_flight_crew: int = 0
    in_flight_rest_used: bool = False
    rest_facility_class: Optional[RestFacilityClass] = None
    in_flight_rest_minutes_each: int = 0
    in_flight_rest_control_crew_minutes: int = 0

    # Split duty
    is_split_duty: bool = False
    split_break_start_utc: Optional[dt.datetime] = None
    split_break_end_utc: Optional[dt.datetime] = None
    split_break_has_suitable_accommodation: bool = False
    follows_reduced_rest: bool = False

    # Standby / reserve
    standby_notified_start_utc: Optional[dt.datetime] = None
    standby_notified_end_utc: Optional[dt.datetime] = None
    call_time_utc: Optional[dt.datetime] = None

    # LR / ELR / ULR
    is_lr: bool = False
    is_elr: bool = False
    is_ulr: bool = False

    # Operational support flags
    snack_provided: Optional[bool] = None
    meal_provided: Optional[bool] = None
    fitness_for_duty_confirmed: Optional[bool] = None
    fasting_risk_declared: Optional[bool] = None
    license_exam_on_duty_day: Optional[bool] = None

    @property
    def duration_minutes(self) -> int:
        return max(0, int((self.end_utc - self.start_utc).total_seconds() // 60))

    @property
    def fdp_minutes(self) -> int:
        # For FDP, start/end must already represent report-to-postflight release.
        return self.duration_minutes

    @property
    def sector_count(self) -> int:
        if self.duty_type != DutyType.FDP:
            return 0
        count = len(self.sectors)
        count += sum(1 for s in self.sectors if s.is_positioning and s.counts_as_fdp_sector)
        return max(count, 1)

    @property
    def flight_time_minutes(self) -> int:
        return sum(s.flight_time_minutes for s in self.sectors if not s.is_positioning)

    @property
    def start_local(self) -> dt.datetime:
        return self.start_utc + dt.timedelta(hours=self.report_tz_offset)

    @property
    def end_local(self) -> dt.datetime:
        return self.end_utc + dt.timedelta(hours=self.report_tz_offset)


@dataclass
class CrewMember:
    crew_id: str
    name: str
    home_base: str
    home_base_tz_offset: float = 5.0


@dataclass
class RuleAlert:
    rule_code: str
    status: AlertStatus
    severity: str
    message: str
    calculated_value: Any = None
    required_limit: Any = None
    duty_index: Optional[int] = None
    duty_id: Optional[str] = None
    flight_id: Optional[Any] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "status": self.status.value,
            "severity": self.severity,
            "message": self.message,
            "calculated_value": self.calculated_value,
            "required_limit": self.required_limit,
            "duty_index": self.duty_index,
            "duty_id": self.duty_id,
            "flight_id": self.flight_id,
            "rule_source": "PCAA ANO-012-FSXX Version 7.0",
        }


@dataclass
class ValidationResult:
    status: AlertStatus = AlertStatus.LEGAL
    alerts: List[RuleAlert] = field(default_factory=list)
    computed: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_legal(self) -> bool:
        return self.status in {AlertStatus.LEGAL, AlertStatus.WARNING}

    def add_alert(self, alert: RuleAlert) -> None:
        self.alerts.append(alert)

        if alert.status == AlertStatus.ILLEGAL:
            self.status = AlertStatus.ILLEGAL
        elif alert.status == AlertStatus.NEEDS_MANUAL_REVIEW and self.status != AlertStatus.ILLEGAL:
            self.status = AlertStatus.NEEDS_MANUAL_REVIEW
        elif alert.status == AlertStatus.WARNING and self.status == AlertStatus.LEGAL:
            self.status = AlertStatus.WARNING

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "legal": self.is_legal,
            "alerts": [a.as_dict() for a in self.alerts],
            "computed": self.computed,
        }


# ============================================================
# CORE VALIDATOR
# ============================================================

class ANO012CoreValidator:
    """
    Pure PCAA ANO-012-FSXX v7.0 validator.

    Important:
    - This class does not read from database.
    - This class does not know Streamlit.
    - K2-specific adapter will map database rows into Duty/Sector objects.
    """

    def __init__(
        self,
        has_approved_frm: bool = False,
        default_operation_type: str = "cargo_charter",
    ):
        self.has_approved_frm = has_approved_frm
        self.default_operation_type = default_operation_type

    # --------------------------------------------------------
    # PUBLIC API
    # --------------------------------------------------------

    def validate_schedule(self, crew: CrewMember, duties: List[Duty]) -> ValidationResult:
        result = ValidationResult()

        duties = sorted(duties, key=lambda d: d.start_utc)

        self._check_overlaps(duties, result)
        self._check_duties(crew, duties, result)
        self._check_rest_between_duties(crew, duties, result)
        self._check_cumulative_limits(duties, result)
        self._check_seventh_day_off(duties, result)
        self._check_mandatory_days_off(duties, result)
        self._check_lr_elr_ulr_intervals(duties, result)

        return result

    # --------------------------------------------------------
    # D8 FDP TABLES
    # --------------------------------------------------------

    def get_max_fdp_minutes(
        self,
        duty_start_utc: dt.datetime,
        report_tz_offset: float,
        sectors: int,
        acclimatization_state: AcclimatizationState,
    ) -> Tuple[Optional[int], str]:
        sectors = max(1, int(sectors))

        if acclimatization_state == AcclimatizationState.X:
            if self.has_approved_frm:
                return self._table4_unknown_acclimatization_frm(sectors), "D8.2.3_TABLE_4_UNKNOWN_WITH_FRM"
            return self._table3_unknown_acclimatization(sectors), "D8.2.2_TABLE_3_UNKNOWN"

        local_start = duty_start_utc + dt.timedelta(hours=report_tz_offset)
        hhmm = local_start.hour * 100 + local_start.minute

        return self._table2_acclimatized(hhmm, sectors)

    def _table2_acclimatized(self, hhmm: int, sectors: int) -> Tuple[Optional[int], str]:
        """
        D8.2.1 Table 2 — acclimatized crew.
        Correctly handles overnight band 1630–0459.
        Returns minutes and rule code.
        """

        # 0600–1459
        if 600 <= hhmm <= 1459:
            limits = {
                1: 13 * 60,
                2: 13 * 60,
                3: 12 * 60 + 30,
                4: 12 * 60,
                5: 11 * 60 + 30,
                6: 11 * 60,
            }
            if sectors in limits:
                return limits[sectors], "D8.2.1_TABLE_2_0600_1459"
            return None, "D8.2.1_TABLE_2_7_SECTORS_REQUIRES_PCAA_APPROVAL"

        # 1500–1629
        if 1500 <= hhmm <= 1629:
            limits = {
                1: 12 * 60,
                2: 12 * 60,
                3: 11 * 60 + 30,
                4: 11 * 60,
                5: 10 * 60 + 30,
                6: 10 * 60,
            }
            if sectors in limits:
                return limits[sectors], "D8.2.1_TABLE_2_1500_1629"
            return None, "D8.2.1_TABLE_2_7_SECTORS_REQUIRES_PCAA_APPROVAL"

        # 1630–0459 — overnight band
        if hhmm >= 1630 or hhmm <= 459:
            limits = {
                1: 11 * 60,
                2: 11 * 60,
                3: 10 * 60 + 30,
                4: 10 * 60,
            }
            if sectors in limits:
                return limits[sectors], "D8.2.1_TABLE_2_1630_0459"
            return None, "D8.2.1_TABLE_2_PRIOR_PCAA_APPROVAL_REQUIRED"

        # 0500–0559
        if 500 <= hhmm <= 559:
            limits = {
                1: 12 * 60,
                2: 12 * 60,
                3: 11 * 60 + 30,
                4: 11 * 60,
                5: 10 * 60 + 30,
                6: 10 * 60,
            }
            if sectors in limits:
                return limits[sectors], "D8.2.1_TABLE_2_0500_0559"
            return None, "D8.2.1_TABLE_2_SECTOR_COUNT_NOT_ALLOWED"

        return None, "D8.2.1_TABLE_2_START_TIME_UNRECOGNIZED"

    def _table3_unknown_acclimatization(self, sectors: int) -> int:
        limits = {
            1: 11 * 60,
            2: 11 * 60,
            3: 10 * 60 + 30,
            4: 10 * 60,
            5: 9 * 60 + 30,
            6: 9 * 60,
            7: 9 * 60,
            8: 9 * 60,
        }
        return limits.get(sectors, 9 * 60)

    def _table4_unknown_acclimatization_frm(self, sectors: int) -> int:
        limits = {
            1: 12 * 60,
            2: 12 * 60,
            3: 11 * 60 + 30,
            4: 11 * 60,
            5: 10 * 60 + 30,
            6: 10 * 60,
            7: 9 * 60 + 30,
            8: 9 * 60,
        }
        return limits.get(sectors, 9 * 60)

    # --------------------------------------------------------
    # D1.1 ACCLIMATIZATION
    # --------------------------------------------------------

    def determine_acclimatization_state(
        self,
        previous_duty: Optional[Duty],
        current_duty: Duty,
    ) -> AcclimatizationState:
        """
        D1.1 Table 1.
        Corrected from the KIMI suggestion:
        For <4h time difference, elapsed <48h remains B, not always D.
        """

        if previous_duty is None:
            return AcclimatizationState.B

        time_diff = abs(current_duty.report_tz_offset - previous_duty.report_tz_offset)

        if time_diff <= 2:
            return AcclimatizationState.B

        elapsed_hours = (current_duty.start_utc - previous_duty.start_utc).total_seconds() / 3600

        if time_diff < 4:
            if elapsed_hours < 48:
                return AcclimatizationState.B
            return AcclimatizationState.D

        if 4 <= time_diff <= 6:
            if elapsed_hours < 48:
                return AcclimatizationState.B
            if elapsed_hours < 72:
                return AcclimatizationState.X
            return AcclimatizationState.D

        if 6 < time_diff <= 9:
            if elapsed_hours < 48:
                return AcclimatizationState.B
            if elapsed_hours < 96:
                return AcclimatizationState.X
            return AcclimatizationState.D

        if 9 < time_diff <= 12:
            if elapsed_hours < 48:
                return AcclimatizationState.B
            if elapsed_hours < 120:
                return AcclimatizationState.X
            return AcclimatizationState.D

        # More than 12h is not directly table-covered; use conservative unknown.
        if elapsed_hours >= 120:
            return AcclimatizationState.D
        return AcclimatizationState.X

    # --------------------------------------------------------
    # DUTY VALIDATION
    # --------------------------------------------------------

    def _check_duties(self, crew: CrewMember, duties: List[Duty], result: ValidationResult) -> None:
        for idx, duty in enumerate(duties):
            previous_duty = self._previous_operational_duty(duties, idx)
            if duty.duty_type == DutyType.FDP:
                self._check_fdp(crew, duty, previous_duty, idx, result)
            elif duty.duty_type == DutyType.AIRPORT_STANDBY:
                self._check_airport_standby(duty, idx, result)
            elif duty.duty_type == DutyType.OTHER_STANDBY:
                self._check_other_standby(duty, idx, result)
            elif duty.duty_type == DutyType.RESERVE:
                self._check_reserve(duty, idx, result)
            elif duty.duty_type == DutyType.POSITIONING:
                self._check_positioning(duty, idx, result)
            elif duty.duty_type in {DutyType.TRAINING_SIM, DutyType.TRAINING_AIRCRAFT}:
                self._check_training(duty, idx, result)

    def _check_fdp(
        self,
        crew: CrewMember,
        duty: Duty,
        previous_duty: Optional[Duty],
        idx: int,
        result: ValidationResult,
    ) -> None:
        acclim_state = self.determine_acclimatization_state(previous_duty, duty)
        max_fdp_minutes, rule_code = self.get_max_fdp_minutes(
            duty.start_utc,
            duty.report_tz_offset,
            duty.sector_count,
            acclim_state,
        )

        if max_fdp_minutes is None:
            result.add_alert(RuleAlert(
                rule_code=rule_code,
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message=(
                    f"FDP start/sector combination requires PCAA/operator approval. "
                    f"Start local {duty.start_local.strftime('%H:%M')}, sectors {duty.sector_count}."
                ),
                calculated_value=f"{duty.start_local.strftime('%H:%M')} / {duty.sector_count} sectors",
                required_limit="PCAA/operator approval required",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))
            return

        allowed_minutes = max_fdp_minutes

        # Planned extension without in-flight rest is separate from PIC discretion.
        if duty.planned_extension_without_inflight_rest_minutes:
            self._check_planned_extension_without_inflight_rest(duty, idx, result)
            allowed_minutes += duty.planned_extension_without_inflight_rest_minutes

        # In-flight rest extension.
        if duty.in_flight_rest_used:
            extension = self._check_inflight_rest_extension(duty, idx, result)
            allowed_minutes = max(allowed_minutes, extension)

        # Split duty extension.
        if duty.is_split_duty:
            split_extension = self._check_split_duty_extension(duty, idx, result)
            allowed_minutes += split_extension

        # Illegal combinations.
        if duty.is_split_duty and duty.in_flight_rest_used:
            result.add_alert(RuleAlert(
                rule_code="D17.4.8_SPLIT_DUTY_INFLIGHT_REST_COMBINATION",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Split duty cannot be combined with in-flight rest.",
                calculated_value="Split duty + in-flight rest",
                required_limit="Not permitted",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        # PIC discretion is not normal planning authority.
        if duty.pic_discretion_minutes:
            max_pic = 180 if duty.is_augmented else 120
            if duty.pic_discretion_minutes > max_pic:
                result.add_alert(RuleAlert(
                    rule_code="D8.2.6.1.1_PIC_DISCRETION_EXCEEDED",
                    status=AlertStatus.ILLEGAL,
                    severity="RED",
                    message=(
                        f"PIC discretion {duty.pic_discretion_minutes} minutes exceeds "
                        f"maximum {max_pic} minutes."
                    ),
                    calculated_value=duty.pic_discretion_minutes,
                    required_limit=max_pic,
                    duty_index=idx,
                    duty_id=duty.duty_id,
                    flight_id=duty.flight_id,
                ))
            else:
                allowed_minutes += duty.pic_discretion_minutes
                result.add_alert(RuleAlert(
                    rule_code="D8.2.6_PIC_DISCRETION_RECORDED",
                    status=AlertStatus.WARNING,
                    severity="YELLOW",
                    message="PIC discretion used. Report/regularization workflow must be completed where applicable.",
                    calculated_value=duty.pic_discretion_minutes,
                    required_limit=max_pic,
                    duty_index=idx,
                    duty_id=duty.duty_id,
                    flight_id=duty.flight_id,
                ))

        if duty.accountable_manager_extension_minutes:
            result.add_alert(RuleAlert(
                rule_code="D15.4_ACCOUNTABLE_MANAGER_EXTENSION",
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message="Accountable Manager extension requires documented approval/regularization.",
                calculated_value=duty.accountable_manager_extension_minutes,
                required_limit="Approval record required",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.fdp_minutes > allowed_minutes:
            result.add_alert(RuleAlert(
                rule_code=rule_code,
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message=(
                    f"FDP {self._fmt_minutes(duty.fdp_minutes)} exceeds allowed "
                    f"{self._fmt_minutes(allowed_minutes)}."
                ),
                calculated_value=self._fmt_minutes(duty.fdp_minutes),
                required_limit=self._fmt_minutes(allowed_minutes),
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        self._check_night_duty(duty, idx, result)
        self._check_augmented_sector_limit(duty, idx, result)
        self._check_nutrition(duty, idx, result)
        self._check_license_exam_and_fitness(duty, idx, result)

    # --------------------------------------------------------
    # EXTENSIONS
    # --------------------------------------------------------

    def _check_planned_extension_without_inflight_rest(
        self,
        duty: Duty,
        idx: int,
        result: ValidationResult,
    ) -> None:
        if duty.planned_extension_without_inflight_rest_minutes > 60:
            result.add_alert(RuleAlert(
                rule_code="D8.2.4.1_PLANNED_EXTENSION_EXCEEDS_1H",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Planned FDP extension without in-flight rest cannot exceed 1 hour.",
                calculated_value=duty.planned_extension_without_inflight_rest_minutes,
                required_limit=60,
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.in_flight_rest_used or duty.is_split_duty:
            result.add_alert(RuleAlert(
                rule_code="D8.2.4.4_EXTENSION_COMBINATION_NOT_ALLOWED",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Extension without in-flight rest must not be combined with in-flight rest or split duty.",
                calculated_value="Combined extension types",
                required_limit="Not permitted",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_inflight_rest_extension(self, duty: Duty, idx: int, result: ValidationResult) -> int:
        if duty.sector_count > 3:
            result.add_alert(RuleAlert(
                rule_code="D15.2.1_INFLIGHT_REST_MAX_3_SECTORS",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="FDP extended due to in-flight rest is limited to 3 sectors.",
                calculated_value=duty.sector_count,
                required_limit=3,
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.rest_facility_class is None:
            result.add_alert(RuleAlert(
                rule_code="D15.2.3_REST_FACILITY_CLASS_MISSING",
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message="In-flight rest extension requires recorded rest facility class.",
                calculated_value="Missing",
                required_limit="Class 1, 2, or 3",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))
            return 0

        if duty.in_flight_rest_minutes_each < 90:
            result.add_alert(RuleAlert(
                rule_code="D15.2.2_MIN_INFLIGHT_REST",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Minimum in-flight rest is 90 consecutive minutes for each crew member.",
                calculated_value=duty.in_flight_rest_minutes_each,
                required_limit=90,
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.in_flight_rest_control_crew_minutes < 120:
            result.add_alert(RuleAlert(
                rule_code="D15.2.2_CONTROL_CREW_REST",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Flight crew at controls during landing require 2 consecutive hours of in-flight rest.",
                calculated_value=duty.in_flight_rest_control_crew_minutes,
                required_limit=120,
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        # D15.2.3 maximum FDP values.
        if duty.additional_flight_crew == 1:
            values = {
                RestFacilityClass.CLASS_3: 14 * 60,
                RestFacilityClass.CLASS_2: 15 * 60,
                RestFacilityClass.CLASS_1: 16 * 60,
            }
            return values[duty.rest_facility_class]

        if duty.additional_flight_crew == 2:
            values = {
                RestFacilityClass.CLASS_3: 15 * 60,
                RestFacilityClass.CLASS_2: 16 * 60,
                RestFacilityClass.CLASS_1: 17 * 60,
            }
            return values[duty.rest_facility_class]

        result.add_alert(RuleAlert(
            rule_code="D15.2.3_ADDITIONAL_CREW_REQUIRED",
            status=AlertStatus.ILLEGAL,
            severity="RED",
            message="In-flight rest extension requires one or two additional flight crew members.",
            calculated_value=duty.additional_flight_crew,
            required_limit="1 or 2",
            duty_index=idx,
            duty_id=duty.duty_id,
            flight_id=duty.flight_id,
        ))
        return 0

    def _check_split_duty_extension(self, duty: Duty, idx: int, result: ValidationResult) -> int:
        if duty.follows_reduced_rest:
            result.add_alert(RuleAlert(
                rule_code="D17.3_SPLIT_DUTY_AFTER_REDUCED_REST",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Split duty shall not follow a reduced rest.",
                calculated_value="Split duty after reduced rest",
                required_limit="Not permitted",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if not duty.split_break_start_utc or not duty.split_break_end_utc:
            result.add_alert(RuleAlert(
                rule_code="D17.4.1_SPLIT_BREAK_MISSING",
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message="Split duty requires recorded break start and end time.",
                calculated_value="Missing",
                required_limit="Break >= 3h",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))
            return 0

        break_minutes = int((duty.split_break_end_utc - duty.split_break_start_utc).total_seconds() // 60)

        if break_minutes < 180:
            result.add_alert(RuleAlert(
                rule_code="D17.4.1_SPLIT_BREAK_TOO_SHORT",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Split duty break must be at least 3 consecutive hours.",
                calculated_value=self._fmt_minutes(break_minutes),
                required_limit="03:00",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))
            return 0

        if break_minutes >= 360 and not duty.split_break_has_suitable_accommodation:
            result.add_alert(RuleAlert(
                rule_code="D17.4.4_SPLIT_BREAK_ACCOMMODATION_REQUIRED",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Split duty break of 6h or more requires suitable accommodation.",
                calculated_value=self._fmt_minutes(break_minutes),
                required_limit="Suitable accommodation",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        # D17.4.2 excludes minimum 30 minutes for post/pre-flight/travel.
        effective_break = max(0, break_minutes - 30)

        # D17.4.3 max increase = up to 50% of break.
        return int(effective_break * 0.5)

    # --------------------------------------------------------
    # REST
    # --------------------------------------------------------

    def required_rest_minutes(self, previous_duty: Duty, next_duty: Duty, crew: CrewMember) -> Tuple[int, str]:
        """
        D20 general rest plus D21/D22 charter/aerial work rest.

        For K2 cargo-charter above 5700kg:
        required rest = max(12h, 2 × previous FDP)
        """

        previous_fdp = previous_duty.fdp_minutes

        if (
            next_duty.operation_type == "cargo_charter"
            and next_duty.aircraft_weight_above_5700kg
        ):
            return max(12 * 60, 2 * previous_fdp), "D21.1_CHARTER_AERIAL_WORK_ABOVE_5700KG_REST"

        starts_at_home = (next_duty.report_location or "").upper() == (crew.home_base or "").upper()

        if starts_at_home:
            return max(12 * 60, previous_duty.duration_minutes), "D20.1.1_HOME_BASE_REST"

        return max(10 * 60, previous_duty.duration_minutes), "D20.1.2_AWAY_BASE_REST"

    def _check_rest_between_duties(self, crew: CrewMember, duties: List[Duty], result: ValidationResult) -> None:
        operational = [d for d in duties if d.duty_type not in {DutyType.REST, DutyType.DAY_OFF}]

        for i in range(1, len(operational)):
            prev = operational[i - 1]
            curr = operational[i]

            available = int((curr.start_utc - prev.end_utc).total_seconds() // 60)
            required, rule_code = self.required_rest_minutes(prev, curr, crew)

            if available < required:
                result.add_alert(RuleAlert(
                    rule_code=rule_code,
                    status=AlertStatus.ILLEGAL,
                    severity="RED",
                    message=(
                        f"Insufficient rest before duty. Available {self._fmt_minutes(available)}, "
                        f"required {self._fmt_minutes(required)}."
                    ),
                    calculated_value=self._fmt_minutes(available),
                    required_limit=self._fmt_minutes(required),
                    duty_index=duties.index(curr),
                    duty_id=curr.duty_id,
                    flight_id=curr.flight_id,
                ))

    # --------------------------------------------------------
    # CUMULATIVE LIMITS
    # --------------------------------------------------------

    def _check_cumulative_limits(self, duties: List[Duty], result: ValidationResult) -> None:
        operational = [d for d in duties if d.duty_type not in {DutyType.REST, DutyType.DAY_OFF}]

        for idx, duty in enumerate(duties):
            if duty.duty_type in {DutyType.REST, DutyType.DAY_OFF}:
                continue

            ref_end = duty.end_utc

            duty_7 = self._sum_duty_minutes_in_window(operational, ref_end - dt.timedelta(days=7), ref_end)
            duty_14 = self._sum_duty_minutes_in_window(operational, ref_end - dt.timedelta(days=14), ref_end)
            duty_28 = self._sum_duty_minutes_in_window(operational, ref_end - dt.timedelta(days=28), ref_end)

            flight_7 = self._sum_flight_minutes_in_window(operational, ref_end - dt.timedelta(days=7), ref_end)
            flight_30 = self._sum_flight_minutes_in_window(operational, ref_end - dt.timedelta(days=30), ref_end)
            flight_365 = self._sum_flight_minutes_in_window(operational, ref_end - dt.timedelta(days=365), ref_end)

            checks = [
                ("D9.1.1_7_DAY_DUTY_LIMIT", duty_7, 60 * 60),
                ("D9.1.2_14_DAY_DUTY_LIMIT", duty_14, 110 * 60),
                ("D9.1.3_28_DAY_DUTY_LIMIT", duty_28, 190 * 60),
                ("D9.2.1_7_DAY_FLIGHT_TIME_LIMIT", flight_7, 35 * 60),
                ("D9.2.2_30_DAY_FLIGHT_TIME_LIMIT", flight_30, 100 * 60),
                ("D9.2.3_12_MONTH_FLIGHT_TIME_LIMIT", flight_365, 1000 * 60),
            ]

            for rule_code, actual, limit in checks:
                if actual > limit:
                    result.add_alert(RuleAlert(
                        rule_code=rule_code,
                        status=AlertStatus.ILLEGAL,
                        severity="RED",
                        message=(
                            f"{rule_code} exceeded. Actual {self._fmt_minutes(actual)}, "
                            f"limit {self._fmt_minutes(limit)}."
                        ),
                        calculated_value=self._fmt_minutes(actual),
                        required_limit=self._fmt_minutes(limit),
                        duty_index=idx,
                        duty_id=duty.duty_id,
                        flight_id=duty.flight_id,
                    ))

    def _sum_duty_minutes_in_window(self, duties: List[Duty], start: dt.datetime, end: dt.datetime) -> int:
        total = 0
        for duty in duties:
            overlap = self._overlap_minutes(duty.start_utc, duty.end_utc, start, end)
            total += overlap
        return total

    def _sum_flight_minutes_in_window(self, duties: List[Duty], start: dt.datetime, end: dt.datetime) -> int:
        total = 0
        for duty in duties:
            if duty.duty_type != DutyType.FDP:
                continue
            for sector in duty.sectors:
                if sector.is_positioning:
                    continue
                total += self._overlap_minutes(sector.departure_utc, sector.arrival_utc, start, end)
        return total

    # --------------------------------------------------------
    # OTHER RULES
    # --------------------------------------------------------

    def _check_overlaps(self, duties: List[Duty], result: ValidationResult) -> None:
        for i in range(1, len(duties)):
            prev = duties[i - 1]
            curr = duties[i]
            if curr.start_utc < prev.end_utc:
                result.add_alert(RuleAlert(
                    rule_code="DUTY_OVERLAP",
                    status=AlertStatus.ILLEGAL,
                    severity="RED",
                    message="Duty overlaps previous duty.",
                    calculated_value=f"{curr.start_utc} starts before {prev.end_utc}",
                    required_limit="No overlap",
                    duty_index=i,
                    duty_id=curr.duty_id,
                    flight_id=curr.flight_id,
                ))

    def _check_airport_standby(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        # ANO requires operator scheme to define max duration; use 16h as hard combined guard for K2 initial rule pack.
        if duty.duration_minutes > 16 * 60:
            result.add_alert(RuleAlert(
                rule_code="D18.2.1.4_AIRPORT_STANDBY_COMBINED_LIMIT",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Airport standby / combined assigned FDP must not exceed 16h without approved scheme.",
                calculated_value=self._fmt_minutes(duty.duration_minutes),
                required_limit="16:00",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_other_standby(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        if duty.duration_minutes > 16 * 60:
            result.add_alert(RuleAlert(
                rule_code="D18.2.2.1_OTHER_STANDBY_MAX_16H",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Standby other than airport standby must not exceed 16h.",
                calculated_value=self._fmt_minutes(duty.duration_minutes),
                required_limit="16:00",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_reserve(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        result.add_alert(RuleAlert(
            rule_code="D19_RESERVE_OPERATOR_SCHEME_REQUIRED",
            status=AlertStatus.NEEDS_MANUAL_REVIEW,
            severity="YELLOW",
            message="Reserve legality requires K2 approved reserve scheme, max reserve duration, consecutive reserve days, and 8h sleep protection data.",
            calculated_value="Reserve duty present",
            required_limit="Approved operator scheme",
            duty_index=idx,
            duty_id=duty.duty_id,
            flight_id=duty.flight_id,
        ))

    def _check_positioning(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        if duty.duration_minutes >= 4 * 60:
            result.add_alert(RuleAlert(
                rule_code="D7.1.6_POSITIONING_COUNTS_AS_DUTY",
                status=AlertStatus.WARNING,
                severity="YELLOW",
                message="Surface positioning of 4h or more must count as duty period.",
                calculated_value=self._fmt_minutes(duty.duration_minutes),
                required_limit="Count as duty",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_training(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        result.add_alert(RuleAlert(
            rule_code="D12_TRAINING_COUNTS_AS_FDP",
            status=AlertStatus.WARNING,
            severity="YELLOW",
            message="Training/simulator duty must include pre/post briefing and count toward FDP/duty as applicable.",
            calculated_value=duty.duty_type.value,
            required_limit="Include in duty calculations",
            duty_index=idx,
            duty_id=duty.duty_id,
            flight_id=duty.flight_id,
        ))

    def _check_night_duty(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        if not self._interval_overlaps_local_time(duty.start_utc, duty.end_utc, duty.report_tz_offset, 2, 0, 4, 59):
            return

        if duty.sector_count > 4:
            result.add_alert(RuleAlert(
                rule_code="D16.2.1.2_NIGHT_DUTY_MAX_4_SECTORS",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Night duty FDP is limited to 4 sectors.",
                calculated_value=duty.sector_count,
                required_limit=4,
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.fdp_minutes > 10 * 60 and not self.has_approved_frm:
            result.add_alert(RuleAlert(
                rule_code="D16.2.2_NIGHT_DUTY_OVER_10H_FRM_REQUIRED",
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message="Night duty over 10h requires active fatigue risk management.",
                calculated_value=self._fmt_minutes(duty.fdp_minutes),
                required_limit="FRM required",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_augmented_sector_limit(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        if not duty.is_augmented:
            return

        is_international = any(abs(s.arrival_tz_offset - s.departure_tz_offset) > 0 for s in duty.sectors)
        max_sectors = 3 if is_international else 4

        if duty.sector_count > max_sectors:
            result.add_alert(RuleAlert(
                rule_code="D28.3_AUGMENTED_CREW_SECTOR_LIMIT",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message=f"Augmented crew limited to {max_sectors} sectors for this duty type.",
                calculated_value=duty.sector_count,
                required_limit=max_sectors,
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_nutrition(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        if duty.fdp_minutes > 4 * 60 and duty.snack_provided is False:
            result.add_alert(RuleAlert(
                rule_code="D2.18_D25_SNACK_REQUIRED",
                status=AlertStatus.WARNING,
                severity="YELLOW",
                message="FDP exceeds 4h. Snack opportunity must be arranged.",
                calculated_value=self._fmt_minutes(duty.fdp_minutes),
                required_limit="Snack opportunity",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.fdp_minutes > 6 * 60 and duty.meal_provided is False:
            result.add_alert(RuleAlert(
                rule_code="D2.18_D25_MEAL_REQUIRED",
                status=AlertStatus.WARNING,
                severity="YELLOW",
                message="FDP exceeds 6h. Healthy meal must be arranged.",
                calculated_value=self._fmt_minutes(duty.fdp_minutes),
                required_limit="Meal opportunity",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.fdp_minutes > 6 * 60 and duty.meal_provided is None:
            result.add_alert(RuleAlert(
                rule_code="D25_NUTRITION_DATA_MISSING",
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message="Meal/snack provision data missing for FDP over 6h.",
                calculated_value="Missing",
                required_limit="Nutrition opportunity confirmation",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_license_exam_and_fitness(self, duty: Duty, idx: int, result: ValidationResult) -> None:
        if duty.license_exam_on_duty_day is True:
            result.add_alert(RuleAlert(
                rule_code="D27_LICENSE_EXAM_ON_DUTY_DAY",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Crew member must not undertake pilot license examination on scheduled flight duty day or intervening rest period.",
                calculated_value="Exam on duty/rest day",
                required_limit="Not permitted",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.fitness_for_duty_confirmed is False:
            result.add_alert(RuleAlert(
                rule_code="D3_D26_FITNESS_FOR_DUTY_NOT_CONFIRMED",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message="Crew fitness for duty not confirmed.",
                calculated_value="Not fit / not confirmed",
                required_limit="Fit for duty",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

        if duty.fasting_risk_declared is True:
            result.add_alert(RuleAlert(
                rule_code="D26_FASTING_FITNESS_REVIEW",
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message="Fasting/fitness risk declared. Crew must confirm medical fitness standard before operating.",
                calculated_value="Fasting risk declared",
                required_limit="Fit for licensed duty",
                duty_index=idx,
                duty_id=duty.duty_id,
                flight_id=duty.flight_id,
            ))

    def _check_seventh_day_off(self, duties: List[Duty], result: ValidationResult) -> None:
        duty_dates = sorted({
            d.start_utc.date()
            for d in duties
            if d.duty_type not in {DutyType.REST, DutyType.DAY_OFF}
        })

        if not duty_dates:
            return

        streak = 1
        for i in range(1, len(duty_dates)):
            if duty_dates[i] == duty_dates[i - 1] + dt.timedelta(days=1):
                streak += 1
                if streak > 6:
                    result.add_alert(RuleAlert(
                        rule_code="D23.2_SEVENTH_DAY_OFF",
                        status=AlertStatus.ILLEGAL,
                        severity="RED",
                        message="After six consecutive duty days, a single day free of duty is required.",
                        calculated_value=streak,
                        required_limit=6,
                    ))
                    return
            else:
                streak = 1

    def _check_mandatory_days_off(self, duties: List[Duty], result: ValidationResult) -> None:
        # Only check months that are represented by at least 20 calendar days in provided data.
        months: Dict[Tuple[int, int], Dict[str, set]] = {}

        for duty in duties:
            key = (duty.start_utc.year, duty.start_utc.month)
            months.setdefault(key, {"duty_days": set(), "off_days": set()})

            if duty.duty_type == DutyType.DAY_OFF:
                months[key]["off_days"].add(duty.start_utc.date())
            elif duty.duty_type != DutyType.REST:
                months[key]["duty_days"].add(duty.start_utc.date())

        for (year, month), values in months.items():
            represented_days = values["duty_days"] | values["off_days"]
            if len(represented_days) < 20:
                continue

            off_count = len(values["off_days"])
            if off_count < 5:
                result.add_alert(RuleAlert(
                    rule_code="D23.1_MANDATORY_5_DAYS_OFF",
                    status=AlertStatus.ILLEGAL,
                    severity="RED",
                    message=f"Only {off_count} mandatory days off recorded in {year}-{month:02d}; minimum is 5.",
                    calculated_value=off_count,
                    required_limit=5,
                ))

    def _check_lr_elr_ulr_intervals(self, duties: List[Duty], result: ValidationResult) -> None:
        last_lr: Optional[Duty] = None
        last_elr: Optional[Duty] = None
        last_ulr: Optional[Duty] = None

        for idx, duty in enumerate(duties):
            if duty.duty_type != DutyType.FDP:
                continue

            if duty.is_ulr:
                if last_ulr:
                    gap_days = (duty.start_utc - last_ulr.start_utc).total_seconds() / 86400
                    if gap_days < 12:
                        result.add_alert(RuleAlert(
                            rule_code="D24.5.3_ULR_INTERVAL",
                            status=AlertStatus.ILLEGAL,
                            severity="RED",
                            message="ULR flights require 12 days interval between flights.",
                            calculated_value=round(gap_days, 2),
                            required_limit=12,
                            duty_index=idx,
                            duty_id=duty.duty_id,
                            flight_id=duty.flight_id,
                        ))
                last_ulr = duty

            elif duty.is_elr:
                if last_elr:
                    gap_days = (duty.start_utc - last_elr.start_utc).total_seconds() / 86400
                    if gap_days < 10:
                        result.add_alert(RuleAlert(
                            rule_code="D24.5.2_ELR_INTERVAL",
                            status=AlertStatus.ILLEGAL,
                            severity="RED",
                            message="ELR flights require 10 days interval between flights.",
                            calculated_value=round(gap_days, 2),
                            required_limit=10,
                            duty_index=idx,
                            duty_id=duty.duty_id,
                            flight_id=duty.flight_id,
                        ))
                last_elr = duty

            elif duty.is_lr:
                if last_lr:
                    gap_days = (duty.start_utc - last_lr.start_utc).total_seconds() / 86400
                    if gap_days < 7:
                        result.add_alert(RuleAlert(
                            rule_code="D24.5.1_LR_INTERVAL",
                            status=AlertStatus.ILLEGAL,
                            severity="RED",
                            message="LR flights require 7 days interval between flights.",
                            calculated_value=round(gap_days, 2),
                            required_limit=7,
                            duty_index=idx,
                            duty_id=duty.duty_id,
                            flight_id=duty.flight_id,
                        ))
                last_lr = duty

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    def _previous_operational_duty(self, duties: List[Duty], idx: int) -> Optional[Duty]:
        for j in range(idx - 1, -1, -1):
            if duties[j].duty_type not in {DutyType.REST, DutyType.DAY_OFF}:
                return duties[j]
        return None

    def _overlap_minutes(
        self,
        a_start: dt.datetime,
        a_end: dt.datetime,
        b_start: dt.datetime,
        b_end: dt.datetime,
    ) -> int:
        start = max(a_start, b_start)
        end = min(a_end, b_end)
        if end <= start:
            return 0
        return int((end - start).total_seconds() // 60)

    def _interval_overlaps_local_time(
        self,
        start_utc: dt.datetime,
        end_utc: dt.datetime,
        tz_offset: float,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
    ) -> bool:
        local_start = start_utc + dt.timedelta(hours=tz_offset)
        local_end = end_utc + dt.timedelta(hours=tz_offset)

        current_day = local_start.date() - dt.timedelta(days=1)
        final_day = local_end.date() + dt.timedelta(days=1)

        while current_day <= final_day:
            window_start = dt.datetime.combine(current_day, dt.time(start_hour, start_minute))
            window_end = dt.datetime.combine(current_day, dt.time(end_hour, end_minute))
            if window_end <= window_start:
                window_end += dt.timedelta(days=1)

            if local_start < window_end and local_end > window_start:
                return True

            current_day += dt.timedelta(days=1)

        return False

    def _fmt_minutes(self, minutes: int) -> str:
        sign = "-" if minutes < 0 else ""
        minutes = abs(int(minutes))
        h = minutes // 60
        m = minutes % 60
        return f"{sign}{h:02d}:{m:02d}"