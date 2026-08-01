"""
services/assignment_service.py

Canonical write path for the roster table. Shared by both the
Roster page (scheduled flights) and Control Room (ad-hoc/charter) —
one assignment mechanism, not two, per SSOT. The only difference
between those two pages is where the flight came from and whether
flight-creation and crew-assignment happen in the same UI action;
the legality gate and audit trail are identical either way.

Every assignment goes through two checks, not one:

1. IMMEDIATE gate — is this specific assignment legal, given the
   crew member's existing duty history? ILLEGAL blocks the save
   entirely (ALLOWED/REJECTED, per the explicit "assessed for
   legality and ALLOWED/rejected before - save flt" requirement).

2. DOWNSTREAM impact check — after an immediate-legal assignment is
   saved, does it break the legality of any of that crew member's
   OTHER duties already scheduled later? This is real and distinct
   from #1: an ad-hoc assignment can be perfectly legal on its own
   and still consume enough rest/cumulative hours to make an
   already-rostered future duty illegal. This does NOT block the
   save (the immediate assignment already passed its own gate) — it
   raises a swap alert with legal replacement candidates for the
   affected future duty, and a human confirms any swap. This is
   Section 14's post_override_impact_check(), scoped down from "full
   automated reoptimization" per an explicit decision: alert +
   suggest candidates, not auto-reassign.
"""
import uuid
import datetime as dt
from datetime import timedelta
from typing import Optional, List
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import text

from db.db import get_engine
from services.audit_service import log_audit
from services import crew_service, flight_service
from services.crew_service import ROLE_SYNONYMS
from core.duty_builder import (
    build_duty, recompute_fdp_after_delay, FlightLeg,
    DOMESTIC_POST_FLIGHT_MINUTES, INTERNATIONAL_POST_FLIGHT_MINUTES,
)
from core.legality.pcaa_ano012_core import (
    ANO012CoreValidator, CrewMember, Duty, Sector, DutyType, AlertStatus, ValidationResult, RuleAlert,
)
from services.alert_summary import summarize_alerts, build_audit_reason, AlertSummary

validator = ANO012CoreValidator()

# How far back to look when checking a crew member's rolling-window
# history. Must cover the WIDEST window any rule in
# core/legality/pcaa_ano012_core.py actually checks — D9.2.3
# (365-day/1000h cumulative flight time) is the widest, not the
# 28-day duty limit. This was previously 35 (confirmed real bug,
# 2026-08-01: enough for D9's 7/14/28-day duty/flight windows, but it
# silently starved D9.2.3 of the 330 extra days of history it needs
# — that rule has never once been able to fire correctly for any real
# assignment, since _load_duty_records_for_crew() never fetched
# enough history for it to see a breach). 370 gives a safe margin
# past 365. Applies uniformly at every call site
# (_validate_new_duty(), _check_downstream_impact(),
# find_legal_candidates_for_duty()) — deliberately not split into a
# narrower window for most checks and a wider one just for D9.2.3;
# Air Eagle's crew pool is small enough that the extra data per query
# is not a real cost, and one constant is one less thing to keep in
# sync.
LOOKBACK_DAYS = 370

# Confirmed 2026-07-21: Loadmasters and Engr (line-maintenance AME,
# not flight-deck) are NOT subject to ANO-012's FTL/FDP/rest rules —
# those govern FLIGHT crew duty time specifically. This is a role-
# based operational classification, not a mathematical rule, so it's
# handled here in the orchestration layer, not inside
# core/legality/pcaa_ano012_core.py — the core engine stays
# role-agnostic (it only knows about CrewMember/Duty/Sector), and
# "which roles this applies to" is a regulatory/operational decision
# that belongs with the service that orchestrates the check, not
# baked into the math itself.
FTL_EXEMPT_ROLES = {"LM", "ENGR"}

# Confirmed gap (2026-07-31): FTL_EXEMPT_ROLES only excuses LM/ENGR
# from FDP/rest MATH — it says nothing about whether a crew member
# currently holds a valid license/medical/currency at all. Every
# role, exempt or not, needs this check, which is exactly why it's
# a separate set/function rather than folded into FTL_EXEMPT_ROLES.
QUALIFICATION_EXPIRY_FIELDS = {
    "license_expiry": "LICENSE",
    "medical_expiry": "MEDICAL",
    "sim_expiry": "SIM",
    "route_check_expiry": "ROUTE_CHECK",
    "ir_expiry": "IR",
    "sep_expiry": "SEP",
    "crm_expiry": "CRM",
    "dg_expiry": "DG",
}
# type_rating_expiry and contract_expiry are not columns on crew at
# all (migrations/008_drop_type_rating_and_contract_expiry.sql,
# 2026-08-01) — both were empty for every real crew row received,
# which meant this gate would hold every real crew member for review
# indefinitely. Decision: trust OCC's own offline process to have
# already removed anyone unqualified, rather than gate on two fields
# the operator shows no sign of tracking. Don't re-add either without
# an equally explicit decision, same discipline as FTL_EXEMPT_ROLES.


