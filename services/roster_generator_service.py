"""
services/roster_generator_service.py

Phase 7 final piece (2026-08-04): the roster generator. Orchestration
only — writes exclusively to `roster`, via the existing
assignment_service.assign_crew_to_duty(), never a direct INSERT here
(Ownership Table unchanged). This module never re-expresses an FTL or
age-pairing rule: every legality decision goes through the real gate,
every single time. It only decides WHICH ORDER to try candidates in
(core/roster_generation.py's order_candidates()) and WHETHER a seat is
already filled — see the approved plan (HANDOVER.md's 2026-08-04
roster-generator entry) for the full Q1-Q7 design this implements.

For a date window: every APPROVED rotation_instance in range gets its
CPT and FO seats filled or explicitly, permanently recorded as
UNCOVERED — never silently skipped. UNCOVERED is a legitimate, expected
outcome, not a failure.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd
from sqlalchemy import text

from db.db import get_engine
from services import assignment_service, crew_service, flight_service, rotation_template_service
from services.audit_service import log_audit
from services.alert_summary import build_audit_reason
from core.legality.pcaa_ano012_core import AlertStatus
from core.duty_summary import group_roster_rows_into_duties
from core.roster_generation import Candidate, order_candidates

# FO filled before CPT — deliberate, verified, not arbitrary
# (2026-08-04). International's age-fix is unconditional on BOTH seats
# (order_candidates() applies it even with partner_age=None), so fill
# order never matters there. Domestic's fix is conditional on the
# SECOND seat only — which means fill order decides whether the fix
# can ever engage at all. Confirmed by direct simulation against the
# real pairing math (age_on/_evaluate_pair_age logic): given a
# domestic rotation with an entirely-65+ FO pool (66, 68) and a mixed
# CPT pool whose fewest-duty candidate happens to be 65+ (67) but a
# legal 40yo alternative exists — filling CPT first locks onto the
# 67yo (no partner yet, so nothing blocks it) before the conditional
# fix ever gets a chance to act, dooming the FO seat (an entirely-65+
# pool can't be rescued by reordering it, whatever order its own
# candidates are tried in). Filling FO first instead means FO's
# (unavoidably 65+) pick becomes the KNOWN partner_age feeding CPT's
# own conditional ordering, which then correctly prefers the 40yo over
# the 67yo — full coverage. FO is also the real-world smaller pool (4
# vs 6 CPT), making it the more likely one to end up homogeneously
# 65+ in the first place, so filling it first and letting the larger,
# more likely-mixed CPT pool absorb the conditional protection second
# is the operationally sound choice, not just the one that happens to
# pass this scenario.
ROLES = ("FO", "CPT")


@dataclass
class SeatResult:
    rotation_instance_id: int
    rotation_code: str
    rotation_date: dt.date
    role: str
    crew_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class GenerationSummary:
    filled: List[SeatResult] = field(default_factory=list)
    uncovered: List[SeatResult] = field(default_factory=list)
    already_covered: List[SeatResult] = field(default_factory=list)


def _seed_duty_counts(crew_ids: List[str], role: str, date_from: dt.date, date_to: dt.date) -> dict:
    """One batch query per role — real, duty-deduped counts (Q2),
    scoped to the generation window itself, never raw search_roster()
    row counts (core/duty_summary.py's own warning). include_proposed
    is True here so a seat this same run already filled earlier in the
    walk counts toward fairness immediately, not just already-PLANNED
    rows from before this run."""
    counts = {cid: 0 for cid in crew_ids}
    if not crew_ids:
        return counts
    rows = assignment_service.search_roster(
        crew_ids=crew_ids, role=role, date_from=date_from, date_to=date_to,
        include_proposed=True,
    )
    duties = group_roster_rows_into_duties(rows)
    if not duties.empty:
        for crew_id, n in duties.groupby("crew_id").size().items():
            counts[crew_id] = int(n)
    return counts


def _seat_already_filled(flight_ids: List[int], role: str) -> Optional[str]:
    """The crew_id already holding this seat (any ACTIVE status,
    including a prior run's own PROPOSED), or None. Only the first
    flight_id needs checking — assign_crew_to_duty() writes one roster
    row per flight_id for the same crew_id/duty, so the first leg
    already tells us whether this seat is taken."""
    if not flight_ids:
        return None
    roster = assignment_service.get_roster_for_flight(flight_ids[0], include_proposed=True)
    matches = roster.loc[roster["role_assigned"] == role, "crew_id"]
    return matches.iloc[0] if not matches.empty else None


def _age_of(crew_id: str, reference_date: dt.date) -> Optional[int]:
    crew_row = crew_service.get_crew(crew_id)
    if crew_row is None or pd.isna(crew_row["date_of_birth"]):
        return None
    return assignment_service.age_on(crew_row["date_of_birth"], reference_date)


def generate_for_window(date_from: dt.date, date_to: dt.date,
                         app_user: Optional[str] = None) -> GenerationSummary:
    """
    Fills CPT/FO seats for every APPROVED rotation_instance whose
    rotation_date falls in [date_from, date_to] (Q1) — ad-hoc Control
    Room flights (rotation_instance_id IS NULL) are out of scope, those
    are already assigned interactively.

    Writes PROPOSED roster rows (Q5) via assign_crew_to_duty(); nothing
    is visible to crew until publish_window() flips them to PLANNED.

    Idempotent by construction (Q7): a seat with an existing ACTIVE
    assignment (any status, including PROPOSED from an earlier run) is
    skipped, never re-attempted or replaced. Not wrapped in one
    transaction (Q6) — each assign_crew_to_duty() call is already its
    own atomic, committed unit; a crash mid-run simply stops, safely
    resumable by re-running with no special recovery step.

    Rotations are walked chronologically, same-day ties broken by
    rotation_code (Q3, deterministic — matters for idempotency).
    """
    instances = rotation_template_service.get_instances(status="APPROVED")
    if not instances.empty:
        instances = instances[
            (instances["rotation_date"] >= date_from) & (instances["rotation_date"] <= date_to)
        ].sort_values(["rotation_date", "rotation_code"])

    summary = GenerationSummary()
    if instances.empty:
        return summary

    all_crew = crew_service.get_all_crew(active_only=True)
    pools = {role: all_crew[all_crew["role"] == role] for role in ROLES}
    duty_counts = {
        role: _seed_duty_counts(pools[role]["crew_id"].tolist(), role, date_from, date_to)
        for role in ROLES
    }

    for _, instance in instances.iterrows():
        instance_id = int(instance["id"])
        rotation_code = instance["rotation_code"]
        reference_date = instance["rotation_date"]

        flight_ids = rotation_template_service.get_promoted_flight_ids(instance_id)
        if not flight_ids:
            # approve_instance() promotes every leg atomically, so an
            # APPROVED instance with zero promoted flights shouldn't
            # happen — skip defensively rather than fail the whole
            # window's run over one inconsistent instance.
            continue

        flights = [flight_service.get_flight(fid) for fid in flight_ids]
        domestic = all(bool(f["domestic"]) for f in flights)

        # Two passes, not one loop over ROLES in a fixed order: an
        # already-filled seat's age must feed the OTHER seat's
        # ordering regardless of which role happens to come first in
        # ROLES — e.g. a manually-PLANNED 67yo CPT assigned before
        # this run must still condition FO's ordering, even though
        # ROLES processes FO before CPT for the both-empty case.
        # Checking every seat's existing state up front (pass 1)
        # before deciding any new fill (pass 2) is what makes that
        # information available when it's needed, not just when it
        # happens to already exist.
        seat_ages: dict = {}  # role -> age of whoever ends up filling that seat
        still_needed = []
        for role in ROLES:
            existing_crew_id = _seat_already_filled(flight_ids, role)
            if existing_crew_id is not None:
                summary.already_covered.append(SeatResult(
                    rotation_instance_id=instance_id, rotation_code=rotation_code,
                    rotation_date=reference_date, role=role, crew_id=existing_crew_id,
                ))
                seat_ages[role] = _age_of(existing_crew_id, reference_date)
            else:
                still_needed.append(role)

        for role in still_needed:
            other_role = "FO" if role == "CPT" else "CPT"
            partner_age = seat_ages.get(other_role)

            candidates = [
                Candidate(
                    crew_id=crew_row["crew_id"],
                    duty_count=duty_counts[role].get(crew_row["crew_id"], 0),
                    age=_age_of(crew_row["crew_id"], reference_date),
                )
                for _, crew_row in pools[role].iterrows()
            ]
            ordered_crew_ids = order_candidates(candidates, domestic=domestic, partner_age=partner_age)

            filled_crew_id = None
            rejection_reasons = []
            for crew_id in ordered_crew_ids:
                result = assignment_service.assign_crew_to_duty(
                    crew_id, flight_ids, role, app_user=app_user, roster_status="PROPOSED")
                if result.status == "ALLOWED":
                    filled_crew_id = crew_id
                    break
                reason_text = None
                if result.alert_summary is not None:
                    reason_text = build_audit_reason(
                        result.alert_summary,
                        frozenset({AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW}),
                    )
                rejection_reasons.append(f"{crew_id} ({result.status}): {reason_text or 'no detail'}")

            if filled_crew_id is not None:
                duty_counts[role][filled_crew_id] = duty_counts[role].get(filled_crew_id, 0) + 1
                seat_ages[role] = _age_of(filled_crew_id, reference_date)
                summary.filled.append(SeatResult(
                    rotation_instance_id=instance_id, rotation_code=rotation_code,
                    rotation_date=reference_date, role=role, crew_id=filled_crew_id,
                ))
            else:
                reason = "; ".join(rejection_reasons) if rejection_reasons else "No candidates in pool"
                log_audit(
                    action_type="ROSTER_GENERATION_SEAT_UNCOVERED",
                    reason=f"{rotation_code} {reference_date} {role}",
                    warning_or_failure_reason=reason,
                    app_user=app_user,
                )
                summary.uncovered.append(SeatResult(
                    rotation_instance_id=instance_id, rotation_code=rotation_code,
                    rotation_date=reference_date, role=role, reason=reason,
                ))

    return summary


def publish_window(date_from: dt.date, date_to: dt.date, app_user: Optional[str] = None) -> int:
    """
    The mechanical PROPOSED -> PLANNED flip (Q5) — the roster-level
    analog of rotation_template_service.approve_instance(). Reviewing
    WHICH proposals to publish is a UI concern, out of scope here (no
    Phase 7 piece has UI yet); this is only the flip itself, needed for
    a PROPOSED row to ever become real and for this piece's own tests
    to verify the full round-trip.

    Scoped by roster.duty_date (the duty's own calendar date), matching
    the window framing generate_for_window() itself uses. Returns the
    number of rows flipped.
    """
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE roster SET status = 'PLANNED'
            WHERE status = 'PROPOSED' AND duty_date >= :date_from AND duty_date <= :date_to
        """), {"date_from": date_from, "date_to": date_to})
        published_count = result.rowcount

    if published_count:
        log_audit(
            action_type="ROSTER_WINDOW_PUBLISHED",
            reason=f"{date_from} to {date_to}",
            changed_state=f"{published_count} roster row(s) published",
            app_user=app_user,
        )

    return published_count
