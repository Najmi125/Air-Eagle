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

# Flight-deck crew package (2026-08-12) — the composition rule as
# data, one place. Grade (crew.role: CPT/FO) and operating position
# (roster.operating_position: COMMANDER/SECOND_PILOT — what a pilot
# DOES on this specific flight) are two different things; this maps
# which grades are eligible for which seat. Commander must be
# CPT-graded; Second Pilot may be CPT or FO graded — operator
# confirmed no separate right-seat qualification exists, any current
# Captain may fly Second Pilot.
SEAT_ELIGIBLE_GRADES = {"COMMANDER": {"CPT"}, "SECOND_PILOT": {"CPT", "FO"}}

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
class CandidateStatus:
    """Replaces the old bare List[str] find_legal_candidates_for_duty()
    returned (2026-08-12) — that shape let a NEEDS_REVIEW candidate
    (e.g. a missing DG expiry) show up indistinguishable from a
    genuinely LEGAL one, only for the real gate to refuse them at
    actual assignment time. Every evaluated candidate is returned now,
    not just the passing ones — callers filter to LEGAL + permitted
    WARNING for "selectable," and can still show NEEDS_REVIEW/ILLEGAL
    candidates separately with their real reason, never mislabeled
    "legal"."""
    crew_id: str
    status: str  # AlertStatus values: "LEGAL", "WARNING", "ILLEGAL", "NEEDS_MANUAL_REVIEW"
    blocking_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


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
    # Age-pairing (AE-CREW-PAIR-AGE-001, Step 7, 2026-08-02) — CPT/FO
    # only, populated regardless of status, same reasoning as the
    # computed_* fields above. A genuine RuleAlert/ILLEGAL result for
    # this rule already shows up in alerts/alert_summary like any other
    # check; these three fields exist for the case that ISN'T an
    # alert — a single pilot assigned with no partner yet, where the
    # rule can't be evaluated at all. Deliberately NOT a RuleAlert
    # (see _check_crew_pairing_age()'s docstring for why): the pending
    # case would otherwise elevate every first-pilot assignment to
    # WARNING for a condition that isn't actually wrong yet.
    pairing_pending: bool = False
    paired_crew_id: Optional[str] = None
    # Populated only when pairing_pending is True AND this pilot is
    # already 65+ — the real operational trap: nothing blocks this
    # assignment, but the rotation's eventual legality is already
    # constrained (or, for an international rotation, already
    # impossible) by this pilot's age alone, with no earlier signal
    # than this field if a second pilot is never assigned at all.
    pairing_constraint: Optional[str] = None


@dataclass
class PairValidationResult:
    """validate_pair()'s dry-run result — both seats evaluated
    together, nothing written. status is the worst of: either pilot's
    own individual FDP/rest/qualification result, or the direct
    age-pairing check between the two proposed candidates (computed
    here, not via _check_crew_pairing_age()'s DB-lookup path — see
    validate_pair()'s own docstring for why)."""
    status: str  # AlertStatus values: "LEGAL", "WARNING", "ILLEGAL", "NEEDS_MANUAL_REVIEW"
    commander_crew_id: str
    second_pilot_crew_id: str
    commander_status: str
    second_pilot_status: str
    commander_alerts: list = field(default_factory=list)
    second_pilot_alerts: list = field(default_factory=list)
    pair_alerts: list = field(default_factory=list)
    commander_alert_summary: Optional["AlertSummary"] = None
    second_pilot_alert_summary: Optional["AlertSummary"] = None
    commander_computed_report_time: Optional[object] = None
    commander_computed_debrief_time: Optional[object] = None
    commander_computed_fdp_hours: Optional[float] = None
    second_pilot_computed_report_time: Optional[object] = None
    second_pilot_computed_debrief_time: Optional[object] = None
    second_pilot_computed_fdp_hours: Optional[float] = None


@dataclass
class PairAssignmentResult:
    """assign_pair_to_duty()'s result — both-or-neither, matching
    validation.status: REJECTED/NEEDS_REVIEW writes nothing for either
    seat; ALLOWED writes both."""
    status: str  # "ALLOWED", "REJECTED", or "NEEDS_REVIEW"
    validation: PairValidationResult
    commander_roster_ids: List[int] = field(default_factory=list)
    second_pilot_roster_ids: List[int] = field(default_factory=list)
    commander_duty_id: Optional[str] = None
    second_pilot_duty_id: Optional[str] = None
    commander_downstream_conflicts: List["DownstreamConflict"] = field(default_factory=list)
    second_pilot_downstream_conflicts: List["DownstreamConflict"] = field(default_factory=list)


# ------------------------------------------------------------------
# Internal: reconstruct Duty objects (and their underlying roster
# records) from the roster+flights tables for one crew member.
# ------------------------------------------------------------------

DUTY_ROW_COLUMNS = [
    "roster_id", "duty_id", "report_time", "debrief_time", "role_assigned",
    "operating_position", "flight_id", "dep_time", "arr_time", "origin",
    "destination", "meal_provided", "snack_provided",
]


class ProvisionalDuties:
    """Duties a caller has DECIDED but not yet written, in the exact row
    shape _read_duty_rows() returns, so the legality gate sees them as
    history.

    WHY THIS EXISTS AT ALL. Generation used to write PROPOSED roster
    rows as it walked the window, which meant cross-rotation legality
    was enforced by accident: by the time rotation 2 was validated,
    rotation 1's rows were already in the roster table, so
    _fetch_duty_rows() picked them up and the rest/cumulative rules saw
    them. A preview that writes nothing loses that silently — every
    rotation validates against an empty history, each one passes on its
    own, and the SET is illegal. Nothing raises; the seats just fill.

    So the union here is not an optimisation and not a convenience. It
    is the thing that replaces the side effect the writes were
    providing, and tests/test_cross_rotation_legality.py exists to hold
    it in place.

    NOT A CACHE, and deliberately kept separate from Prefetch.duty_rows
    (which is one). Prefetch.duty_rows memoises what the DATABASE said
    and is invalidated by nothing, because during a preview nothing is
    written and the answer cannot change. These rows are unioned on
    AFTER that cache is consulted, so adding a provisional duty costs
    zero round-trips and invalidates nothing — see add_duty().
    """

    def __init__(self):
        # crew_id -> list of row dicts. Kept as dicts rather than one
        # DataFrame because the hot path is "append one duty, then read
        # one crew member's rows"; concatenating a frame per append was
        # measurably the wrong shape.
        self._rows_by_crew = {}

    def add_duty(self, crew_id: str, duty_id: str, report_time, debrief_time,
                 role_assigned: str, operating_position, flights: List[dict]) -> None:
        """Record one provisional duty — every sector of it, one row per
        flight, matching what the roster table would hold.

        Invalidates NOTHING. Prefetch.duty_rows holds database answers,
        and a preview issues no writes, so a provisional duty cannot
        make a cached database answer wrong. That is what keeps the
        round-trip count flat across this change rather than multiplying
        it by the number of rotations (measured — see
        tests/test_generation_round_trips.py).

        roster_id is None: there is no roster row. Anything that needs a
        real roster_id is a write path and must not be reading these.
        """
        rows = self._rows_by_crew.setdefault(crew_id, [])
        for flight in flights:
            rows.append({
                "roster_id": None,
                "duty_id": duty_id,
                "report_time": report_time,
                "debrief_time": debrief_time,
                "role_assigned": role_assigned,
                "operating_position": operating_position,
                "flight_id": flight["flight_id"],
                "dep_time": flight["dep_time"],
                "arr_time": flight["arr_time"],
                "origin": flight["origin"],
                "destination": flight["destination"],
                "meal_provided": flight["meal_provided"],
                "snack_provided": flight["snack_provided"],
            })

    def duty_ids_for(self, crew_id: str) -> set:
        return {row["duty_id"] for row in self._rows_by_crew.get(crew_id, [])}

    def rows_for(self, crew_id: str, start=None, end=None) -> pd.DataFrame:
        """This crew member's provisional rows in [start, end], filtered
        on report_time — the same predicate _fetch_duty_rows() applies in
        SQL, so a provisional duty is in or out of the lookback window on
        identical terms to a committed one."""
        rows = self._rows_by_crew.get(crew_id)
        if not rows:
            return pd.DataFrame(columns=DUTY_ROW_COLUMNS)
        if start is not None:
            rows = [r for r in rows if r["report_time"] >= start]
        if end is not None:
            rows = [r for r in rows if r["report_time"] <= end]
        return pd.DataFrame(rows, columns=DUTY_ROW_COLUMNS)


def _provisional_duty_rows(prefetch, crew_id: str, start=None, end=None) -> pd.DataFrame:
    """The seam the cross-rotation legality test disables.

    A one-line indirection for the same reason _read_duty_rows() is one:
    a test can neutralise the provisional union here and watch the suite
    go red, without also replacing the caching and filtering around it
    with its own reimplementation. Watching this fail is the only thing
    that distinguishes "cross-rotation legality is enforced by the
    provisional union" from "cross-rotation legality happens to still be
    enforced by leftover committed rows".
    """
    if prefetch is None or prefetch.provisional is None:
        return pd.DataFrame(columns=DUTY_ROW_COLUMNS)
    return prefetch.provisional.rows_for(crew_id, start=start, end=end)


class Prefetch:
    """Rows a caller ALREADY holds, so this module does not re-fetch
    them per call.

    OPT-IN AND NON-AUTHORITATIVE. Every lookup falls back to a live
    fetch when the row is absent, and every existing caller passes
    nothing — so the default path is byte-for-byte today's behaviour.
    Only the roster generator supplies one.

    On staleness, since passing rows in DOES weaken the "always current"
    guarantee the direct fetch gave:

      * Lifetime is one generate_for_window() call. Not module-level,
        not memoised across runs.
      * Within a run a SHARED snapshot is required for correctness, not
        merely tolerated: the qualification pre-filter and this gate
        must judge a candidate on the same data, and they would not if
        the filter read a snapshot while the gate re-fetched per trial.
      * Generation writes nothing at all now — generate_preview()
        decides, accept_preview() writes, and accept re-validates every
        rotation against FRESH data before committing it. A crew edit
        landing mid-preview therefore cannot reach the roster table at
        all, which is a stronger guarantee than the PROPOSED-rows
        design this replaced.

    `provisional` is the other half of that change: with no writes
    during a run, the duties a run has already decided exist nowhere the
    gate would find them, so they are carried here instead. See
    ProvisionalDuties.
    """

    def __init__(self, crew_by_id=None, flights_by_id=None, provisional=None):
        self.crew_by_id = crew_by_id or {}
        self.flights_by_id = flights_by_id or {}
        # (crew_id, start, end) -> DataFrame of duty rows. Inert
        # data only; Duty objects are rebuilt per call. DATABASE ANSWERS
        # ONLY — provisional duties are unioned on after this is read,
        # never stored in it, so this stays valid for a whole preview.
        self.duty_rows = {}
        self.provisional = provisional