def _check_crew_qualifications(crew_row: pd.Series, duty_date) -> List[RuleAlert]:
    """
    AE-CREW-QUAL-001 — orchestration-layer crew-qualification gate,
    same placement rationale as FTL_EXEMPT_ROLES: "is this person
    currently qualified" is an operational/regulatory classification,
    not FDP/rest math, so it lives here rather than in
    core/legality/pcaa_ano012_core.py, which stays qualification-
    agnostic.

    Checked against duty_date — the actual duty's own debrief (end)
    date, NOT its report (start) date, and NEVER date.today(). A
    document must remain valid through the END of the duty, not just
    at report time: for any duty that crosses midnight (e.g. Air
    Eagle's real EPE 786/787 KHI-LHE-KHI rotation, which reports
    18:15 and debriefs 00:00 the following day), checking only the
    report date would incorrectly pass a document that's already
    expired before the duty is actually over. Separately, checking
    against date.today() instead of either duty date is the class of
    bug this project's hard-lessons catalogue already calls out as a
    past production incident, not a hypothetical one.

    Collects every failing reason, not just the first — HANDOVER.md
    documents first-failure-only evaluation as a real, already-found
    bug elsewhere in this file (_check_downstream_impact's original
    before/after comparison), not a hypothetical concern here.

    Applies to every role, including LM/ENGR — deliberately NOT
    gated on FTL_EXEMPT_ROLES. That set only exempts flight-duty-time
    math; it says nothing about whether the person holds a valid
    document to be on the roster at all.

    An expiry date equal to duty_date is treated as already expired
    (valid strictly before its own expiry date, not through it).
    """
    alerts: List[RuleAlert] = []

    if not bool(crew_row["is_active"]):
        alerts.append(RuleAlert(
            rule_code="AE-CREW-QUAL-001_INACTIVE_CREW",
            status=AlertStatus.ILLEGAL,
            severity="RED",
            message=f"{crew_row['crew_id']} is not an active crew member (is_active=False).",
            calculated_value="is_active=False",
            required_limit="is_active=True",
        ))

    for field_name, label in QUALIFICATION_EXPIRY_FIELDS.items():
        raw_value = crew_row[field_name]
        if raw_value is None or pd.isna(raw_value):
            alerts.append(RuleAlert(
                rule_code=f"AE-CREW-QUAL-001_{label}_EXPIRY_MISSING",
                status=AlertStatus.NEEDS_MANUAL_REVIEW,
                severity="YELLOW",
                message=f"{crew_row['crew_id']}'s {label} expiry date is not recorded.",
                calculated_value="Missing",
                required_limit=f"Valid {label} expiry after {duty_date}",
            ))
            continue

        expiry_date = raw_value.date() if hasattr(raw_value, "date") else raw_value
        if expiry_date <= duty_date:
            alerts.append(RuleAlert(
                rule_code=f"AE-CREW-QUAL-001_{label}_EXPIRED",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message=(
                    f"{crew_row['crew_id']}'s {label} expired {expiry_date}, "
                    f"not valid for duty date {duty_date}."
                ),
                calculated_value=str(expiry_date),
                required_limit=f"After {duty_date}",
            ))

    return alerts


@dataclass
class DownstreamConflict:
    duty_id: str
    flight_ids: List[int]
    role_assigned: str
    report_time: object
    debrief_time: object
    candidates: List[str] = field(default_factory=list)


@dataclass
class AssignmentResult:
    status: str  # "ALLOWED", "REJECTED", or "NEEDS_REVIEW"
    legality_status: str
    alerts: list
    # Bucketed view of `alerts` for display/audit — see
    # services/alert_summary.py. `alerts` itself is untouched (full
    # fidelity, still consumed directly by existing callers/tests);
    # this is additive.
    alert_summary: Optional["AlertSummary"] = None
    roster_ids: List[int] = field(default_factory=list)
    duty_id: Optional[str] = None
    downstream_conflicts: List[DownstreamConflict] = field(default_factory=list)
    # Populated regardless of status — a human reviewing a
    # NEEDS_REVIEW result still needs to see what was actually
    # computed (report/debrief/FDP), even though nothing was written.
    computed_report_time: Optional[object] = None
    computed_debrief_time: Optional[object] = None
    computed_fdp_hours: Optional[float] = None


# ------------------------------------------------------------------
# Internal: reconstruct Duty objects (and their underlying roster
# records) from the roster+flights tables for one crew member.
# ------------------------------------------------------------------

def _load_duty_records_for_crew(engine, crew_id: str, home_base: str,
                                 start=None, end=None) -> List[dict]:
    """
    Returns a list of dicts, one per duty:
        {"duty": Duty, "flight_ids": [...], "role_assigned": str,
         "roster_ids": [...]}

    Only ACTIVE (non-cancelled) roster rows are included. Uses actual
    departure/arrival times where recorded, falling back to planned —
    "ground truth wins" once a flight has actually operated.
    """
    query = """
        SELECT r.roster_id, r.duty_id, r.report_time, r.debrief_time,
               r.role_assigned, f.flight_id,
               COALESCE(f.dep_time_actual, f.dep_time_planned) AS dep_time,
               COALESCE(f.arr_time_actual, f.arr_time_planned) AS arr_time,
               f.origin, f.destination
        FROM roster r
        JOIN flights f ON r.flight_id = f.flight_id
        WHERE r.crew_id = :crew_id AND r.status != 'CANCELLED'
    """
    params = {"crew_id": crew_id}
    if start is not None:
        query += " AND r.report_time >= :start"
        params["start"] = start
    if end is not None:
        query += " AND r.report_time <= :end"
        params["end"] = end
    query += " ORDER BY r.report_time, f.dep_time_planned"

    df = pd.read_sql(text(query), engine, params=params)
    if df.empty:
        return []

    records = []
    for duty_id, group in df.groupby("duty_id", sort=False):
        first = group.iloc[0]
        sectors = [
            Sector(departure_utc=row["dep_time"], arrival_utc=row["arr_time"],
                   origin=row["origin"], destination=row["destination"])
            for _, row in group.iterrows()
        ]
        duty = Duty(
            duty_type=DutyType.FDP,
            start_utc=first["report_time"],
            end_utc=first["debrief_time"],
            crew_id=crew_id,
            duty_id=duty_id,
            report_location=sectors[0].origin,
            home_base=home_base or "",
            sectors=sectors,
        )
        records.append({
            "duty": duty,
            "flight_ids": list(group["flight_id"]),
            "role_assigned": first["role_assigned"],
            "roster_ids": list(group["roster_id"]),
        })

    records.sort(key=lambda r: r["duty"].start_utc)
    return records


def _crew_member(crew_row: pd.Series) -> CrewMember:
    return CrewMember(crew_id=crew_row["crew_id"], name=crew_row["name"],
                       home_base=crew_row["base"] or "")


