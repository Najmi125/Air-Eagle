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
from datetime import timedelta
from typing import Optional, List
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import text

from db.db import get_engine
from services.audit_service import log_audit
from services import crew_service, flight_service
from core.duty_builder import build_duty, FlightLeg
from core.legality.pcaa_ano012_core import (
    ANO012CoreValidator, CrewMember, Duty, Sector, DutyType, AlertStatus, ValidationResult,
)

validator = ANO012CoreValidator()

# How far back to look when checking a crew member's rolling-window
# history. 28-day limits need this margin; 35 gives headroom.
LOOKBACK_DAYS = 35

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
    status: str  # "ALLOWED" or "REJECTED"
    legality_status: str
    alerts: list
    roster_ids: List[int] = field(default_factory=list)
    duty_id: Optional[str] = None
    downstream_conflicts: List[DownstreamConflict] = field(default_factory=list)


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


def _validate_new_duty(engine, crew_id: str, legs: List[FlightLeg], domestic: bool):
    """
    Shared validation core for both assign_crew_to_duty() (assigning
    to flights that already exist) and assign_crew_to_new_flights()
    (Control Room's atomic flight+assignment). Builds the duty, loads
    the crew member's existing history, validates — writes nothing.

    Returns (validation_result, new_duty, crew_member, crew_row,
    duty_result). Raises ValueError if crew_id doesn't exist.
    """
    duty_result = build_duty(legs, domestic=domestic)

    crew_row = crew_service.get_crew(crew_id)
    if crew_row is None:
        raise ValueError(f"No crew member with crew_id={crew_id}")
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
        return validation_result, new_duty, crew_member, crew_row, duty_result

    lookback_start = duty_result.report_time - timedelta(days=LOOKBACK_DAYS)
    existing_records = _load_duty_records_for_crew(
        engine, crew_id, crew_row["base"], start=lookback_start, end=duty_result.debrief_time)
    existing_duties = [r["duty"] for r in existing_records]

    all_duties = sorted(existing_duties + [new_duty], key=lambda d: d.start_utc)
    validation_result = validator.validate_schedule(crew_member, all_duties)

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

    domestic_values = {bool(f["domestic"]) for f in flights}
    if len(domestic_values) > 1:
        raise ValueError("All flights in one duty must have the same domestic value")
    domestic = domestic_values.pop()

    legs = [
        FlightLeg(
            dep_time=f["dep_time_actual"] if pd.notna(f["dep_time_actual"]) else f["dep_time_planned"],
            arr_time=f["arr_time_actual"] if pd.notna(f["arr_time_actual"]) else f["arr_time_planned"],
            origin=f["origin"], destination=f["destination"],
        )
        for f in flights
    ]

    validation_result, new_duty, crew_member, crew_row, duty_result = _validate_new_duty(
        engine, crew_id, legs, domestic)

    if validation_result.status == AlertStatus.ILLEGAL:
        log_audit(
            action_type="ASSIGNMENT_REJECTED",
            affected_crew=crew_id,
            affected_flight=flight_ids[0],
            affected_duty=new_duty.duty_id,
            legality_result=validation_result.status.value,
            warning_or_failure_reason="; ".join(a.message for a in validation_result.alerts if a.status == AlertStatus.ILLEGAL),
            app_user=app_user,
        )
        return AssignmentResult(
            status="REJECTED",
            legality_status=validation_result.status.value,
            alerts=validation_result.alerts,
        )

    # Passed the immediate gate — write to roster, one row per sector.
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
        rule_applied="ANO-012-FSXX D21" if not domestic else "ANO-012-FSXX D8/D9",
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
        roster_ids=roster_ids,
        duty_id=new_duty.duty_id,
        downstream_conflicts=downstream_conflicts,
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

    domestic_values = {bool(f["domestic"]) for f in flights_data}
    if len(domestic_values) > 1:
        raise ValueError("All flights in one duty must have the same domestic value")
    domestic = domestic_values.pop()

    legs = [
        FlightLeg(dep_time=f["dep_time_planned"], arr_time=f["arr_time_planned"],
                  origin=f["origin"], destination=f["destination"])
        for f in flights_data
    ]

    validation_result, new_duty, crew_member, crew_row, duty_result = _validate_new_duty(
        engine, crew_id, legs, domestic)

    if validation_result.status == AlertStatus.ILLEGAL:
        log_audit(
            action_type="ADHOC_FLIGHT_REJECTED",
            affected_crew=crew_id,
            affected_duty=new_duty.duty_id,
            legality_result=validation_result.status.value,
            warning_or_failure_reason="; ".join(a.message for a in validation_result.alerts if a.status == AlertStatus.ILLEGAL),
            app_user=app_user,
        )
        return AssignmentResult(
            status="REJECTED",
            legality_status=validation_result.status.value,
            alerts=validation_result.alerts,
        ), []

    # LEGAL — now actually create the flight(s) and the assignment
    # together. Two separate statements, but both only run after the
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
        rule_applied="ANO-012-FSXX D21" if not domestic else "ANO-012-FSXX D8/D9",
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
        roster_ids=roster_ids,
        duty_id=new_duty.duty_id,
        downstream_conflicts=downstream_conflicts,
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
    that role is trivially a legal candidate — there's no FTL history
    that could make them illegal, so the simulation loop is skipped
    entirely rather than running validate_schedule() against rules
    that don't apply to them.
    """
    all_crew = crew_service.get_all_crew(active_only=True)
    candidates_pool = all_crew[all_crew["role"] == role_assigned]

    if role_assigned in FTL_EXEMPT_ROLES:
        return [
            cid for cid in candidates_pool["crew_id"]
            if not (exclude_crew_id and cid == exclude_crew_id)
        ]

    engine = get_engine()
    flights = [flight_service.get_flight(fid) for fid in flight_ids]
    if any(f is None for f in flights):
        raise ValueError("One or more flight_ids not found")

    domestic = bool(flights[0]["domestic"])
    legs = [FlightLeg(
        dep_time=f["dep_time_actual"] if pd.notna(f["dep_time_actual"]) else f["dep_time_planned"],
        arr_time=f["arr_time_actual"] if pd.notna(f["arr_time_actual"]) else f["arr_time_planned"],
        origin=f["origin"], destination=f["destination"],
    ) for f in flights]
    duty_result = build_duty(legs, domestic=domestic)

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