def _get_crew_row(crew_id: str, prefetch=None):
    """The crew row, from the caller's snapshot if it has it."""
    if prefetch is not None and crew_id in prefetch.crew_by_id:
        return prefetch.crew_by_id[crew_id]
    return crew_service.get_crew(crew_id)


def _get_flight_row(flight_id: int, prefetch=None):
    if prefetch is not None and flight_id in prefetch.flights_by_id:
        return prefetch.flights_by_id[flight_id]
    return flight_service.get_flight(flight_id)


def _read_duty_rows(query: str, engine, params: dict) -> pd.DataFrame:
    """The single statement that actually goes to the database for duty
    history. A one-line seam, so a test can count round-trips while the
    REAL caching in _fetch_duty_rows() still runs — patching that
    function instead would replace the cache with the test's own copy of
    it, which is the drift this codebase has repeatedly paid for.
    """
    return pd.read_sql(text(query), engine, params=params)


def _fetch_duty_rows(engine, crew_id: str, start=None, end=None, prefetch=None) -> pd.DataFrame:
    """The QUERY behind _load_duty_records_for_crew(), split out so its
    result can be cached without caching constructed objects.

    That distinction is deliberate and load-bearing. Within one
    rotation every candidate trial asks for the SAME crew member over
    the SAME window — start/end derive from build_duty(legs), and the
    legs are the rotation's own, identical for every candidate — so the
    query repeats C x S times per rotation and was the last quadratic
    term in generation (2026-08-22).

    Caching the DATAFRAME rather than the record list is what makes it
    safe: `Duty` is a plain mutable dataclass, so handing the same
    objects to every trial would share mutable state through the
    legality engine. Rows are inert data; the records and their Duty
    objects are rebuilt fresh on every call, exactly as before.

    PROVISIONAL ROWS ARE UNIONED ON AFTER THE CACHE, not into it. The
    cached frame is what the DATABASE said, which during a preview never
    changes because a preview writes nothing; the provisional rows are
    what this run has decided since. Keeping them apart is what lets the
    cache survive a provisional duty being added — the alternative,
    caching the union, would have to drop every entry for that crew
    member on every fill and would put the per-candidate duty-history
    query back, once per rotation, which is the exact 2026-08-22 defect.
    """
    db_rows = _cached_db_duty_rows(engine, crew_id, start=start, end=end, prefetch=prefetch)
    extra = _provisional_duty_rows(prefetch, crew_id, start=start, end=end)
    if extra.empty:
        return db_rows
    if db_rows.empty:
        combined = extra
    else:
        combined = pd.concat([db_rows, extra], ignore_index=True)
    # Same ordering the query itself applies (report_time, then departure
    # within the duty) — _load_duty_records_for_crew() builds a duty's
    # sectors in row order, so an unsorted concat would hand the
    # legality engine a duty whose sectors run backwards.
    return combined.sort_values(["report_time", "dep_time"], kind="stable").reset_index(drop=True)