def _validate_new_duty(engine, crew_id: str, legs: List[FlightLeg], domestic: bool, role_assigned: str):
    """
    Shared validation core for both assign_crew_to_duty() (assigning
    to flights that already exist) and assign_crew_to_new_flights()
    (Control Room's atomic flight+assignment). Builds the duty, loads
    the crew member's existing history, validates — writes nothing.

    Enforces role_assigned == crew_row["role"] (case-normalized).
    This is a real, confirmed fix, not defensive boilerplate: without
    it, role_assigned was written to the roster completely
    unvalidated against the crew member's actual registered role,
    while the FTL exemption decision correctly used the real role —
    meaning an ENGR (FTL-exempt) crew member could be assigned with
    role_assigned="CPT" and retain the exemption while being recorded
    as filling the Captain role, with zero FDP/rest checking ever
    applied. Air Eagle's crew model has exactly one fixed role per
    person (1 CPT, 1 FO, 1 LM, 1 AME per rotation) — there's no
    legitimate case where these should differ.

    Returns (validation_result, new_duty, crew_member, crew_row,
    duty_result). Raises ValueError if crew_id doesn't exist or
    role_assigned doesn't match the crew member's registered role.
    """
    duty_result = build_duty(legs, domestic=domestic)

    crew_row = crew_service.get_crew(crew_id)
    if crew_row is None:
        raise ValueError(f"No crew member with crew_id={crew_id}")

    normalized_assigned = ROLE_SYNONYMS.get(role_assigned.strip().upper(), role_assigned.strip().upper())
    if normalized_assigned != crew_row["role"]:
        raise ValueError(
            f"role_assigned '{role_assigned}' does not match {crew_id}'s "
            f"registered role '{crew_row['role']}' — assignment rejected"
        )

    crew_member = _crew_member(crew_row)

    new_duty_id = f"DUTY-{uuid.uuid4().hex[:12].upper()}"
    new_duty = Duty(
        duty_type=DutyType.FDP,
        start_utc=duty_result.report_time,
        end_utc=duty_result.debrief_time,
        crew_id=crew_id,
        duty_id=new_duty_id,
        report_location=legs[0].origin,
        home_base=crew_row["base"] or "",
        sectors=[Sector(departure_utc=l.dep_time, arrival_utc=l.arr_time,
                         origin=l.origin, destination=l.destination) for l in legs],
    )

    if crew_row["role"] in FTL_EXEMPT_ROLES:
        # No FTL rules apply to this role — don't run FDP/rest math
        # against duty history that isn't relevant to it. Still build
        # new_duty above (needed for the roster write and audit
        # trail), just skip the legality computation itself.
        validation_result = ValidationResult(
            status=AlertStatus.LEGAL,
            alerts=[],
            computed={"ftl_exempt": True, "role": crew_row["role"]},
        )
    else:
        lookback_start = duty_result.report_time - timedelta(days=LOOKBACK_DAYS)
        existing_records = _load_duty_records_for_crew(
            engine, crew_id, crew_row["base"], start=lookback_start, end=duty_result.debrief_time)
        existing_duties = [r["duty"] for r in existing_records]

        all_duties = sorted(existing_duties + [new_duty], key=lambda d: d.start_utc)
        validation_result = validator.validate_schedule(crew_member, all_duties)

    # Qualification gate applies regardless of FTL exemption — see
    # _check_crew_qualifications' docstring. Folded into the same
    # ValidationResult via add_alert() so its existing status
    # precedence (ILLEGAL > NEEDS_MANUAL_REVIEW > WARNING > LEGAL)
    # and the caller's existing ILLEGAL/NEEDS_MANUAL_REVIEW branches
    # handle this with no new branching logic there.
    for alert in _check_crew_qualifications(crew_row, duty_result.debrief_time.date()):
        validation_result.add_alert(alert)

    return validation_result, new_duty, crew_member, crew_row, duty_result


# ------------------------------------------------------------------
# Core assignment — flight(s) already exist (Roster page)
# ------------------------------------------------------------------

def assign_crew_to_duty(crew_id: str, flight_ids: List[int], role_assigned: str,
                         app_user: Optional[str] = None) -> AssignmentResult:
    """
    Assigns crew_id to the duty formed by flight_ids (in chronological
    order — the caller's decision which flights form one duty).
    domestic is read from the flights themselves, not asked again
    here — all flights in the duty must agree on it.

    ILLEGAL blocks the save entirely (status="REJECTED", nothing
    written). LEGAL/WARNING saves and returns status="ALLOWED", along
    with any downstream conflicts found on the crew member's other
    already-scheduled future duties.
    """
    engine = get_engine()

    flights = [flight_service.get_flight(fid) for fid in flight_ids]
    missing = [fid for fid, f in zip(flight_ids, flights) if f is None]
    if missing:
        raise ValueError(f"Flight_id(s) not found: {missing}")

    # Duty-level classification for the D7.1.2 report/debrief buffer:
    # domestic only if EVERY sector is domestic. One international
    # sector makes the whole duty international — this is what
    # actually allows the real KHI-LHE-DWC-KHI rotation to be built;
    # rejecting any duty with mixed sectors would make that rotation
    # impossible to construct at all. Each flight keeps its own
    # domestic flag independently for Flight Log/reporting — this
    # only affects which buffer applies to the duty as a whole.
    domestic = all(bool(f["domestic"]) for f in flights)

    legs = [
        FlightLeg(
            dep_time=f["dep_time_actual"] if pd.notna(f["dep_time_actual"]) else f["dep_time_planned"],
            arr_time=f["arr_time_actual"] if pd.notna(f["arr_time_actual"]) else f["arr_time_planned"],
            origin=f["origin"], destination=f["destination"],
        )
        for f in flights
    ]

    validation_result, new_duty, crew_member, crew_row, duty_result = _validate_new_duty(
        engine, crew_id, legs, domestic, role_assigned)
    alert_summary = summarize_alerts(validation_result, target_duty_id=new_duty.duty_id)

    if validation_result.status == AlertStatus.ILLEGAL:
        log_audit(
            action_type="ASSIGNMENT_REJECTED",
            affected_crew=crew_id,
            affected_flight=flight_ids[0],
            affected_duty=new_duty.duty_id,
            legality_result=validation_result.status.value,
            warning_or_failure_reason=build_audit_reason(alert_summary, frozenset({AlertStatus.ILLEGAL})),
            app_user=app_user,
        )
        return AssignmentResult(
            status="REJECTED",
            legality_status=validation_result.status.value,
            alerts=validation_result.alerts,
            alert_summary=alert_summary,
            computed_report_time=duty_result.report_time,
            computed_debrief_time=duty_result.debrief_time,
            computed_fdp_hours=duty_result.fdp_hours,
        )

    if validation_result.status == AlertStatus.NEEDS_MANUAL_REVIEW:
        # Confirmed bug, now fixed: this branch previously didn't
        # exist, so NEEDS_MANUAL_REVIEW fell through to the write
        # path below and was silently treated exactly like LEGAL or
        # WARNING — directly contradicting its own defined meaning
        # ("cannot be determined deterministically, requires
        # authorized review"). Nothing gets written here — same as
        # ILLEGAL in that respect — but this is reported as HELD, not
        # REJECTED, since it isn't a known violation, just an
        # unresolved uncertainty that needs a human decision.
        log_audit(
            action_type="ASSIGNMENT_HELD_FOR_REVIEW",
            affected_crew=crew_id,
            affected_flight=flight_ids[0],
            affected_duty=new_duty.duty_id,
            legality_result=validation_result.status.value,
            warning_or_failure_reason=build_audit_reason(
                alert_summary, frozenset({AlertStatus.NEEDS_MANUAL_REVIEW})),
            app_user=app_user,
        )
        return AssignmentResult(
            status="NEEDS_REVIEW",
            legality_status=validation_result.status.value,
            alerts=validation_result.alerts,
            alert_summary=alert_summary,
            computed_report_time=duty_result.report_time,
            computed_debrief_time=duty_result.debrief_time,
            computed_fdp_hours=duty_result.fdp_hours,
        )

    # Only LEGAL or WARNING reach here — passed the immediate gate,
    # write to roster, one row per sector.
    roster_ids = []
    with engine.begin() as conn:
        for fid in flight_ids:
            result = conn.execute(text("""
                INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                    report_time, debrief_time, fdp_hours, role_assigned)
                VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                    :report_time, :debrief_time, :fdp_hours, :role_assigned)
                RETURNING roster_id
            """), {
                "crew_id": crew_id, "flight_id": fid, "duty_id": new_duty.duty_id,
                "duty_date": duty_result.report_time.date(),
                "report_time": duty_result.report_time, "debrief_time": duty_result.debrief_time,
                "fdp_hours": duty_result.fdp_hours, "role_assigned": role_assigned,
            })
            roster_ids.append(result.scalar())

    log_audit(
        action_type="ASSIGNMENT_CREATED",
        affected_crew=crew_id,
        affected_flight=flight_ids[0],
        affected_duty=new_duty.duty_id,
        legality_result=validation_result.status.value,
        rule_applied=f"ANO-012-FSXX D8.2.1 ({'domestic' if domestic else 'international'} buffer)",
        app_user=app_user,
    )

    if crew_row["role"] in FTL_EXEMPT_ROLES:
        downstream_conflicts = []
    else:
        downstream_conflicts = _check_downstream_impact(
            engine, crew_id, crew_row["base"], crew_member, new_duty)

    return AssignmentResult(
        status="ALLOWED",
        legality_status=validation_result.status.value,
        alerts=validation_result.alerts,
        alert_summary=alert_summary,
        roster_ids=roster_ids,
        duty_id=new_duty.duty_id,
        downstream_conflicts=downstream_conflicts,
        computed_report_time=duty_result.report_time,
        computed_debrief_time=duty_result.debrief_time,
        computed_fdp_hours=duty_result.fdp_hours,
    )


# ------------------------------------------------------------------
# Atomic flight-creation + assignment (Control Room, ad-hoc/charter)
# ------------------------------------------------------------------