def _cached_db_duty_rows(engine, crew_id: str, start=None, end=None, prefetch=None) -> pd.DataFrame:
    """The committed half of _fetch_duty_rows() — the query and its
    per-run memo, with no knowledge of provisional duties."""
    if prefetch is not None:
        key = (crew_id, start, end)
        if key in prefetch.duty_rows:
            return prefetch.duty_rows[key]

    query = """
        SELECT r.roster_id, r.duty_id, r.report_time, r.debrief_time,
               r.role_assigned, r.operating_position, f.flight_id,
               COALESCE(f.dep_time_actual, f.dep_time_planned) AS dep_time,
               COALESCE(f.arr_time_actual, f.arr_time_planned) AS arr_time,
               f.origin, f.destination, f.meal_provided, f.snack_provided
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

    df = _read_duty_rows(query, engine, params)
    if prefetch is not None:
        prefetch.duty_rows[(crew_id, start, end)] = df
    return df


def _load_duty_records_for_crew(engine, crew_id: str, home_base: str,
                                 start=None, end=None, prefetch=None) -> List[dict]:
    """
    Returns a list of dicts, one per duty:
        {"duty": Duty, "flight_ids": [...], "role_assigned": str,
         "roster_ids": [...]}

    Only ACTIVE (non-cancelled) roster rows are included. Uses actual
    departure/arrival times where recorded, falling back to planned —
    "ground truth wins" once a flight has actually operated.

    The rows may come from a caller's cache (see _fetch_duty_rows), but
    the Duty objects built from them are always fresh.
    """
    df = _fetch_duty_rows(engine, crew_id, start=start, end=end, prefetch=prefetch)
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
        # meal_provided/snack_provided aggregated across every flight in
        # this duty, same all()-of-the-legs pattern already used for
        # `domestic` elsewhere in this file (e.g. assign_crew_to_duty())
        # — a duty only "had a meal/snack provided" if every leg
        # genuinely did. Both columns are NOT NULL (migrations/014,
        # migrations/015), so this is always a real True/False, never
        # the None that used to silently trip D25/D2.18 for every
        # rebuilt historical duty.
        duty = Duty(
            duty_type=DutyType.FDP,
            start_utc=first["report_time"],
            end_utc=first["debrief_time"],
            crew_id=crew_id,
            duty_id=duty_id,
            report_location=sectors[0].origin,
            home_base=home_base or "",
            sectors=sectors,
            meal_provided=bool(group["meal_provided"].all()),
            snack_provided=bool(group["snack_provided"].all()),
        )
        records.append({
            "duty": duty,
            "flight_ids": list(group["flight_id"]),
            "role_assigned": first["role_assigned"],
            "operating_position": first["operating_position"],
            "roster_ids": list(group["roster_id"]),
        })

    records.sort(key=lambda r: r["duty"].start_utc)
    return records


def _crew_member(crew_row: pd.Series) -> CrewMember:
    return CrewMember(crew_id=crew_row["crew_id"], name=crew_row["name"],
                       home_base=crew_row["base"] or "")


# ------------------------------------------------------------------
# Age-pairing rule (AE-CREW-PAIR-AGE-001, Step 7, 2026-08-02) —
# confirmed Air Eagle operating policy, NOT an ANO-012 provision (the
# actual document was checked directly and contains no age-eligibility
# rule at all), so this lives here in the orchestration layer, same
# placement reasoning as FTL_EXEMPT_ROLES and _check_crew_qualifications()
# above — core/legality/pcaa_ano012_core.py stays regulation-only and
# role/policy-agnostic.
#
# Settled wording: domestic requires at least 1 pilot under 65
# (illegal only if BOTH are 65+); international requires BOTH pilots
# under 65 (illegal if EITHER is 65+). Exactly 65 counts as NOT under
# 65, either way. LM/AME are irrelevant — this only ever applies to
# CPT/FO. Age is calculated on the rotation's first operating date.
# ------------------------------------------------------------------

@dataclass
class PairingCheckResult:
    pending: bool = False
    paired_crew_id: Optional[str] = None
    constraint_message: Optional[str] = None


def age_on(dob, reference_date) -> int:
    """Complete years as of reference_date. Turning 65 ON
    reference_date already counts as 65, not 64 — "exactly 65 does
    not count as below 65 either way" (HANDOVER.md, settled wording).

    Promoted from private (_age_on) to a plain shared utility
    (2026-08-04, Phase 7's roster generator) — services/
    roster_generator_service.py reuses this exact, already-tested
    function for its own candidate-ordering heuristic rather than
    re-deriving age arithmetic; still the same function, same
    behavior, every existing call site in this file unaffected."""
    years = reference_date.year - dob.year
    if (reference_date.month, reference_date.day) < (dob.month, dob.day):
        years -= 1
    return years


def _evaluate_pair_age(age_a: int, age_b: int, domestic: bool) -> bool:
    """Returns True if the pair is ILLEGAL under the settled rule."""
    if domestic:
        return age_a >= 65 and age_b >= 65
    return age_a >= 65 or age_b >= 65


def _pairing_constraint_message(crew_id: str, age: int, domestic: bool) -> str:
    if domestic:
        return (
            f"{crew_id} is {age} (65 or older). Domestic rule: illegal only if BOTH "
            f"pilots are 65 or older. The other seat must be filled by a pilot under "
            f"65 for this rotation's pairing to be legal."
        )
    return (
        f"{crew_id} is {age} (65 or older). International rule: illegal if EITHER "
        f"pilot is 65 or older — since this pilot already is, NO second pilot can "
        f"make this rotation's pairing legal under its current international "
        f"classification."
    )


def _find_paired_pilot(engine, flight_ids: Optional[List[int]], operating_position: str,
                        crew_id: str) -> Optional[dict]:
    """
    Looks for another ACTIVE pilot holding the OTHER operating_position
    (COMMANDER/SECOND_PILOT) covering the EXACT SAME set of flight_ids
    as this duty — an exact set match, not just any overlap. Two
    pilots can each legitimately have a roster row referencing the
    same single flight_id while actually flying different, unrelated
    duties (one shared sector between two otherwise-distinct
    multi-sector rotations); overlap alone would wrongly pair them.

    Re-keyed 2026-08-12 (flight-deck crew package) from grade
    (role_assigned CPT/FO) to seat (operating_position COMMANDER/
    SECOND_PILOT) — a CPT+CPT pair means grade can no longer tell you
    who "the other seat" is; operating_position always can, regardless
    of grade.

    Returns None (nobody on the other seat yet) whenever flight_ids is
    empty/None — the Control Room path calls this BEFORE the new
    flight(s) exist, so nothing could possibly already be assigned to
    them; there is structurally nothing to find at that point.
    """
    if not flight_ids:
        return None

    other_position = "SECOND_PILOT" if operating_position == "COMMANDER" else "COMMANDER"
    candidates = pd.read_sql(text("""
        SELECT r.crew_id, r.duty_id, r.flight_id, c.date_of_birth
        FROM roster r
        JOIN crew c ON c.crew_id = r.crew_id
        WHERE r.operating_position = :other_position
          AND r.status != 'CANCELLED'
          AND r.crew_id != :crew_id
          AND r.duty_id IN (
              SELECT DISTINCT duty_id FROM roster
              WHERE operating_position = :other_position AND status != 'CANCELLED'
                AND crew_id != :crew_id AND flight_id = ANY(:flight_ids)
          )
    """), engine, params={
        "other_position": other_position, "crew_id": crew_id, "flight_ids": list(flight_ids),
    })

    if candidates.empty:
        return None

    target_set = set(flight_ids)
    for (candidate_crew_id, duty_id), group in candidates.groupby(["crew_id", "duty_id"]):
        if set(group["flight_id"]) == target_set:
            return {"crew_id": candidate_crew_id, "date_of_birth": group.iloc[0]["date_of_birth"]}
    return None


def _check_crew_pairing_age(engine, operating_position: Optional[str], crew_row: pd.Series,
                             flight_ids: Optional[List[int]],
                             domestic: bool, reference_date) -> tuple:
    """
    Returns (alerts, PairingCheckResult). Scoped to an actual pilot
    seat only — operating_position=None (LM/ENGR, or any caller not
    part of a flight-deck pair) returns immediately with no alerts and
    a default (not-applicable) PairingCheckResult.

    Re-keyed 2026-08-12 from "crew_row['role'] in ('CPT','FO')" to
    "operating_position is not None" — the rule now applies to the
    OPERATING PAIR regardless of grade (a CPT+CPT pair is checked
    exactly like CPT+FO always was), per explicit operator instruction
    that _evaluate_pair_age()'s own math stays byte-for-byte unchanged.

    This function finds the pair via a REAL, ALREADY-ASSIGNED partner
    in the database (_find_paired_pilot()) — the right tool when one
    seat is already committed (a solo/first-pilot assignment, or
    filling the second seat of an existing pair) and the question is
    "does the other seat's real occupant make this legal." It is NOT
    used to compare two freshly-PROPOSED candidates against each other
    before either is written — validate_pair() does that age check
    directly, since _find_paired_pilot()'s DB lookup would find nothing
    for either brand-new candidate and silently skip the check the
    pair most needs.

    Deliberately does NOT emit a RuleAlert for the "nobody on the other
    seat yet" case: a WARNING-severity alert would elevate
    ValidationResult.status from LEGAL to WARNING under
    ValidationResult.add_alert()'s existing precedence — meaning every
    first-pilot assignment to any rotation, Control Room or Roster,
    would show as WARNING for a condition that isn't actually wrong,
    just not yet decidable. That's a false alarm baked into the common
    case. Instead, the pending state (and, when relevant, the
    constraint it creates) rides along on AssignmentResult's own
    pairing_pending/paired_crew_id/pairing_constraint fields — visible
    without touching legality status at all.
    """
    if operating_position is None:
        return [], PairingCheckResult()

    paired = _find_paired_pilot(engine, flight_ids, operating_position, crew_row["crew_id"])

    if paired is None:
        constraint = None
        if pd.notna(crew_row["date_of_birth"]):
            age = age_on(crew_row["date_of_birth"], reference_date)
            if age >= 65:
                constraint = _pairing_constraint_message(crew_row["crew_id"], age, domestic)
        return [], PairingCheckResult(pending=True, constraint_message=constraint)

    if pd.isna(crew_row["date_of_birth"]) or pd.isna(paired["date_of_birth"]):
        alert = RuleAlert(
            rule_code="AE-CREW-PAIR-AGE-001_DOB_MISSING",
            status=AlertStatus.NEEDS_MANUAL_REVIEW,
            severity="YELLOW",
            message=(
                f"Cannot evaluate the age-pairing rule for {crew_row['crew_id']} + "
                f"{paired['crew_id']}: date of birth is missing for at least one pilot."
            ),
            calculated_value="DOB missing",
            required_limit="Both pilots' date_of_birth required",
        )
        return [alert], PairingCheckResult(paired_crew_id=paired["crew_id"])

    age_this = age_on(crew_row["date_of_birth"], reference_date)
    age_paired = age_on(paired["date_of_birth"], reference_date)

    if _evaluate_pair_age(age_this, age_paired, domestic):
        alert = RuleAlert(
            rule_code="AE-CREW-PAIR-AGE-001_AGE_LIMIT",
            status=AlertStatus.ILLEGAL,
            severity="RED",
            message=(
                f"Age-pairing rule violated ({'domestic' if domestic else 'international'}): "
                f"{crew_row['crew_id']} is {age_this}, {paired['crew_id']} is {age_paired}. "
                + ("Both pilots are 65 or older." if domestic
                   else "At least one pilot is 65 or older.")
            ),
            calculated_value=f"{age_this}/{age_paired}",
            required_limit="Domestic: >=1 pilot under 65. International: both pilots under 65.",
        )
        return [alert], PairingCheckResult(paired_crew_id=paired["crew_id"])

    return [], PairingCheckResult(paired_crew_id=paired["crew_id"])


def _validate_new_duty(engine, crew_id: str, legs: List[FlightLeg], domestic: bool, role_assigned: str,
                        meal_provided: bool, snack_provided: bool,
                        flight_ids: Optional[List[int]] = None,
                        operating_position: Optional[str] = None,
                        prefetch=None):
    """
    Shared validation core for assign_crew_to_duty() (LM/ENGR, or a
    pilot filling the remaining seat of an already-real pair),
    assign_crew_to_new_flights() (Control Room's atomic flight+
    assignment, LM/ENGR only), and validate_pair() (each pilot's own
    individual FDP/rest/qualification check, called once per seat).
    Builds the duty, loads the crew member's existing history,
    validates — writes nothing.

    operating_position (2026-08-12, flight-deck crew package): None
    for LM/ENGR (not part of any pair) or when the pairing question is
    being handled entirely by the caller (validate_pair() does its own
    direct age check between two fresh candidates — see
    _check_crew_pairing_age()'s docstring for why that's not the same
    question this function's own pairing call answers).
    'COMMANDER'/'SECOND_PILOT' when this individual check needs to
    also ask "does a REAL, already-assigned partner make this legal" —
    _find_paired_pilot() looks up an actual committed row for the
    other seat, so this is only meaningful when one seat is already
    real, not when validating two brand-new candidates against each
    other.

    meal_provided/snack_provided: required, no default, same footing as
    the existing `domestic` parameter — both callers compute them the
    same way, from the flights that make up this duty (migrations/014,
    migrations/015, 2026-08-08). core/legality/pcaa_ano012_core.py's D25
    rule fires NEEDS_MANUAL_REVIEW for any FDP over 6h whose
    meal_provided is unknown; D2.18 fires a WARNING for any FDP over 4h
    whose snack_provided is False. This is what finally gives both real
    data instead of the dataclass default.

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

    flight_ids is optional and used only for the age-pairing check
    (see _check_crew_pairing_age()) — it's None for
    assign_crew_to_new_flights() (Control Room), which calls this
    BEFORE the flight(s) it's about to create exist, so there is
    structurally nothing yet for a pairing check to find.

    Returns (validation_result, new_duty, crew_member, crew_row,
    duty_result, pairing_info). Raises ValueError if crew_id doesn't
    exist or role_assigned doesn't match the crew member's registered
    role.
    """
    duty_result = build_duty(legs, domestic=domestic)

    crew_row = _get_crew_row(crew_id, prefetch)
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
        meal_provided=meal_provided,
        snack_provided=snack_provided,
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
            engine, crew_id, crew_row["base"], start=lookback_start,
            end=duty_result.debrief_time, prefetch=prefetch)
        # Exclude any existing record covering the EXACT SAME flight_ids
        # as the duty being validated here -- that's not real history,
        # it's this same duty already written (publish_window()
        # re-validating an already-PROPOSED pair is the one real caller
        # that hits this: without this filter, the pilot's own
        # already-committed row for these flights would sit alongside
        # the freshly-built candidate `new_duty` for the identical
        # report/debrief window, and the validator would see two
        # simultaneous duties with zero rest between them -- a
        # self-inflicted rest violation, not a genuine one. Fresh
        # assignments (assign_crew_to_duty()/assign_pair_to_duty() at
        # write time) never have an existing duty for these exact
        # flight_ids yet, so this is a no-op for them.
        if flight_ids:
            target_flight_set = set(flight_ids)
            existing_records = [r for r in existing_records if set(r["flight_ids"]) != target_flight_set]
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

    # Age-pairing (AE-CREW-PAIR-AGE-001) — reuses the same duty-level
    # `domestic` classification already computed above for the D7.1.2
    # buffer, per HANDOVER.md's own note that this should be "one
    # boolean, two consumers." reference_date is the rotation's first
    # operating date — the earliest departure among the duty's own
    # legs, not today's date and not the debrief date (unlike the
    # qualification gate, which deliberately checks against the END of
    # the duty — these two checks intentionally use different
    # reference dates for different reasons).
    reference_date = min(l.dep_time for l in legs).date()
    pairing_alerts, pairing_info = _check_crew_pairing_age(
        engine, operating_position, crew_row, flight_ids, domestic, reference_date)
    for alert in pairing_alerts:
        validation_result.add_alert(alert)

    return validation_result, new_duty, crew_member, crew_row, duty_result, pairing_info


# ------------------------------------------------------------------
# Core assignment — flight(s) already exist (Roster page)
# ------------------------------------------------------------------

def assign_crew_to_duty(crew_id: str, flight_ids: List[int], role_assigned: str,
                         app_user: Optional[str] = None,
                         roster_status: str = "PLANNED",
                         operating_position: Optional[str] = None,
                         prefetch=None,
                         audit_trials: bool = True,
                         dry_run: bool = False) -> AssignmentResult:
    """
    Assigns crew_id to the duty formed by flight_ids (in chronological
    order — the caller's decision which flights form one duty).
    domestic is read from the flights themselves, not asked again
    here — all flights in the duty must agree on it.

    audit_trials: as on assign_pair_to_duty() — see that docstring for
    what it does and the three things that bound it. Default True; only
    the roster generator's internal candidate search passes False.

    dry_run: run the whole validation and write nothing, returning the
    result the write would have produced (including new_duty's duty_id,
    so a caller can record the duty provisionally). Only
    generate_preview() passes it. It short-circuits the INSERT and
    nothing else — the qualification gate, the FTL gate, the partner
    lookup and the age-pairing check all still run, on the same inputs,
    in the same order.

    ILLEGAL blocks the save entirely (AssignmentResult.status=
    "REJECTED", nothing written). LEGAL/WARNING saves and returns
    AssignmentResult.status="ALLOWED", along with any downstream
    conflicts found on the crew member's other already-scheduled
    future duties.

    operating_position (2026-08-12, flight-deck crew package): LM/ENGR
    ignore this entirely (stays None, unchanged behavior). For CPT/FO,
    operating_position is REQUIRED, and a REAL, already-active pilot
    must currently hold the OTHER seat on these exact flight_ids — this
    function fills the REMAINING seat of an already-real pair (the
    pairing question has a real, committed answer to check against).
    It is NOT how a fresh pair gets its first two seats — that always
    goes through assign_pair_to_duty(), which validates and commits
    both together in one transaction, closing the defect where a
    solo pilot could previously commit as PLANNED before any partner
    was known. Raises ValueError for CPT/FO with no operating_position
    given, or with no real partner found on the other seat.

    roster_status: the value written to the new roster row's own
    status column (migrations/003, 009, 013 — currently 'PLANNED',
    'OPERATED', 'CANCELLED', 'DISRUPTED', 'NEEDS_REVIEW', 'PROPOSED')
    — a completely different thing from AssignmentResult.status above,
    which describes the OUTCOME of this call (REJECTED/NEEDS_REVIEW/
    ALLOWED), not the roster row's own lifecycle state. Defaults to
    'PLANNED', unchanged for every pre-existing call site (the Roster
    page). Phase 7's roster generator (2026-08-04) is the one caller
    that passes 'PROPOSED' — a draft assignment pending OCC review/
    publish (services/roster_generator_service.py's publish_window()),
    per the requirements doc's "draft -> OCC review -> publish, crew
    sees only published."
    """
    engine = get_engine()

    normalized_role = ROLE_SYNONYMS.get(role_assigned.strip().upper(), role_assigned.strip().upper())
    if normalized_role in ("CPT", "FO"):
        if operating_position not in ("COMMANDER", "SECOND_PILOT"):
            raise ValueError(
                "Pilots (CPT/FO) require operating_position ('COMMANDER' or "
                "'SECOND_PILOT') and a real partner already on the other seat — "
                "assigning a fresh pair goes through assign_pair_to_duty() instead, "
                "which validates and commits both seats together."
            )
        partner = _find_paired_pilot(engine, flight_ids, operating_position, crew_id)
        if partner is None:
            raise ValueError(
                f"No active pilot currently holds the other seat on these flights — "
                f"a fresh pair must be assigned together via assign_pair_to_duty(), "
                f"not assign_crew_to_duty()."
            )

    flights = [_get_flight_row(fid, prefetch) for fid in flight_ids]
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

    # Same all()-of-the-legs aggregation as `domestic` above — a duty
    # only "had a meal/snack provided" if every leg genuinely did.
    # flights.meal_provided/snack_provided are NOT NULL (migrations/014,
    # migrations/015), so these are always a real True/False.
    meal_provided = all(bool(f["meal_provided"]) for f in flights)
    snack_provided = all(bool(f["snack_provided"]) for f in flights)

    legs = [
        FlightLeg(
            dep_time=f["dep_time_actual"] if pd.notna(f["dep_time_actual"]) else f["dep_time_planned"],
            arr_time=f["arr_time_actual"] if pd.notna(f["arr_time_actual"]) else f["arr_time_planned"],
            origin=f["origin"], destination=f["destination"],
        )
        for f in flights
    ]

    validation_result, new_duty, crew_member, crew_row, duty_result, pairing_info = _validate_new_duty(
        engine, crew_id, legs, domestic, role_assigned, meal_provided, snack_provided,
        flight_ids=flight_ids, operating_position=operating_position, prefetch=prefetch)
    alert_summary = summarize_alerts(validation_result, target_duty_id=new_duty.duty_id)

    if validation_result.status == AlertStatus.ILLEGAL:
        if audit_trials:
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
            pairing_pending=pairing_info.pending,
            paired_crew_id=pairing_info.paired_crew_id,
            pairing_constraint=pairing_info.constraint_message,
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
        if audit_trials:
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
            pairing_pending=pairing_info.pending,
            paired_crew_id=pairing_info.paired_crew_id,
            pairing_constraint=pairing_info.constraint_message,
            alert_summary=alert_summary,
            computed_report_time=duty_result.report_time,
            computed_debrief_time=duty_result.debrief_time,
            computed_fdp_hours=duty_result.fdp_hours,
        )

    # Only LEGAL or WARNING reach here — passed the immediate gate.
    if dry_run:
        # The preview's answer, taken from the SAME call the write path
        # takes and at the same point in it. Everything above this line
        # ran; only the INSERT below is skipped. That is the whole
        # reason dry_run is a flag on this function rather than a
        # separate validate-only twin: a twin would be free to drift,
        # and "the preview said ALLOWED but accept rejected it" would
        # then have two possible causes instead of one.
        return AssignmentResult(
            status="ALLOWED",
            legality_status=validation_result.status.value,
            alerts=validation_result.alerts,
            alert_summary=alert_summary,
            duty_id=new_duty.duty_id,
            computed_report_time=duty_result.report_time,
            computed_debrief_time=duty_result.debrief_time,
            computed_fdp_hours=duty_result.fdp_hours,
            pairing_pending=pairing_info.pending,
            paired_crew_id=pairing_info.paired_crew_id,
            pairing_constraint=pairing_info.constraint_message,
        )

    # write to roster (one row per sector) and its audit record
    # together, in one transaction (Step 6, 2026-08-02): previously
    # these were 2 separate, independently-committed transactions, so
    # a crash between them left a committed roster row with no audit
    # trail for it — a real gap in a permanent regulatory record, even
    # though (unlike assign_crew_to_new_flights()) there's no flight
    # here to orphan. See audit_service.log_audit()'s conn parameter.
    roster_ids = []
    with engine.begin() as conn:
        _resolve_uncovered_seat(conn, flight_ids[0], operating_position)
        for fid in flight_ids:
            result = conn.execute(text("""
                INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                    report_time, debrief_time, fdp_hours, role_assigned, status, operating_position)
                VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                    :report_time, :debrief_time, :fdp_hours, :role_assigned, :roster_status, :operating_position)
                RETURNING roster_id
            """), {
                "crew_id": crew_id, "flight_id": fid, "duty_id": new_duty.duty_id,
                "roster_status": roster_status,
                "duty_date": duty_result.report_time.date(),
                "report_time": duty_result.report_time, "debrief_time": duty_result.debrief_time,
                "fdp_hours": duty_result.fdp_hours, "role_assigned": role_assigned,
                "operating_position": operating_position,
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
            conn=conn,
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
        pairing_pending=pairing_info.pending,
        paired_crew_id=pairing_info.paired_crew_id,
        pairing_constraint=pairing_info.constraint_message,
    )


# ------------------------------------------------------------------
# Atomic flight-creation + assignment (Control Room, ad-hoc/charter)
# ------------------------------------------------------------------

# NO UI PATH AS OF 2026-08-20 — deliberately kept, do not delete as
# dead code.
#
# pages/1_Control_Room.py's single-crew branch was its only caller and
# was removed: there is no such thing as a flight operated by one crew
# member, and the one combination that still worked ("Other" crew member
# assigned role "Other") created a flight with no flight deck at all.
#
# This function stays because its TESTS are the explicit statement of a
# guarantee the pair model depends on, and they document it by contrast
# rather than by assertion elsewhere:
# test_assign_crew_to_new_flights_rejects_pilots_outright pins that a
# solo pilot assignment cannot bypass pair-atomicity. Delete the
# function and that assertion has nowhere to live.
#
# If a future client genuinely needs single-crew ad-hoc flights (FTLguard
# still supports LM/ENGR crew records generally), this is the path — it
# is unreachable from Air Eagle's UI, not unsupported.
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

    LM/ENGR only (2026-08-12, flight-deck crew package) — raises
    ValueError for CPT/FO. Unlike assign_crew_to_duty(), there is no
    "fill the remaining seat of an already-real pair" case here: the
    flights don't exist yet, so a real partner could never already be
    on the other seat. A fresh ad-hoc pair goes through
    assign_pair_to_new_flights() instead, which creates the flight(s)
    and both seats together, atomically.

    flights_data: list of dicts with the same shape add_flight()
    expects (origin, destination, dep_time_planned, arr_time_planned,
    domestic, and optionally flight_no/aircraft/cargo_dg/remarks) —
    NOT yet-created flight_ids, since nothing is created until this
    passes the legality gate.

    Returns (AssignmentResult, flight_ids). flight_ids is empty on
    REJECTED.
    """
    engine = get_engine()

    normalized_role = ROLE_SYNONYMS.get(role_assigned.strip().upper(), role_assigned.strip().upper())
    if normalized_role in ("CPT", "FO"):
        raise ValueError(
            "Pilots (CPT/FO) must be assigned via assign_pair_to_new_flights(), not "
            "assign_crew_to_new_flights() — a solo ad-hoc pilot assignment bypasses "
            "pair-atomicity the same way a solo scheduled-flight assignment would."
        )

    # Same duty-level classification rule as assign_crew_to_duty() —
    # see the comment there. Any international sector makes the
    # whole duty international for D7.1.2 buffer purposes.
    domestic = all(bool(f["domestic"]) for f in flights_data)

    # flights_data are plain caller-supplied dicts, not yet-inserted
    # rows — a caller may omit the key entirely. Default True here
    # explicitly (not just bool(None)=False) to mirror flights.
    # meal_provided/snack_provided's own DEFAULT TRUE: this function
    # gates legality BEFORE any DB write happens, so the pre-insert
    # computation here must agree with what the row will actually end
    # up holding once written, or the gate and the stored data would
    # silently disagree.
    meal_provided = all(bool(f.get("meal_provided", True)) for f in flights_data)
    snack_provided = all(bool(f.get("snack_provided", True)) for f in flights_data)

    legs = [
        FlightLeg(dep_time=f["dep_time_planned"], arr_time=f["arr_time_planned"],
                  origin=f["origin"], destination=f["destination"])
        for f in flights_data
    ]

    validation_result, new_duty, crew_member, crew_row, duty_result, pairing_info = _validate_new_duty(
        engine, crew_id, legs, domestic, role_assigned, meal_provided, snack_provided,
        flight_ids=None)
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
            pairing_pending=pairing_info.pending,
            paired_crew_id=pairing_info.paired_crew_id,
            pairing_constraint=pairing_info.constraint_message,
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
            pairing_pending=pairing_info.pending,
            paired_crew_id=pairing_info.paired_crew_id,
            pairing_constraint=pairing_info.constraint_message,
        ), []

    # Only LEGAL or WARNING reach here — now actually create the
    # flight(s) and the assignment together, ONE transaction covering
    # both inserts and both audit records. Previously these were 4
    # separate, independently-committed transactions (flight insert,
    # FLIGHT_ADDED audit, roster insert, ASSIGNMENT_CREATED audit) — a
    # crash between the first and third left a real, committed,
    # uncrewed flight in Flight Log, exactly the orphan the gate above
    # exists to prevent, just relocated to a later failure window
    # (Step 6, 2026-08-02). Folding everything into one
    # `engine.begin()` means all four writes commit together or none
    # do — see audit_service.log_audit()'s conn parameter.
    flight_ids = []
    roster_ids = []
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
            conn=conn,
        )

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
            conn=conn,
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
        pairing_pending=pairing_info.pending,
        paired_crew_id=pairing_info.paired_crew_id,
        pairing_constraint=pairing_info.constraint_message,
    ), flight_ids


# ------------------------------------------------------------------
# Flight-deck pair assignment (2026-08-12) — the atomic Commander +
# Second Pilot path. Closes the defect where a solo pilot could
# commit as PLANNED before any partner was known: both seats are
# validated together and written together, in one transaction, or
# neither is.
# ------------------------------------------------------------------

_STATUS_PRECEDENCE = {
    AlertStatus.LEGAL: 0, AlertStatus.WARNING: 1,
    AlertStatus.NEEDS_MANUAL_REVIEW: 2, AlertStatus.ILLEGAL: 3,
}


def _validate_pair_internal(engine, commander_crew_id: str, second_pilot_crew_id: str,
                             flight_ids: List[int], prefetch=None):
    """
    Shared core for validate_pair() (read-only) and
    assign_pair_to_duty()/assign_pair_to_new_flights() (write path) —
    the write path needs the SAME Duty/duty_result objects (same
    duty_ids) this produces, not just a pass/fail summary, so it's
    factored out here rather than having the write path re-derive
    them separately (which would generate different duty_ids than
    whatever a caller's own prior validate_pair() dry-run showed).

    Returns a dict with everything either caller needs. Raises
    ValueError for the same caller-error conditions validate_pair()
    documents (same crew_id twice, missing crew_id, wrong grade for
    seat, missing flight_id).
    """
    if commander_crew_id == second_pilot_crew_id:
        raise ValueError("Commander and Second Pilot must be different crew members")

    commander_row = _get_crew_row(commander_crew_id, prefetch)
    if commander_row is None:
        raise ValueError(f"No crew member with crew_id={commander_crew_id}")
    second_pilot_row = _get_crew_row(second_pilot_crew_id, prefetch)
    if second_pilot_row is None:
        raise ValueError(f"No crew member with crew_id={second_pilot_crew_id}")

    if commander_row["role"] not in SEAT_ELIGIBLE_GRADES["COMMANDER"]:
        raise ValueError(
            f"{commander_crew_id} is graded {commander_row['role']}, not eligible for "
            f"Commander (must be CPT)"
        )
    if second_pilot_row["role"] not in SEAT_ELIGIBLE_GRADES["SECOND_PILOT"]:
        raise ValueError(
            f"{second_pilot_crew_id} is graded {second_pilot_row['role']}, not eligible "
            f"for Second Pilot (must be CPT or FO)"
        )

    flights = [_get_flight_row(fid, prefetch) for fid in flight_ids]
    missing = [fid for fid, f in zip(flight_ids, flights) if f is None]
    if missing:
        raise ValueError(f"Flight_id(s) not found: {missing}")

    domestic = all(bool(f["domestic"]) for f in flights)
    meal_provided = all(bool(f["meal_provided"]) for f in flights)
    snack_provided = all(bool(f["snack_provided"]) for f in flights)
    legs = [
        FlightLeg(
            dep_time=f["dep_time_actual"] if pd.notna(f["dep_time_actual"]) else f["dep_time_planned"],
            arr_time=f["arr_time_actual"] if pd.notna(f["arr_time_actual"]) else f["arr_time_planned"],
            origin=f["origin"], destination=f["destination"],
        )
        for f in flights
    ]

    # operating_position=None on both individual checks, deliberately:
    # neither pilot is written yet, so _check_crew_pairing_age()'s own
    # DB lookup would find nothing for either and silently skip the
    # check this pair most needs. The age-pairing check between these
    # two ACTUAL proposed candidates is done directly below instead,
    # using the same unchanged age_on()/_evaluate_pair_age() the
    # DB-lookup path itself uses internally — same math, just applied
    # to the two real candidates instead of a database lookup.
    commander_result, commander_duty, _, _, commander_duty_result, _ = _validate_new_duty(
        engine, commander_crew_id, legs, domestic, "CPT", meal_provided, snack_provided,
        flight_ids=flight_ids, operating_position=None, prefetch=prefetch)
    second_pilot_result, second_pilot_duty, _, _, second_pilot_duty_result, _ = _validate_new_duty(
        engine, second_pilot_crew_id, legs, domestic, second_pilot_row["role"], meal_provided, snack_provided,
        flight_ids=flight_ids, operating_position=None, prefetch=prefetch)

    reference_date = min(l.dep_time for l in legs).date()
    pair_alerts: List[RuleAlert] = []
    if pd.isna(commander_row["date_of_birth"]) or pd.isna(second_pilot_row["date_of_birth"]):
        pair_alerts.append(RuleAlert(
            rule_code="AE-CREW-PAIR-AGE-001_DOB_MISSING",
            status=AlertStatus.NEEDS_MANUAL_REVIEW,
            severity="YELLOW",
            message=(
                f"Cannot evaluate the age-pairing rule for {commander_crew_id} + "
                f"{second_pilot_crew_id}: date of birth is missing for at least one pilot."
            ),
            calculated_value="DOB missing",
            required_limit="Both pilots' date_of_birth required",
        ))
    else:
        age_commander = age_on(commander_row["date_of_birth"], reference_date)
        age_second_pilot = age_on(second_pilot_row["date_of_birth"], reference_date)
        if _evaluate_pair_age(age_commander, age_second_pilot, domestic):
            pair_alerts.append(RuleAlert(
                rule_code="AE-CREW-PAIR-AGE-001_AGE_LIMIT",
                status=AlertStatus.ILLEGAL,
                severity="RED",
                message=(
                    f"Age-pairing rule violated ({'domestic' if domestic else 'international'}): "
                    f"{commander_crew_id} is {age_commander}, {second_pilot_crew_id} is {age_second_pilot}. "
                    + ("Both pilots are 65 or older." if domestic
                       else "At least one pilot is 65 or older.")
                ),
                calculated_value=f"{age_commander}/{age_second_pilot}",
                required_limit="Domestic: >=1 pilot under 65. International: both pilots under 65.",
            ))

    pair_status = AlertStatus.LEGAL
    for alert in pair_alerts:
        if _STATUS_PRECEDENCE[alert.status] > _STATUS_PRECEDENCE[pair_status]:
            pair_status = alert.status

    overall_status = max(
        [commander_result.status, second_pilot_result.status, pair_status],
        key=lambda s: _STATUS_PRECEDENCE[s],
    )

    return {
        "overall_status": overall_status,
        "commander_row": commander_row, "second_pilot_row": second_pilot_row,
        "commander_result": commander_result, "second_pilot_result": second_pilot_result,
        "commander_duty": commander_duty, "second_pilot_duty": second_pilot_duty,
        "commander_duty_result": commander_duty_result, "second_pilot_duty_result": second_pilot_duty_result,
        "pair_alerts": pair_alerts, "domestic": domestic,
    }