def assign_crew_to_new_flights(crew_id: str, flights_data: List[dict], role_assigned: str,
                                app_user: Optional[str] = None):
    """
    Control Room's action: create flight(s) AND assign crew, gated by
    legality BEFORE anything is written to either table. This is
    deliberately different from assign_crew_to_duty(): the spec is
    explicit that the flight save itself waits on the crew legality
    check ("assessed for legality and ALLOWED/rejected before - save
    flt") — an illegal proposed assignment must not leave an orphan,
    uncrewed flight sitting in Flight Log. Either both the flight(s)
    and the assignment are saved, or neither is.

    flights_data: list of dicts with the same shape add_flight()
    expects (origin, destination, dep_time_planned, arr_time_planned,
    domestic, and optionally flight_no/aircraft/cargo_dg/remarks) —
    NOT yet-created flight_ids, since nothing is created until this
    passes the legality gate.

    Returns (AssignmentResult, flight_ids). flight_ids is empty on
    REJECTED.
    """
    engine = get_engine()

    # Same duty-level classification rule as assign_crew_to_duty() —
    # see the comment there. Any international sector makes the
    # whole duty international for D7.1.2 buffer purposes.
    domestic = all(bool(f["domestic"]) for f in flights_data)

    legs = [
        FlightLeg(dep_time=f["dep_time_planned"], arr_time=f["arr_time_planned"],
                  origin=f["origin"], destination=f["destination"])
        for f in flights_data
    ]

    validation_result, new_duty, crew_member, crew_row, duty_result = _validate_new_duty(
        engine, crew_id, legs, domestic, role_assigned)
    alert_summary = summarize_alerts(validation_result, target_duty_id=new_duty.duty_id)

    if validation_result.status == AlertStatus.ILLEGAL:
        log_audit(
            action_type="ADHOC_FLIGHT_REJECTED",
            affected_crew=crew_id,
            affected_duty=new_duty.duty_id,
            legality_result=validation_result.status.value,
            warning_or_failure_reason=build_audit_reason(alert_summary, frozenset({AlertStatus.ILLEGAL})),
            app_user=app_user,
        )
        return AssignmentResult(
            status="REJECTED",
            legality_status=validation_result.status.value,
            alerts=validation_result.alerts,
            alert_summary=alert_summary,
            computed_report_time=duty_result.report_time,
            computed_debrief_time=duty_result.debrief_time,
            computed_fdp_hours=duty_result.fdp_hours,
        ), []

    if validation_result.status == AlertStatus.NEEDS_MANUAL_REVIEW:
        # Same fix as assign_crew_to_duty() above, same reasoning.
        # Nothing gets saved here either — no flight, no assignment —
        # consistent with the atomic "gate before save" guarantee
        # this function already provides for ILLEGAL.
        log_audit(
            action_type="ADHOC_FLIGHT_HELD_FOR_REVIEW",
            affected_crew=crew_id,
            affected_duty=new_duty.duty_id,
            legality_result=validation_result.status.value,
            warning_or_failure_reason=build_audit_reason(
                alert_summary, frozenset({AlertStatus.NEEDS_MANUAL_REVIEW})),
            app_user=app_user,
        )
        return AssignmentResult(
            status="NEEDS_REVIEW",
            legality_status=validation_result.status.value,
            alerts=validation_result.alerts,
            alert_summary=alert_summary,
            computed_report_time=duty_result.report_time,
            computed_debrief_time=duty_result.debrief_time,
            computed_fdp_hours=duty_result.fdp_hours,
        ), []

    # Only LEGAL or WARNING reach here — now actually create the
    # flight(s) and the assignment together. Two separate statements,
    # but both only run after the
    # gate already passed, so there's no window where an illegal
    # assignment could leave a saved flight behind.
    flight_ids = []
    with engine.begin() as conn:
        for f in flights_data:
            fields = {k: v for k, v in f.items() if k in flight_service.UPDATABLE_FIELDS}
            columns = ", ".join(fields.keys())
            placeholders = ", ".join(f":{k}" for k in fields.keys())
            result = conn.execute(text(
                f"INSERT INTO flights ({columns}) VALUES ({placeholders}) RETURNING flight_id"
            ), fields)
            flight_ids.append(result.scalar())

    log_audit(
        action_type="FLIGHT_ADDED",
        affected_flight=flight_ids[0],
        changed_state=str(flights_data),
        data_source="control_room",
        app_user=app_user,
    )

    roster_ids = []
    with engine.begin() as conn:
        for fid in flight_ids:
            result = conn.execute(text("""
                INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                    report_time, debrief_time, fdp_hours, role_assigned)
                VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                    :report_time, :debrief_time, :fdp_hours, :role_assigned)
                RETURNING roster_id
            """), {
                "crew_id": crew_id, "flight_id": fid, "duty_id": new_duty.duty_id,
                "duty_date": duty_result.report_time.date(),
                "report_time": duty_result.report_time, "debrief_time": duty_result.debrief_time,
                "fdp_hours": duty_result.fdp_hours, "role_assigned": role_assigned,
            })
            roster_ids.append(result.scalar())

    log_audit(
        action_type="ASSIGNMENT_CREATED",
        affected_crew=crew_id,
        affected_flight=flight_ids[0],
        affected_duty=new_duty.duty_id,
        legality_result=validation_result.status.value,
        rule_applied=f"ANO-012-FSXX D8.2.1 ({'domestic' if domestic else 'international'} buffer)",
        data_source="control_room",
        app_user=app_user,
    )

    if crew_row["role"] in FTL_EXEMPT_ROLES:
        downstream_conflicts = []
    else:
        downstream_conflicts = _check_downstream_impact(
            engine, crew_id, crew_row["base"], crew_member, new_duty)

    return AssignmentResult(
        status="ALLOWED",
        legality_status=validation_result.status.value,
        alerts=validation_result.alerts,
        alert_summary=alert_summary,
        roster_ids=roster_ids,
        duty_id=new_duty.duty_id,
        downstream_conflicts=downstream_conflicts,
        computed_report_time=duty_result.report_time,
        computed_debrief_time=duty_result.debrief_time,
        computed_fdp_hours=duty_result.fdp_hours,
    ), flight_ids


def _check_downstream_impact(engine, crew_id, home_base, crew_member, new_duty):
    """
    For every duty already scheduled AFTER the new duty ends: was it
    legal before the new duty existed, and is it still legal now that
    it does? A flip from legal to illegal is a downstream conflict —
    the new assignment didn't touch this duty directly, but it now
    consumes enough of the crew member's rest/cumulative hours to
    break it.

    Loads full context independently for EACH future duty checked
    (lookback window through that future duty's own start time), not
    reused from the immediate-gate check — the immediate check's duty
    list is deliberately scoped only up to the new duty's own end
    time and would not contain the future duty at all, which was an
    earlier bug here: comparing duty lists that never actually
    included the duty being assessed meant the rest conflict between
    the new duty and the future one was never evaluated.
    """
    future_records = _load_duty_records_for_crew(engine, crew_id, home_base, start=new_duty.end_utc)
    future_records = [r for r in future_records if r["duty"].duty_id != new_duty.duty_id]

    if not future_records:
        return []

    conflicts = []
    for record in future_records:
        future_duty = record["duty"]
        lookback_start = future_duty.start_utc - timedelta(days=LOOKBACK_DAYS)

        context_records = _load_duty_records_for_crew(
            engine, crew_id, home_base, start=lookback_start, end=future_duty.start_utc)
        context_duties = [r["duty"] for r in context_records if r["duty"].duty_id != new_duty.duty_id]

        before_duties = sorted(context_duties, key=lambda d: d.start_utc)
        before_result = validator.validate_schedule(crew_member, before_duties)

        after_duties = sorted(context_duties + [new_duty], key=lambda d: d.start_utc)
        after_result = validator.validate_schedule(crew_member, after_duties)

        was_legal = before_result.status != AlertStatus.ILLEGAL
        still_legal = after_result.status != AlertStatus.ILLEGAL

        if was_legal and not still_legal:
            candidates = find_legal_candidates_for_duty(
                record["flight_ids"], record["role_assigned"], exclude_crew_id=crew_id)
            conflicts.append(DownstreamConflict(
                duty_id=future_duty.duty_id,
                flight_ids=record["flight_ids"],
                role_assigned=record["role_assigned"],
                report_time=future_duty.start_utc,
                debrief_time=future_duty.end_utc,
                candidates=candidates,
            ))

    return conflicts