def validate_pair(commander_crew_id: str, second_pilot_crew_id: str,
                   flight_ids: List[int]) -> PairValidationResult:
    """
    Dry-run: validates both seats of a flight-deck pair together,
    writes nothing. The one choke point both assign_pair_to_duty()/
    assign_pair_to_new_flights() and the roster generator's own pair
    search go through before ever committing — see
    _validate_pair_internal()'s own docstring for the shared core, and
    that function's inline comment for why the age-pairing check here
    is direct (comparing the two actual candidates) rather than
    delegated to _check_crew_pairing_age()'s DB-lookup path.
    """
    engine = get_engine()
    core = _validate_pair_internal(engine, commander_crew_id, second_pilot_crew_id, flight_ids)

    commander_summary = summarize_alerts(
        core["commander_result"], target_duty_id=core["commander_duty"].duty_id)
    second_pilot_summary = summarize_alerts(
        core["second_pilot_result"], target_duty_id=core["second_pilot_duty"].duty_id)

    return PairValidationResult(
        status=core["overall_status"].value,
        commander_crew_id=commander_crew_id,
        second_pilot_crew_id=second_pilot_crew_id,
        commander_status=core["commander_result"].status.value,
        second_pilot_status=core["second_pilot_result"].status.value,
        commander_alerts=core["commander_result"].alerts,
        second_pilot_alerts=core["second_pilot_result"].alerts,
        pair_alerts=core["pair_alerts"],
        commander_alert_summary=commander_summary,
        second_pilot_alert_summary=second_pilot_summary,
        commander_computed_report_time=core["commander_duty_result"].report_time,
        commander_computed_debrief_time=core["commander_duty_result"].debrief_time,
        commander_computed_fdp_hours=core["commander_duty_result"].fdp_hours,
        second_pilot_computed_report_time=core["second_pilot_duty_result"].report_time,
        second_pilot_computed_debrief_time=core["second_pilot_duty_result"].debrief_time,
        second_pilot_computed_fdp_hours=core["second_pilot_duty_result"].fdp_hours,
    )


def _resolve_uncovered_seat(conn, flight_id: int, operating_position: Optional[str]) -> None:
    """Marks any open uncovered_seats row for this seat resolved, now
    that a real roster row fills it — keeps the table from showing a
    seat as still-uncovered after it's genuinely been filled (by the
    generator's own retry, or a controller assigning a replacement).
    No-op for LM/ENGR (operating_position None) or ad-hoc Control Room
    flights (rotation_instance_id NULL — uncovered_seats never had a
    row for those in the first place, nothing to resolve)."""
    if operating_position is None:
        return
    conn.execute(text("""
        UPDATE uncovered_seats SET resolved_at = NOW()
        WHERE resolved_at IS NULL AND operating_position = :operating_position
          AND rotation_instance_id = (SELECT rotation_instance_id FROM flights WHERE flight_id = :flight_id)
    """), {"operating_position": operating_position, "flight_id": flight_id})