def find_legal_candidates_for_duty(flight_ids: List[int], role_assigned: str,
                                    exclude_crew_id: Optional[str] = None) -> List[str]:
    """
    Searches all active crew holding role_assigned and returns the
    crew_ids who WOULD be legal if assigned to this duty, given their
    own existing duty history. Does not check location (not tracked —
    see HANDOVER.md) or write anything; read-only candidate search.

    For FTL-exempt roles (LM, ENGR), every active crew member holding
    that role is trivially a legal candidate from an FTL standpoint —
    there's no FTL history that could make them illegal, so the
    FDP/rest simulation loop is skipped for them. They still go
    through _check_crew_qualifications() (2026-07-31): FTL exemption
    is not qualification exemption, so a deactivated or
    expired-document crew member must not be suggested as a
    downstream-swap candidate just because their role skips FDP/rest
    math.
    """
    role_assigned = ROLE_SYNONYMS.get(role_assigned.strip().upper(), role_assigned.strip().upper())

    all_crew = crew_service.get_all_crew(active_only=True)
    candidates_pool = all_crew[all_crew["role"] == role_assigned]

    engine = get_engine()
    flights = [flight_service.get_flight(fid) for fid in flight_ids]
    if any(f is None for f in flights):
        raise ValueError("One or more flight_ids not found")

    # Same duty-level classification rule as assign_crew_to_duty() —
    # any international sector makes the whole duty international.
    domestic = all(bool(f["domestic"]) for f in flights)
    legs = [FlightLeg(
        dep_time=f["dep_time_actual"] if pd.notna(f["dep_time_actual"]) else f["dep_time_planned"],
        arr_time=f["arr_time_actual"] if pd.notna(f["arr_time_actual"]) else f["arr_time_planned"],
        origin=f["origin"], destination=f["destination"],
    ) for f in flights]
    duty_result = build_duty(legs, domestic=domestic)
    # debrief date, not report date — see _check_crew_qualifications'
    # docstring: a document must stay valid through the END of the
    # duty, not just at report time.
    duty_date = duty_result.debrief_time.date()

    if role_assigned in FTL_EXEMPT_ROLES:
        legal_candidates = []
        for _, crew_row in candidates_pool.iterrows():
            if exclude_crew_id and crew_row["crew_id"] == exclude_crew_id:
                continue
            qualification_alerts = _check_crew_qualifications(crew_row, duty_date)
            if not any(a.status == AlertStatus.ILLEGAL for a in qualification_alerts):
                legal_candidates.append(crew_row["crew_id"])
        return legal_candidates

    legal_candidates = []
    for _, crew_row in candidates_pool.iterrows():
        if exclude_crew_id and crew_row["crew_id"] == exclude_crew_id:
            continue

        crew_member = _crew_member(crew_row)
        candidate_duty_id = f"CANDIDATE-{uuid.uuid4().hex[:8]}"
        candidate_duty = Duty(
            duty_type=DutyType.FDP,
            start_utc=duty_result.report_time,
            end_utc=duty_result.debrief_time,
            crew_id=crew_row["crew_id"],
            duty_id=candidate_duty_id,
            report_location=legs[0].origin,
            home_base=crew_row["base"] or "",
            sectors=[Sector(departure_utc=l.dep_time, arrival_utc=l.arr_time,
                             origin=l.origin, destination=l.destination) for l in legs],
        )

        lookback_start = duty_result.report_time - timedelta(days=LOOKBACK_DAYS)
        existing_records = _load_duty_records_for_crew(
            engine, crew_row["crew_id"], crew_row["base"],
            start=lookback_start, end=duty_result.debrief_time)
        existing_duties = [r["duty"] for r in existing_records]

        all_duties = sorted(existing_duties + [candidate_duty], key=lambda d: d.start_utc)
        result = validator.validate_schedule(crew_member, all_duties)

        for alert in _check_crew_qualifications(crew_row, duty_date):
            result.add_alert(alert)

        if result.status != AlertStatus.ILLEGAL:
            legal_candidates.append(crew_row["crew_id"])

    return legal_candidates


def remove_assignment(crew_id: str, flight_id: int, role_assigned: str,
                       reason: Optional[str] = None, app_user: Optional[str] = None) -> None:
    """Marks the roster row CANCELLED — never deletes, same permanent-
    record pattern as flights. The partial unique index
    (005_roster_partial_unique_index.sql) is what allows the same
    (crew, flight, role) to be assigned again afterward."""
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE roster SET status = 'CANCELLED'
            WHERE crew_id = :crew_id AND flight_id = :flight_id
                AND role_assigned = :role_assigned AND status != 'CANCELLED'
        """), {"crew_id": crew_id, "flight_id": flight_id, "role_assigned": role_assigned})
        if result.rowcount == 0:
            raise ValueError("No active assignment found matching crew_id/flight_id/role_assigned")

    log_audit(
        action_type="ASSIGNMENT_REMOVED",
        affected_crew=crew_id,
        affected_flight=flight_id,
        reason=reason,
        app_user=app_user,
    )


def cancel_flight_and_roster(flight_id: int, reason: Optional[str] = None,
                              app_user: Optional[str] = None) -> None:
    """
    Cancels the flight AND cascades CANCELLED to every roster row
    referencing it — never deletes either, same permanent-record
    pattern as remove_assignment()/flight_service.cancel_flight().

    Fixes a real gap (Step 5, 2026-08-01): flight_service.cancel_flight()
    only ever updated flights.status. _load_duty_records_for_crew()
    filters on roster.status, not flights.status — so a cancelled
    flight's duty kept counting toward that crew member's FDP/rest/
    cumulative-hours history exactly as if it had actually operated.
    Cascading here keeps _load_duty_records_for_crew()'s existing
    `status != 'CANCELLED'` filter as the ONE place that decides what
    counts as active legality history, rather than teaching every
    future query to also join against flights.status.

    Lives here, not in flight_service.py: this needs to know about
    roster, which flight_service.py deliberately doesn't (Ownership
    Table). Pages should call this instead of flight_service.cancel_flight()
    directly whenever the flight might have crew assigned.
    """
    flight_service.cancel_flight(flight_id, reason=reason, app_user=app_user)

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE roster SET status = 'CANCELLED'
            WHERE flight_id = :fid AND status != 'CANCELLED'
        """), {"fid": flight_id})

    log_audit(
        action_type="ROSTER_CANCELLED_WITH_FLIGHT",
        affected_flight=flight_id,
        reason=reason,
        changed_state=f"{result.rowcount} roster row(s) cancelled",
        app_user=app_user,
    )