def _write_pair_rows(conn, crew_id, role_assigned, operating_position, duty, duty_result,
                      flight_ids, roster_status, app_user):
    roster_ids = []
    _resolve_uncovered_seat(conn, flight_ids[0], operating_position)
    for fid in flight_ids:
        result = conn.execute(text("""
            INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                report_time, debrief_time, fdp_hours, role_assigned, status, operating_position)
            VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                :report_time, :debrief_time, :fdp_hours, :role_assigned, :roster_status, :operating_position)
            RETURNING roster_id
        """), {
            "crew_id": crew_id, "flight_id": fid, "duty_id": duty.duty_id,
            "roster_status": roster_status,
            "duty_date": duty_result.report_time.date(),
            "report_time": duty_result.report_time, "debrief_time": duty_result.debrief_time,
            "fdp_hours": duty_result.fdp_hours, "role_assigned": role_assigned,
            "operating_position": operating_position,
        })
        roster_ids.append(result.scalar())
    log_audit(
        action_type="ASSIGNMENT_CREATED",
        affected_crew=crew_id, affected_flight=flight_ids[0], affected_duty=duty.duty_id,
        app_user=app_user, conn=conn,
    )
    return roster_ids


def assign_pair_to_duty(commander_crew_id: str, second_pilot_crew_id: str, flight_ids: List[int],
                         app_user: Optional[str] = None,
                         roster_status: str = "PLANNED",
                         prefetch=None,
                         audit_trials: bool = True,
                         dry_run: bool = False) -> PairAssignmentResult:
    """
    Assigns BOTH seats of a flight-deck pair to flight_ids (flights
    that already exist — Roster page / roster generator), atomically:
    validate_pair()'s underlying check runs for both pilots together,
    and either both are written (all sectors, both pilots, one
    transaction) or neither is — closing the defect where a solo pilot
    previously committed as PLANNED before any partner was validated.

    roster_status: same meaning as assign_crew_to_duty()'s own
    parameter — the roster row's own lifecycle state, not this
    function's outcome. Phase 7's roster generator passes 'PROPOSED'.
    dry_run: as on assign_crew_to_duty() — full validation, no write,
    duty_ids returned so generate_preview() can record the pair
    provisionally. Both seats or neither, in the dry-run case too:
    there is no state in which the preview holds a commander without
    the second pilot it was validated against.
    audit_trials: whether a REJECTED / HELD_FOR_REVIEW outcome leaves an
    audit row. Default True, and the default is the safe direction —
    silence is never what a caller gets by saying nothing, so a page
    written next year is fully audited without knowing this parameter
    exists. ONLY the roster generator passes False, for its internal
    candidate search: those are options the algorithm considered and
    discarded, not decisions anybody made, and one run wrote 2,954 rows
    into a production audit_log that held 165 (2026-08-26).

    Three things bound it, because a flag that a page could set would
    let a REAL rejection go unaudited:

      * It gates ONLY the trial-outcome rows. ASSIGNMENT_CREATED is
        written unconditionally, outside any branch this can reach, so
        no assignment can ever be created without an audit row —
        whatever any caller passes.
      * `tests/test_audit_scope.py` fails the suite if any file outside
        services/roster_generator_service.py passes it at all, and if
        the generator passes anything but the literal False. Pages
        cannot acquire it by accident or by copy-paste.
      * Why a seat went unfilled is recorded independently, by
        `uncovered_seats.reason` (ROSTER_GENERATION_SEAT_UNCOVERED) —
        which is now the ONLY record of it, and so is verified
        byte-identical under both settings.
    """
    engine = get_engine()
    core = _validate_pair_internal(engine, commander_crew_id, second_pilot_crew_id, flight_ids,
                                  prefetch=prefetch)
    overall_status = core["overall_status"]

    commander_summary = summarize_alerts(core["commander_result"], target_duty_id=core["commander_duty"].duty_id)
    second_pilot_summary = summarize_alerts(
        core["second_pilot_result"], target_duty_id=core["second_pilot_duty"].duty_id)

    if overall_status == AlertStatus.ILLEGAL:
        commander_reason = build_audit_reason(commander_summary, frozenset({AlertStatus.ILLEGAL}))
        second_pilot_reason = build_audit_reason(second_pilot_summary, frozenset({AlertStatus.ILLEGAL}))
        if audit_trials:
            log_audit(
                action_type="PAIR_ASSIGNMENT_REJECTED",
                affected_crew=commander_crew_id, affected_flight=flight_ids[0],
                legality_result=overall_status.value,
                warning_or_failure_reason="; ".join(r for r in (commander_reason, second_pilot_reason) if r),
                app_user=app_user,
            )
    elif overall_status == AlertStatus.NEEDS_MANUAL_REVIEW:
        if audit_trials:
            log_audit(
                action_type="PAIR_ASSIGNMENT_HELD_FOR_REVIEW",
                affected_crew=commander_crew_id, affected_flight=flight_ids[0],
                legality_result=overall_status.value,
                app_user=app_user,
            )

    validation = PairValidationResult(
        status=overall_status.value,
        commander_crew_id=commander_crew_id, second_pilot_crew_id=second_pilot_crew_id,
        commander_status=core["commander_result"].status.value,
        second_pilot_status=core["second_pilot_result"].status.value,
        commander_alerts=core["commander_result"].alerts,
        second_pilot_alerts=core["second_pilot_result"].alerts,
        pair_alerts=core["pair_alerts"],
        commander_alert_summary=commander_summary,
        second_pilot_alert_summary=second_pilot_summary,
        commander_computed_report_time=core["commander_duty_result"].report_time,
        commander_computed_debrief_time=core["commander_duty_result"].debrief_time,
        commander_computed_fdp_hours=core["commander_duty_result"].fdp_hours,
        second_pilot_computed_report_time=core["second_pilot_duty_result"].report_time,
        second_pilot_computed_debrief_time=core["second_pilot_duty_result"].debrief_time,
        second_pilot_computed_fdp_hours=core["second_pilot_duty_result"].fdp_hours,
    )

    if overall_status in (AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW):
        return PairAssignmentResult(
            status="REJECTED" if overall_status == AlertStatus.ILLEGAL else "NEEDS_REVIEW",
            validation=validation,
            commander_duty_id=core["commander_duty"].duty_id,
            second_pilot_duty_id=core["second_pilot_duty"].duty_id,
        )

    # LEGAL or WARNING.
    if dry_run:
        # See assign_crew_to_duty()'s dry_run note. Downstream-conflict
        # detection is skipped too: it asks "what did writing this duty
        # break later", and nothing was written. The preview's own
        # cross-rotation question is answered by the provisional union
        # feeding the gate above, not here.
        return PairAssignmentResult(
            status="ALLOWED",
            validation=validation,
            commander_duty_id=core["commander_duty"].duty_id,
            second_pilot_duty_id=core["second_pilot_duty"].duty_id,
        )

    # Both seats, all sectors, one transaction.
    with engine.begin() as conn:
        commander_roster_ids = _write_pair_rows(
            conn, commander_crew_id, "CPT", "COMMANDER",
            core["commander_duty"], core["commander_duty_result"], flight_ids, roster_status, app_user)
        second_pilot_roster_ids = _write_pair_rows(
            conn, second_pilot_crew_id, core["second_pilot_row"]["role"], "SECOND_PILOT",
            core["second_pilot_duty"], core["second_pilot_duty_result"], flight_ids, roster_status, app_user)

    commander_conflicts = _check_downstream_impact(
        engine, commander_crew_id, core["commander_row"]["base"],
        _crew_member(core["commander_row"]), core["commander_duty"])
    second_pilot_conflicts = _check_downstream_impact(
        engine, second_pilot_crew_id, core["second_pilot_row"]["base"],
        _crew_member(core["second_pilot_row"]), core["second_pilot_duty"])

    return PairAssignmentResult(
        status="ALLOWED",
        validation=validation,
        commander_roster_ids=commander_roster_ids,
        second_pilot_roster_ids=second_pilot_roster_ids,
        commander_duty_id=core["commander_duty"].duty_id,
        second_pilot_duty_id=core["second_pilot_duty"].duty_id,
        commander_downstream_conflicts=commander_conflicts,
        second_pilot_downstream_conflicts=second_pilot_conflicts,
    )


def assign_pair_to_new_flights(commander_crew_id: str, second_pilot_crew_id: str,
                                flights_data: List[dict],
                                app_user: Optional[str] = None) -> tuple:
    """
    Control Room's pair-aware ad-hoc path: creates flight(s) AND both
    seats together, gated by legality BEFORE anything is written —
    same atomicity guarantee assign_crew_to_new_flights() already
    gives LM/ENGR, extended to a real flight-deck pair. There is no
    "fill the remaining seat of an already-real pair" case here (the
    flights don't exist yet, so a real partner could never already be
    on the other seat) — every ad-hoc pilot pairing is a fresh pair.

    Returns (PairAssignmentResult, flight_ids). flight_ids is empty on
    REJECTED/NEEDS_REVIEW.
    """
    engine = get_engine()

    commander_row = crew_service.get_crew(commander_crew_id)
    if commander_row is None:
        raise ValueError(f"No crew member with crew_id={commander_crew_id}")
    second_pilot_row = crew_service.get_crew(second_pilot_crew_id)
    if second_pilot_row is None:
        raise ValueError(f"No crew member with crew_id={second_pilot_crew_id}")
    if commander_crew_id == second_pilot_crew_id:
        raise ValueError("Commander and Second Pilot must be different crew members")
    if commander_row["role"] not in SEAT_ELIGIBLE_GRADES["COMMANDER"]:
        raise ValueError(f"{commander_crew_id} is graded {commander_row['role']}, not eligible for Commander")
    if second_pilot_row["role"] not in SEAT_ELIGIBLE_GRADES["SECOND_PILOT"]:
        raise ValueError(f"{second_pilot_crew_id} is graded {second_pilot_row['role']}, not eligible for Second Pilot")

    domestic = all(bool(f["domestic"]) for f in flights_data)
    meal_provided = all(bool(f.get("meal_provided", True)) for f in flights_data)
    snack_provided = all(bool(f.get("snack_provided", True)) for f in flights_data)
    legs = [
        FlightLeg(dep_time=f["dep_time_planned"], arr_time=f["arr_time_planned"],
                  origin=f["origin"], destination=f["destination"])
        for f in flights_data
    ]

    commander_result, commander_duty, _, _, commander_duty_result, _ = _validate_new_duty(
        engine, commander_crew_id, legs, domestic, "CPT", meal_provided, snack_provided,
        flight_ids=None, operating_position=None)
    second_pilot_result, second_pilot_duty, _, _, second_pilot_duty_result, _ = _validate_new_duty(
        engine, second_pilot_crew_id, legs, domestic, second_pilot_row["role"], meal_provided, snack_provided,
        flight_ids=None, operating_position=None)

    reference_date = min(l.dep_time for l in legs).date()
    pair_alerts: List[RuleAlert] = []
    if pd.isna(commander_row["date_of_birth"]) or pd.isna(second_pilot_row["date_of_birth"]):
        pair_alerts.append(RuleAlert(
            rule_code="AE-CREW-PAIR-AGE-001_DOB_MISSING", status=AlertStatus.NEEDS_MANUAL_REVIEW,
            severity="YELLOW",
            message=(f"Cannot evaluate the age-pairing rule for {commander_crew_id} + "
                      f"{second_pilot_crew_id}: date of birth is missing for at least one pilot."),
            calculated_value="DOB missing", required_limit="Both pilots' date_of_birth required",
        ))
    else:
        age_commander = age_on(commander_row["date_of_birth"], reference_date)
        age_second_pilot = age_on(second_pilot_row["date_of_birth"], reference_date)
        if _evaluate_pair_age(age_commander, age_second_pilot, domestic):
            pair_alerts.append(RuleAlert(
                rule_code="AE-CREW-PAIR-AGE-001_AGE_LIMIT", status=AlertStatus.ILLEGAL, severity="RED",
                message=(
                    f"Age-pairing rule violated ({'domestic' if domestic else 'international'}): "
                    f"{commander_crew_id} is {age_commander}, {second_pilot_crew_id} is {age_second_pilot}. "
                    + ("Both pilots are 65 or older." if domestic else "At least one pilot is 65 or older.")
                ),
                calculated_value=f"{age_commander}/{age_second_pilot}",
                required_limit="Domestic: >=1 pilot under 65. International: both pilots under 65.",
            ))

    pair_status = AlertStatus.LEGAL
    for alert in pair_alerts:
        if _STATUS_PRECEDENCE[alert.status] > _STATUS_PRECEDENCE[pair_status]:
            pair_status = alert.status
    overall_status = max(
        [commander_result.status, second_pilot_result.status, pair_status],
        key=lambda s: _STATUS_PRECEDENCE[s],
    )

    commander_summary = summarize_alerts(commander_result, target_duty_id=commander_duty.duty_id)
    second_pilot_summary = summarize_alerts(second_pilot_result, target_duty_id=second_pilot_duty.duty_id)

    validation = PairValidationResult(
        status=overall_status.value,
        commander_crew_id=commander_crew_id, second_pilot_crew_id=second_pilot_crew_id,
        commander_status=commander_result.status.value, second_pilot_status=second_pilot_result.status.value,
        commander_alerts=commander_result.alerts, second_pilot_alerts=second_pilot_result.alerts,
        pair_alerts=pair_alerts,
        commander_alert_summary=commander_summary, second_pilot_alert_summary=second_pilot_summary,
        commander_computed_report_time=commander_duty_result.report_time,
        commander_computed_debrief_time=commander_duty_result.debrief_time,
        commander_computed_fdp_hours=commander_duty_result.fdp_hours,
        second_pilot_computed_report_time=second_pilot_duty_result.report_time,
        second_pilot_computed_debrief_time=second_pilot_duty_result.debrief_time,
        second_pilot_computed_fdp_hours=second_pilot_duty_result.fdp_hours,
    )

    if overall_status in (AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW):
        # Same "every legality outcome gets an audit record, even when
        # nothing is saved" guarantee assign_pair_to_duty() and the
        # LM/ENGR ad-hoc path (assign_crew_to_new_flights()) already
        # both provide — ADHOC_PAIR_* naming mirrors ADHOC_FLIGHT_*'s
        # existing convention for the non-seat ad-hoc path.
        if overall_status == AlertStatus.ILLEGAL:
            commander_reason = build_audit_reason(commander_summary, frozenset({AlertStatus.ILLEGAL}))
            second_pilot_reason = build_audit_reason(second_pilot_summary, frozenset({AlertStatus.ILLEGAL}))
            log_audit(
                action_type="ADHOC_PAIR_REJECTED",
                affected_crew=commander_crew_id,
                legality_result=overall_status.value,
                warning_or_failure_reason="; ".join(r for r in (commander_reason, second_pilot_reason) if r),
                app_user=app_user,
            )
        else:
            log_audit(
                action_type="ADHOC_PAIR_HELD_FOR_REVIEW",
                affected_crew=commander_crew_id,
                legality_result=overall_status.value,
                app_user=app_user,
            )
        return PairAssignmentResult(
            status="REJECTED" if overall_status == AlertStatus.ILLEGAL else "NEEDS_REVIEW",
            validation=validation,
            commander_duty_id=commander_duty.duty_id, second_pilot_duty_id=second_pilot_duty.duty_id,
        ), []

    # LEGAL or WARNING — create the flight(s) AND both seats together,
    # one transaction, same "all four writes commit together or none
    # do" guarantee assign_crew_to_new_flights() already established.
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

        log_audit(action_type="FLIGHT_ADDED", affected_flight=flight_ids[0],
                   changed_state=str(flights_data), data_source="control_room",
                   app_user=app_user, conn=conn)

        commander_roster_ids = _write_pair_rows(
            conn, commander_crew_id, "CPT", "COMMANDER",
            commander_duty, commander_duty_result, flight_ids, "PLANNED", app_user)
        second_pilot_roster_ids = _write_pair_rows(
            conn, second_pilot_crew_id, second_pilot_row["role"], "SECOND_PILOT",
            second_pilot_duty, second_pilot_duty_result, flight_ids, "PLANNED", app_user)

    commander_conflicts = _check_downstream_impact(
        engine, commander_crew_id, commander_row["base"], _crew_member(commander_row), commander_duty)
    second_pilot_conflicts = _check_downstream_impact(
        engine, second_pilot_crew_id, second_pilot_row["base"], _crew_member(second_pilot_row), second_pilot_duty)

    return PairAssignmentResult(
        status="ALLOWED", validation=validation,
        commander_roster_ids=commander_roster_ids, second_pilot_roster_ids=second_pilot_roster_ids,
        commander_duty_id=commander_duty.duty_id, second_pilot_duty_id=second_pilot_duty.duty_id,
        commander_downstream_conflicts=commander_conflicts,
        second_pilot_downstream_conflicts=second_pilot_conflicts,
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
            # find_legal_candidates_for_seat() now returns a richer
            # CandidateStatus per candidate (2026-08-12) — DownstreamConflict.
            # candidates keeps its existing List[str] shape (not part
            # of this piece's requirements; minimal-risk pass-through
            # rather than a speculative redesign), taking only the
            # LEGAL/WARNING crew_ids, same "selectable" bar
            # find_legal_candidates_for_seat()'s own docstring
            # describes for its callers generally.
            candidate_statuses = find_legal_candidates_for_seat(
                record["flight_ids"], record["role_assigned"],
                operating_position=record["operating_position"], exclude_crew_id=crew_id)
            candidates = [c.crew_id for c in candidate_statuses
                          if c.status in (AlertStatus.LEGAL.value, AlertStatus.WARNING.value)]
            conflicts.append(DownstreamConflict(
                duty_id=future_duty.duty_id,
                flight_ids=record["flight_ids"],
                role_assigned=record["role_assigned"],
                report_time=future_duty.start_utc,
                debrief_time=future_duty.end_utc,
                candidates=candidates,
            ))

    return conflicts


def find_legal_candidates_for_seat(flight_ids: List[int], role_assigned: str,
                                    operating_position: Optional[str] = None,
                                    exclude_crew_id: Optional[str] = None) -> List[CandidateStatus]:
    """
    Searches all active, seat-eligible crew and returns a
    CandidateStatus per candidate — replaces find_legal_candidates_
    for_duty() (2026-08-12, flight-deck crew package), whose bare
    List[str] let a NEEDS_MANUAL_REVIEW candidate (e.g. missing DG
    expiry) show up indistinguishable from a genuinely LEGAL one, only
    for the real gate to refuse them at actual assignment time —
    confirmed as a real, reproduced defect, not hypothetical. Every
    evaluated candidate is returned now, not just the passing ones —
    callers filter to LEGAL + permitted WARNING for "selectable," and
    can still show NEEDS_MANUAL_REVIEW/ILLEGAL candidates separately
    with their real reason, never mislabeled "legal." Does not check
    location (not tracked — see HANDOVER.md) or write anything;
    read-only candidate search.

    operating_position (None for LM/ENGR, 'COMMANDER'/'SECOND_PILOT'
    for a pilot seat search): when given, the candidate pool is
    SEAT_ELIGIBLE_GRADES[operating_position] (Commander: CPT only;
    Second Pilot: CPT or FO) rather than an exact role_assigned match,
    and the age-pairing check is asked against operating_position (via
    _check_crew_pairing_age()'s real-partner DB lookup — this searches
    for who could legally fill ONE seat given whoever REALLY holds the
    other, e.g. downstream-conflict replacement search or the
    generator's own retry — never two brand-new candidates against
    each other; that's validate_pair()'s job). When None (LM/ENGR),
    behavior is unchanged from before: exact role_assigned match, no
    pairing check (_check_crew_pairing_age() no-ops on
    operating_position=None).

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
    if operating_position is not None:
        candidates_pool = all_crew[all_crew["role"].isin(SEAT_ELIGIBLE_GRADES[operating_position])]
    else:
        candidates_pool = all_crew[all_crew["role"] == role_assigned]

    engine = get_engine()
    flights = [flight_service.get_flight(fid) for fid in flight_ids]
    if any(f is None for f in flights):
        raise ValueError("One or more flight_ids not found")

    # Same duty-level classification rule as assign_crew_to_duty() —
    # any international sector makes the whole duty international.
    domestic = all(bool(f["domestic"]) for f in flights)
    # Same all()-of-the-legs aggregation as `domestic` above.
    meal_provided = all(bool(f["meal_provided"]) for f in flights)
    snack_provided = all(bool(f["snack_provided"]) for f in flights)
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
    # Age-pairing (AE-CREW-PAIR-AGE-001, 2026-08-08) needs the
    # rotation's first operating date, same computation
    # _validate_new_duty() already uses — hoisted here since it
    # doesn't vary per candidate, same treatment as domestic/
    # meal_provided/snack_provided/duty_date above.
    reference_date = min(l.dep_time for l in legs).date()

    if role_assigned in FTL_EXEMPT_ROLES:
        statuses = []
        for _, crew_row in candidates_pool.iterrows():
            if exclude_crew_id and crew_row["crew_id"] == exclude_crew_id:
                continue
            qualification_alerts = _check_crew_qualifications(crew_row, duty_date)
            worst = AlertStatus.LEGAL
            for a in qualification_alerts:
                if _STATUS_PRECEDENCE[a.status] > _STATUS_PRECEDENCE[worst]:
                    worst = a.status
            blocking = [a.message for a in qualification_alerts
                        if a.status in (AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW)]
            warnings = [a.message for a in qualification_alerts if a.status == AlertStatus.WARNING]
            statuses.append(CandidateStatus(
                crew_id=crew_row["crew_id"], status=worst.value,
                blocking_reasons=blocking, warnings=warnings,
            ))
        return statuses

    statuses = []
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
            meal_provided=meal_provided,
            snack_provided=snack_provided,
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

        # Age-pairing: would this candidate legally pair with whoever
        # REALLY holds the other seat of this duty? Reuses
        # _check_crew_pairing_age() wholesale, keyed on
        # operating_position now (2026-08-12) rather than grade — see
        # that function's own docstring for why this DB-lookup path is
        # the right one here (one seat is real) and not the same
        # question validate_pair() answers (two fresh candidates).
        # AGE_LIMIT (ILLEGAL) excludes the candidate below, same as
        # any other ILLEGAL finding here; DOB_MISSING (NEEDS_MANUAL_
        # REVIEW) does not exclude — same threshold _check_crew_
        # qualifications()'s own missing-expiry case already gets.
        # "Pending" (nobody on the other seat yet) emits no alert at
        # all, by design — see _check_crew_pairing_age()'s own
        # docstring.
        pairing_alerts, _ = _check_crew_pairing_age(
            engine, operating_position, crew_row, flight_ids, domestic, reference_date)
        for alert in pairing_alerts:
            result.add_alert(alert)

        summary = summarize_alerts(result, target_duty_id=candidate_duty_id)
        blocking_reason = build_audit_reason(summary, frozenset({AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW}))
        warning_reason = build_audit_reason(summary, frozenset({AlertStatus.WARNING}))
        statuses.append(CandidateStatus(
            crew_id=crew_row["crew_id"], status=result.status.value,
            blocking_reasons=[blocking_reason] if blocking_reason else [],
            warnings=[warning_reason] if warning_reason else [],
        ))

    return statuses


def remove_assignment_from_duty(crew_id: str, duty_id: str, reason: Optional[str] = None,
                                 app_user: Optional[str] = None) -> None:
    """
    Cancels EVERY active roster row for (crew_id, duty_id) in one
    transaction — replaces remove_assignment() (2026-08-12, flight-deck
    crew package), which operated on a single (crew_id, flight_id,
    role_assigned) row: for a multi-sector duty, that left every OTHER
    sector still active, still carrying the original duty-level
    report_time/debrief_time/fdp_hours — a real, reproduced defect,
    not hypothetical. This predates the pair work — LM/ENGR were just
    as exposed, since assign_crew_to_duty() always wrote a whole
    duty's sectors together but remove_assignment() was never
    duty-scoped to match. The old function is removed, not left
    alongside this one — keeping both would invite exactly the misuse
    that caused the defect. Marks CANCELLED, never deletes, same
    permanent-record pattern as before; the partial unique index
    (migrations/005) still allows the same (crew, flight, role) to be
    assigned again afterward.

    If the cancelled crew_id held a rotation-linked
    (flights.rotation_instance_id IS NOT NULL) COMMANDER/SECOND_PILOT
    seat, this also opens (or reopens) an uncovered_seats row for that
    seat. uncovered_seats is meant to be the single durable source of
    truth for "which seats are currently empty," not just a generator
    failure log — a manually-vacated seat with no durable record here
    would undercut the point of the table (flagged explicitly during
    plan review, decided deliberately here rather than left as a gap).
    """
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT r.flight_id, r.operating_position, f.rotation_instance_id
            FROM roster r JOIN flights f ON r.flight_id = f.flight_id
            WHERE r.crew_id = :crew_id AND r.duty_id = :duty_id AND r.status != 'CANCELLED'
        """), {"crew_id": crew_id, "duty_id": duty_id}).mappings().all()

        if not rows:
            raise ValueError("No active assignment found matching crew_id/duty_id")

        conn.execute(text("""
            UPDATE roster SET status = 'CANCELLED'
            WHERE crew_id = :crew_id AND duty_id = :duty_id AND status != 'CANCELLED'
        """), {"crew_id": crew_id, "duty_id": duty_id})

        operating_position = rows[0]["operating_position"]
        rotation_instance_id = rows[0]["rotation_instance_id"]
        if operating_position is not None and rotation_instance_id is not None:
            conn.execute(text("""
                INSERT INTO uncovered_seats (rotation_instance_id, operating_position, reason)
                VALUES (:rotation_instance_id, :operating_position, :reason)
                ON CONFLICT (rotation_instance_id, operating_position)
                DO UPDATE SET reason = EXCLUDED.reason, generated_at = NOW(), resolved_at = NULL
            """), {
                "rotation_instance_id": rotation_instance_id,
                "operating_position": operating_position,
                "reason": f"Manually unassigned ({crew_id}): {reason or 'no reason given'}",
            })

        log_audit(
            action_type="ASSIGNMENT_REMOVED",
            affected_crew=crew_id,
            affected_flight=rows[0]["flight_id"],
            affected_duty=duty_id,
            reason=reason,
            app_user=app_user,
            conn=conn,
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


def get_roster_for_crew(crew_id: str, include_cancelled: bool = False,
                         include_proposed: bool = False) -> pd.DataFrame:
    """
    include_proposed (2026-08-04, Phase 7's roster generator):
    PROPOSED rows (services/roster_generator_service.py's
    generate_for_window(), not yet published via publish_window()) are
    excluded by default, same shape as include_cancelled — a crew
    member's own duty history should show published assignments only,
    matching the requirements doc's "crew sees only published."
    """
    engine = get_engine()
    query = "SELECT * FROM roster WHERE crew_id = :crew_id"
    if not include_cancelled:
        query += " AND status != 'CANCELLED'"
    if not include_proposed:
        query += " AND status != 'PROPOSED'"
    query += " ORDER BY report_time"
    return pd.read_sql(text(query), engine, params={"crew_id": crew_id})


def get_roster_for_flight(flight_id: int, include_cancelled: bool = False,
                           include_proposed: bool = False) -> pd.DataFrame:
    """See get_roster_for_crew()'s docstring for include_proposed."""
    engine = get_engine()
    query = "SELECT * FROM roster WHERE flight_id = :flight_id"
    if not include_cancelled:
        query += " AND status != 'CANCELLED'"
    if not include_proposed:
        query += " AND status != 'PROPOSED'"
    return pd.read_sql(text(query), engine, params={"flight_id": flight_id})


def search_roster(crew_ids: Optional[List[str]] = None, role: Optional[str] = None,
                   date_from=None, date_to=None, include_cancelled: bool = False,
                   include_proposed: bool = False) -> pd.DataFrame:
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

    include_proposed: see get_roster_for_crew()'s docstring — excluded
    by default, same shape as include_cancelled.
    """
    engine = get_engine()
    query = """
        SELECT r.roster_id, r.crew_id, c.name AS crew_name, r.flight_id,
               r.duty_id, r.duty_date, r.report_time, r.debrief_time,
               -- operating_position added 2026-08-21, additive: no
               -- existing consumer selects columns by position. It is
               -- the SEAT (COMMANDER / SECOND_PILOT), which
               -- role_assigned is NOT — under the pair model a CPT can
               -- legitimately occupy the Second Pilot seat, so deriving
               -- seat coverage from role_assigned reports the wrong
               -- seat filled. That grade-versus-position conflation is
               -- exactly what the flight-deck crew package existed to
               -- remove; Control Room's operational-status board needs
               -- the seat, and needs it for a whole day in ONE query
               -- rather than a get_roster_for_flight() per flight.
               r.operating_position,
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
    if not include_proposed:
        conditions.append("r.status != 'PROPOSED'")
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


def expiry_in_window(crew_row, date_from: Optional[dt.date], date_to: Optional[dt.date]) -> bool:
    """True if ANY of the eight tracked documents expires inside
    [date_from, date_to]. Either bound may be None for open-ended.

    The single implementation of "expiring in this window". Lives here
    because this module owns QUALIFICATION_EXPIRY_FIELDS and the
    qualification gate that acts on it; services/assistant/reports.py
    delegates to it rather than keeping its own copy, so the ops banner
    on the home page and the crew_qualifications report can never come
    to different conclusions about the same crew member.
    """
    for field_name in QUALIFICATION_EXPIRY_FIELDS:
        value = crew_row[field_name]
        if value is None or pd.isna(value):
            continue
        expiry = value.date() if hasattr(value, "date") else value
        if date_from and expiry < date_from:
            continue
        if date_to and expiry > date_to:
            continue
        return True
    return False


def qualification_expiry_counts(as_of: Optional[dt.date] = None, horizon_days: int = 7) -> dict:
    """Counts for the home-page ops banner:
    {"expired": int, "expiring": int, "horizon_days": int}.

    Deliberately returns COUNTS, not a report. reports.crew_qualifications()
    covers the same ground but takes a query_parser.ReportRequest and
    returns an exportable Dataset — the assistant's natural-language
    surface. A landing page should not have to construct an NL request
    object to learn how many documents need attention, and duplicating
    the query would risk the two disagreeing, so this shares the
    predicate above instead.

    "expired" and "expiring" are counted SEPARATELY and are disjoint,
    because the boundary is not the obvious one: the legality gate
    treats expiry <= duty_date as already expired, so a document
    expiring TODAY is invalid today, not "expiring soon". Folding them
    into one number would let a controller read a document that is
    already blocking assignments as one they still have time to renew.

    Counts CREW MEMBERS, not documents — one person with three lapsed
    documents is one row to action, and is what the Crew Data page will
    show them.
    """
    as_of = as_of or dt.date.today()
    crew = crew_service.get_all_crew(active_only=True)
    if crew.empty:
        return {"expired": 0, "expiring": 0, "horizon_days": horizon_days}

    expired = crew.apply(lambda row: expiry_in_window(row, None, as_of), axis=1).sum()
    expiring = crew.apply(
        lambda row: expiry_in_window(
            row, as_of + dt.timedelta(days=1), as_of + dt.timedelta(days=horizon_days)),
        axis=1,
    ).sum()

    return {"expired": int(expired), "expiring": int(expiring), "horizon_days": horizon_days}