def _recompute_one_duty_after_delay(engine, crew_id: str, duty_id: str,
                                     app_user: Optional[str] = None):
    """
    Recomputes debrief_time/fdp_hours for ONE existing duty after a
    delay changed one of its flights' actual times, updates every
    roster row sharing that duty_id, then revalidates the whole duty
    against the crew member's other history (FDP/rest, and the
    qualification gate against the recomputed debrief date).

    report_time is NEVER recomputed — recompute_fdp_after_delay()'s
    docstring explains why (the historical block-time bug: a delayed
    departure must not shift the time the crew already reported).

    If the recomputed duty is no longer LEGAL/WARNING, every roster
    row sharing this duty_id is flagged status='NEEDS_REVIEW' (not
    just an audit-log alert) — a human decides what to do with an
    already-recorded flight the system can't refuse to reflect, but
    the roster row itself now visibly carries the problem. Returns
    None if the duty isn't found (fully cancelled, or the flight
    isn't actually part of any active duty).
    """
    crew_row = crew_service.get_crew(crew_id)
    if crew_row is None:
        return None

    all_records = _load_duty_records_for_crew(engine, crew_id, crew_row["base"])
    record = next((r for r in all_records if r["duty"].duty_id == duty_id), None)
    if record is None:
        return None

    duty = record["duty"]
    old_debrief_time = duty.end_utc
    old_fdp_hours = round(duty.duration_minutes / 60, 2)

    flights = [flight_service.get_flight(fid) for fid in record["flight_ids"]]
    domestic = all(bool(f["domestic"]) for f in flights)
    post_buffer = timedelta(minutes=(
        DOMESTIC_POST_FLIGHT_MINUTES if domestic else INTERNATIONAL_POST_FLIGHT_MINUTES
    ))
    # duty.sectors already reflect actual times where recorded
    # (COALESCE(actual, planned) — see _load_duty_records_for_crew).
    new_debrief_time = duty.sectors[-1].arrival_utc + post_buffer
    new_fdp_hours = recompute_fdp_after_delay(duty.start_utc, new_debrief_time)

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE roster SET debrief_time = :debrief_time, fdp_hours = :fdp_hours
            WHERE duty_id = :duty_id AND status != 'CANCELLED'
        """), {"debrief_time": new_debrief_time, "fdp_hours": new_fdp_hours, "duty_id": duty_id})

    duty.end_utc = new_debrief_time
    crew_member = _crew_member(crew_row)

    if crew_row["role"] in FTL_EXEMPT_ROLES:
        validation_result = ValidationResult(
            status=AlertStatus.LEGAL, alerts=[], computed={"ftl_exempt": True})
    else:
        lookback_start = duty.start_utc - timedelta(days=LOOKBACK_DAYS)
        history_records = _load_duty_records_for_crew(
            engine, crew_id, crew_row["base"], start=lookback_start, end=new_debrief_time)
        other_duties = [r["duty"] for r in history_records if r["duty"].duty_id != duty_id]
        all_duties = sorted(other_duties + [duty], key=lambda d: d.start_utc)
        validation_result = validator.validate_schedule(crew_member, all_duties)

    for alert in _check_crew_qualifications(crew_row, new_debrief_time.date()):
        validation_result.add_alert(alert)

    alert_summary = summarize_alerts(validation_result, target_duty_id=duty_id)

    now_needs_review = validation_result.status in (AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW)
    if now_needs_review:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE roster SET status = 'NEEDS_REVIEW'
                WHERE duty_id = :duty_id AND status != 'CANCELLED'
            """), {"duty_id": duty_id})

    log_audit(
        action_type="DUTY_FLAGGED_FOR_REVIEW_AFTER_DELAY" if now_needs_review else "DUTY_RECOMPUTED_AFTER_DELAY",
        affected_crew=crew_id,
        affected_duty=duty_id,
        legality_result=validation_result.status.value,
        changed_state=(
            f"debrief_time {old_debrief_time}->{new_debrief_time}, "
            f"fdp_hours {old_fdp_hours}->{new_fdp_hours}"
        ),
        # Confirmed bug, now fixed (2026-08-01): this used to join
        # EVERY alert's message unfiltered by status whenever
        # now_needs_review was true — could log WARNING/LEGAL-tier
        # alert text into a NEEDS_REVIEW/ILLEGAL audit row, and was
        # the direct cause of the measured ~150KB audit row against a
        # 2,215-alert scenario (one line per historical duty, not per
        # rule). Now filtered to exactly the two statuses that made
        # this audit-worthy in the first place (matching
        # now_needs_review's own condition) and summarized by
        # rule_code via build_audit_reason() — see
        # services/alert_summary.py.
        warning_or_failure_reason=(
            build_audit_reason(alert_summary, frozenset({AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW}))
            if now_needs_review else None
        ),
        app_user=app_user,
    )

    downstream_conflicts = []
    if crew_row["role"] not in FTL_EXEMPT_ROLES:
        downstream_conflicts = _check_downstream_impact(engine, crew_id, crew_row["base"], crew_member, duty)

    return validation_result, downstream_conflicts, alert_summary


def update_flight_actual_times_and_revalidate(flight_id: int,
                                               dep_time_actual=None,
                                               arr_time_actual=None,
                                               app_user: Optional[str] = None) -> list:
    """
    Records actual departure/arrival times on a flight AND recomputes
    every affected duty's fdp_hours/debrief_time, revalidating each
    one — fixes a real gap (Step 5, 2026-08-01): flight_service.update_flight()
    only ever updated the flights table; core/duty_builder.py's
    recompute_fdp_after_delay() existed since Phase 5 but nothing
    called it, so a delay recorded here never propagated into the
    roster rows built from this flight, and a duty that became
    illegal because of the delay was never caught.

    A single flight can belong to SEVERAL different duty_ids at
    once — one per crew member assigned to it (CPT/FO/LM/AME each
    get their own duty_id even for the exact same flight, per
    _validate_new_duty()) — so every affected (crew_id, duty_id) pair
    found on this flight is recomputed independently, not just one.

    Lives here, not in flight_service.py, for the same reason as
    cancel_flight_and_roster(): this needs roster/legality knowledge
    flight_service.py deliberately doesn't have. Pages should call
    this instead of flight_service.update_flight() directly whenever
    the update is actual departure/arrival times on a flight that may
    have crew assigned.

    Returns a list of dicts, one per affected (crew_id, duty_id):
    {"crew_id", "duty_id", "validation_result", "downstream_conflicts",
    "alert_summary"} — callers (pages) use this to surface
    NEEDS_REVIEW/ILLEGAL flags and downstream swap alerts, same as the
    assignment-time UI does.
    """
    engine = get_engine()
    updates = {}
    if dep_time_actual is not None:
        updates["dep_time_actual"] = dep_time_actual
    if arr_time_actual is not None:
        updates["arr_time_actual"] = arr_time_actual

    flight_service.update_flight(flight_id, updates, app_user=app_user)

    with engine.connect() as conn:
        affected = conn.execute(text("""
            SELECT DISTINCT crew_id, duty_id FROM roster
            WHERE flight_id = :fid AND status != 'CANCELLED'
        """), {"fid": flight_id}).fetchall()

    results = []
    for crew_id, duty_id in affected:
        outcome = _recompute_one_duty_after_delay(engine, crew_id, duty_id, app_user=app_user)
        if outcome is not None:
            validation_result, downstream_conflicts, alert_summary = outcome
            results.append({
                "crew_id": crew_id, "duty_id": duty_id,
                "validation_result": validation_result,
                "downstream_conflicts": downstream_conflicts,
                "alert_summary": alert_summary,
            })
    return results


def get_roster_for_crew(crew_id: str, include_cancelled: bool = False) -> pd.DataFrame:
    engine = get_engine()
    query = "SELECT * FROM roster WHERE crew_id = :crew_id"
    if not include_cancelled:
        query += " AND status != 'CANCELLED'"
    query += " ORDER BY report_time"
    return pd.read_sql(text(query), engine, params={"crew_id": crew_id})


def get_roster_for_flight(flight_id: int, include_cancelled: bool = False) -> pd.DataFrame:
    engine = get_engine()
    query = "SELECT * FROM roster WHERE flight_id = :flight_id"
    if not include_cancelled:
        query += " AND status != 'CANCELLED'"
    return pd.read_sql(text(query), engine, params={"flight_id": flight_id})


def search_roster(crew_ids: Optional[List[str]] = None, role: Optional[str] = None,
                   date_from=None, date_to=None, include_cancelled: bool = False) -> pd.DataFrame:
    """
    Sector-level roster search across crew/date range — the query
    services/assistant/reports.py's crew_duty_history template needs.
    get_roster_for_crew() doesn't cover this: it takes a single
    crew_id, not a list, and has no date filtering at all.

    Returns ONE ROW PER SECTOR (same shape roster itself stores) —
    report_time/debrief_time/fdp_hours REPEAT across every sector
    belonging to one duty; duty_id is what actually identifies a
    duty. DO NOT sum fdp_hours across rows from this function's
    output without grouping by duty_id first (see
    core/duty_summary.py's group_roster_rows_into_duties()) — this is
    the exact same warning migrations/003_roster_table.sql's own
    header carries: "the single most repeated bug in this platform's
    history." A report-layer consumer of this data is exactly where
    that mistake would happen again if this warning weren't repeated
    here too.
    """
    engine = get_engine()
    query = """
        SELECT r.roster_id, r.crew_id, c.name AS crew_name, r.flight_id,
               r.duty_id, r.duty_date, r.report_time, r.debrief_time,
               r.fdp_hours, r.role_assigned, r.status,
               f.flight_no, f.origin, f.destination,
               f.dep_time_planned, f.arr_time_planned,
               f.dep_time_actual, f.arr_time_actual, f.domestic
        FROM roster r
        JOIN flights f ON r.flight_id = f.flight_id
        JOIN crew c ON r.crew_id = c.crew_id
    """
    conditions = []
    params: dict = {}
    if not include_cancelled:
        conditions.append("r.status != 'CANCELLED'")
    if crew_ids:
        conditions.append("r.crew_id = ANY(:crew_ids)")
        params["crew_ids"] = list(crew_ids)
    if role:
        conditions.append("r.role_assigned = :role")
        params["role"] = role
    if date_from is not None:
        conditions.append("r.report_time >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        # Same inclusive-calendar-date widening as
        # flight_service.get_all_flights() — a bare `<=` against a
        # bare date would silently exclude duties reporting later
        # that same day.
        if isinstance(date_to, dt.date) and not isinstance(date_to, dt.datetime):
            date_to = dt.datetime.combine(date_to, dt.time.max)
        conditions.append("r.report_time <= :date_to")
        params["date_to"] = date_to
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY r.report_time, r.duty_id"
    return pd.read_sql(text(query), engine, params=params)
