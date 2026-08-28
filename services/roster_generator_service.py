"""
services/roster_generator_service.py

Phase 7 final piece (2026-08-04), rebuilt for the flight-deck crew
package (2026-08-12): fills the Commander + Second Pilot pair for
every APPROVED rotation_instance in a window, atomically, via
services.assignment_service.assign_pair_to_duty() (fresh pairs) or
assign_crew_to_duty() (filling the remaining seat of an already-real
pair) — never a direct INSERT here (Ownership Table unchanged), and
never a re-derived FTL/age-pairing/composition rule; every legality
decision goes through the real gate, every single time. This module
only decides WHICH ORDER to try candidate pairs in
(core/roster_generation.py's order_candidates(), reused unchanged
from before this piece — it never knew about grade/seat, only
crew_id/duty_count/age, so nothing there needed to change) and WHETHER
a seat is already filled.

For a date window: every APPROVED rotation_instance in range gets its
Commander and Second Pilot seats filled, or explicitly, permanently
recorded as UNCOVERED (uncovered_seats table, migrations/017) — never
silently skipped. UNCOVERED is a legitimate, expected outcome, not a
failure. Durable: a page refresh no longer loses it, unlike the
in-memory-only GenerationSummary this module still also returns for
same-session display convenience.
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

SEATS = ("COMMANDER", "SECOND_PILOT")


@dataclass
class SeatResult:
    rotation_instance_id: int
    rotation_code: str
    rotation_date: dt.date
    operating_position: str
    crew_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class GenerationSummary:
    filled: List[SeatResult] = field(default_factory=list)
    uncovered: List[SeatResult] = field(default_factory=list)
    already_covered: List[SeatResult] = field(default_factory=list)


def _seed_duty_counts(crew_ids: List[str], operating_position: str,
                       date_from: dt.date, date_to: dt.date) -> dict:
    """One batch query per seat — real, duty-deduped counts, scoped
    to the generation window itself, never raw search_roster() row
    counts (core/duty_summary.py's own warning). include_proposed is
    True here so a seat this same run already filled earlier in the
    walk counts toward fairness immediately, not just already-PLANNED
    rows from before this run.

    Counted by SEAT (operating_position), not by grade. Corrected
    2026-08-28 — this filtered on role_assigned, which under the pair
    model is the pilot's GRADE and not the seat they sat in. Since
    every row for a CPT reads role_assigned='CPT' whichever seat they
    occupied, and the Commander pool is exactly the CPTs, the filter
    was a no-op in both directions: a CPT's Second Pilot duties counted
    toward their Commander total and vice versa, so this measured total
    workload while its own docstring claimed seat workload.

    Operator decision, not a silent bug fix (see HANDOVER 2026-08-28):
    this ordering chooses who is OFFERED a particular seat, and command
    is the position carrying the responsibility, so the opportunity
    being distributed is seat-specific. Fatigue is not what this
    balances — the FTL gate handles that, and it is unaffected. THIS
    CHANGES GENERATED ROSTERS: a CPT who has flown many Second Pilot
    duties now sorts as under-used for Commander, where before they
    sorted as heavily used.

    NULL operating_position rows (LM/ENGR, and any pre-016 cockpit row)
    belong to no seat and are counted toward neither — which is what
    "duties in this seat" means, and is why this filters rather than
    falling back to grade."""
    counts = {cid: 0 for cid in crew_ids}
    if not crew_ids:
        return counts
    rows = assignment_service.search_roster(
        crew_ids=crew_ids, date_from=date_from, date_to=date_to, include_proposed=True,
    )
    if not rows.empty:
        rows = rows[rows["operating_position"] == operating_position]
    duties = group_roster_rows_into_duties(rows)
    if not duties.empty:
        for crew_id, n in duties.groupby("crew_id").size().items():
            counts[crew_id] = int(n)
    return counts


def _seat_occupant(flight_ids: List[int], operating_position: str) -> Optional[str]:
    """The crew_id already holding this seat (any ACTIVE status,
    including a prior run's own PROPOSED), or None. Only the first
    flight_id needs checking — a filled seat's rows all share the same
    crew_id across every sector of the duty."""
    if not flight_ids:
        return None
    roster = assignment_service.get_roster_for_flight(flight_ids[0], include_proposed=True)
    matches = roster.loc[roster["operating_position"] == operating_position, "crew_id"]
    return matches.iloc[0] if not matches.empty else None


def _age_of(crew_by_id: dict, crew_id: str, reference_date: dt.date) -> Optional[int]:
    """Age from the ALREADY-LOADED crew snapshot — no query.

    This called crew_service.get_crew() until 2026-08-22: a database
    round-trip to read a birthday, once per candidate per seat. In the
    pair search the second-pilot list is rebuilt inside the commander
    loop, so the cost was C + C x (1 + S) round-trips per rotation
    before a single legality check ran — 72 of them for Air Eagle's real
    pool of 6 commanders and 10 second pilots.

    Invisible locally, where a round-trip costs microseconds. Against
    Supabase from Streamlit Cloud, at 50-300ms each, it is minutes.
    """
    crew_row = crew_by_id.get(crew_id)
    if crew_row is None or pd.isna(crew_row["date_of_birth"]):
        return None
    return assignment_service.age_on(crew_row["date_of_birth"], reference_date)


def _reject_reason(crew_id: str, result) -> str:
    reason_text = None
    if result.alert_summary is not None:
        reason_text = build_audit_reason(
            result.alert_summary, frozenset({AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW}))
    return f"{crew_id} ({result.status}): {reason_text or 'no detail'}"


def _pair_reject_reason(commander_id: str, second_pilot_id: str, pair_result) -> str:
    parts = []
    commander_reason = build_audit_reason(
        pair_result.validation.commander_alert_summary, frozenset({AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW})
    ) if pair_result.validation.commander_alert_summary is not None else None
    second_pilot_reason = build_audit_reason(
        pair_result.validation.second_pilot_alert_summary, frozenset({AlertStatus.ILLEGAL, AlertStatus.NEEDS_MANUAL_REVIEW})
    ) if pair_result.validation.second_pilot_alert_summary is not None else None
    if commander_reason:
        parts.append(f"commander: {commander_reason}")
    if second_pilot_reason:
        parts.append(f"second pilot: {second_pilot_reason}")
    if pair_result.validation.pair_alerts:
        parts.append("; ".join(a.message for a in pair_result.validation.pair_alerts))
    detail = "; ".join(parts) if parts else "no detail"
    return f"{commander_id}+{second_pilot_id} ({pair_result.status}): {detail}"


def _record_uncovered(instance_id: int, rotation_code: str, reference_date: dt.date,
                       operating_position: str, reason: str, app_user: Optional[str]) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO uncovered_seats (rotation_instance_id, operating_position, reason)
            VALUES (:rotation_instance_id, :operating_position, :reason)
            ON CONFLICT (rotation_instance_id, operating_position)
            DO UPDATE SET reason = EXCLUDED.reason, generated_at = NOW(), resolved_at = NULL
        """), {"rotation_instance_id": instance_id, "operating_position": operating_position, "reason": reason})
    log_audit(
        action_type="ROSTER_GENERATION_SEAT_UNCOVERED",
        reason=f"{rotation_code} {reference_date} {operating_position}",
        warning_or_failure_reason=reason,
        app_user=app_user,
    )


def get_open_uncovered_seats(date_from: dt.date, date_to: dt.date) -> pd.DataFrame:
    """The durable read side of uncovered_seats — every currently-open
    (resolved_at IS NULL) row for a rotation in [date_from, date_to],
    joined against rotation_instances for rotation_code/rotation_date.
    Survives a page refresh, unlike GenerationSummary.uncovered: this
    is what a controller sees on returning to this page later, whether
    the gap came from generate_for_window() never finding a legal pair
    or from services.assignment_service.remove_assignment_from_duty()
    manually vacating a rotation-linked seat — both writers, one read
    path, since uncovered_seats' whole point is being the single
    durable source of truth for "which seats are currently empty."
    """
    engine = get_engine()
    return pd.read_sql(text("""
        SELECT u.rotation_instance_id, ri.rotation_code, ri.rotation_date,
               u.operating_position, u.reason, u.generated_at
        FROM uncovered_seats u
        JOIN rotation_instances ri ON ri.id = u.rotation_instance_id
        WHERE u.resolved_at IS NULL
          AND ri.rotation_date >= :date_from AND ri.rotation_date <= :date_to
        ORDER BY ri.rotation_date, ri.rotation_code, u.operating_position
    """), engine, params={"date_from": date_from, "date_to": date_to})


def generate_for_window(date_from: dt.date, date_to: dt.date,
                         app_user: Optional[str] = None) -> GenerationSummary:
    """
    Fills the Commander + Second Pilot pair for every APPROVED
    rotation_instance whose rotation_date falls in [date_from,
    date_to] — ad-hoc Control Room flights (rotation_instance_id IS
    NULL) are out of scope, those are already assigned interactively.

    Writes PROPOSED roster rows via assign_pair_to_duty() (fresh
    pairs) or assign_crew_to_duty() (filling the remaining seat of an
    already-real pair) — nothing is visible to crew until
    publish_window() flips them to PLANNED.

    Idempotent by construction: a seat with an existing ACTIVE
    assignment (any status, including PROPOSED from an earlier run) is
    skipped, never re-attempted or replaced. Not wrapped in one
    transaction across the whole window — each assign_pair_to_duty()/
    assign_crew_to_duty() call is already its own atomic, committed
    unit; a crash mid-run simply stops, safely resumable by re-running
    with no special recovery step.

    Rotations are walked chronologically, same-day ties broken by
    rotation_code (deterministic — matters for idempotency).

    Three cases per rotation, per seat state:
      - Both seats already filled: nothing to do, both recorded
        already_covered.
      - ONE seat already filled: the remaining seat is filled against
        the REAL occupant of the other seat (assign_crew_to_duty(),
        which validates the pairing against a real, committed partner
        — see its own docstring for why this is a legitimate single-
        seat commit, not the "solo pilot before pair known" defect).
      - NEITHER seat filled: a fresh pair search — Commander candidates
        ordered first (no partner_age yet), and for each Commander
        candidate tried, Second Pilot candidates ordered using THAT
        candidate's real age as partner_age, then the pair attempted
        atomically via assign_pair_to_duty() — both seats validated
        and committed together, or neither, closing the defect where a
        solo pilot could previously commit before any partner was
        known. The search tries every Commander x Second Pilot
        combination in fairness/age order until one is ALLOWED or all
        are exhausted (Air Eagle's real crew pool — 6 CPT, 4 FO — is
        small enough that this is not a real performance concern, same
        reasoning LOOKBACK_DAYS's own comment already applies
        elsewhere in this codebase).

    A seat that can't be filled is recorded UNCOVERED with the real
    rejection reason from the actual legality gate — durably, via
    uncovered_seats (migrations/017), not just in this function's
    returned in-memory GenerationSummary.
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

    # ONE snapshot of every active crew row, reused for the whole run:
    # ages, the qualification pre-filter and the validation gate all read
    # from this instead of re-querying per candidate.
    #
    # A SHARED snapshot is required for CORRECTNESS here, not merely
    # tolerated for speed: the pre-filter and the gate must judge a
    # candidate on the same data, and they would not if the filter read
    # this while the gate re-fetched per trial.
    #
    # Its lifetime is this one function call. Bounded staleness is safe
    # because generation writes PROPOSED rows and publish_window()
    # re-validates every pair against fresh data before anything becomes
    # PLANNED — so a crew edit landing mid-run cannot reach a published
    # roster.
    crew_by_id = {row["crew_id"]: row for _, row in all_crew.iterrows()}

    seat_grades = assignment_service.SEAT_ELIGIBLE_GRADES
    pools = {
        position: all_crew[all_crew["role"].isin(seat_grades[position])]
        for position in SEATS
    }
    # Seeded per SEAT, not per grade — the pool is still selected by
    # grade (who MAY sit there), but the fairness count is of duties
    # actually flown in that seat. See _seed_duty_counts().
    duty_counts = {
        position: _seed_duty_counts(pools[position]["crew_id"].tolist(), position, date_from, date_to)
        for position in SEATS
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

        # The flights and crew rows every candidate trial would
        # otherwise re-fetch. Built once per rotation; see
        # assignment_service.Prefetch for why passing rows in is safe
        # here and why it is opt-in everywhere else.
        prefetch = assignment_service.Prefetch(
            crew_by_id=crew_by_id, flights_by_id=dict(zip(flight_ids, flights)))

        commander_id = _seat_occupant(flight_ids, "COMMANDER")
        second_pilot_id = _seat_occupant(flight_ids, "SECOND_PILOT")

        if commander_id is not None:
            summary.already_covered.append(SeatResult(
                rotation_instance_id=instance_id, rotation_code=rotation_code,
                rotation_date=reference_date, operating_position="COMMANDER", crew_id=commander_id,
            ))
        if second_pilot_id is not None:
            summary.already_covered.append(SeatResult(
                rotation_instance_id=instance_id, rotation_code=rotation_code,
                rotation_date=reference_date, operating_position="SECOND_PILOT", crew_id=second_pilot_id,
            ))

        if commander_id is not None and second_pilot_id is not None:
            continue

        if commander_id is not None or second_pilot_id is not None:
            # Exactly one seat already real -- fill the remaining one
            # against it.
            if second_pilot_id is None:
                fill_position, other_position, other_crew_id = "SECOND_PILOT", "COMMANDER", commander_id
                pool = pools["SECOND_PILOT"]
            else:
                fill_position, other_position, other_crew_id = "COMMANDER", "SECOND_PILOT", second_pilot_id
                pool = pools["COMMANDER"]

            partner_age = _age_of(crew_by_id, other_crew_id, reference_date)
            candidates = [
                Candidate(crew_id=row["crew_id"], duty_count=duty_counts[fill_position].get(row["crew_id"], 0),
                          age=_age_of(crew_by_id, row["crew_id"], reference_date))
                for _, row in pool.iterrows() if row["crew_id"] != other_crew_id
            ]
            ordered = order_candidates(candidates, domestic=domestic, partner_age=partner_age)

            filled_id, reasons = None, []
            for crew_id in ordered:
                crew_row = crew_by_id[crew_id]
                # audit_trials=False — same reasoning as the pair search
                # below; this is the one-seat-already-real variant of the
                # same speculative loop.
                result = assignment_service.assign_crew_to_duty(
                    crew_id, flight_ids, crew_row["role"], app_user=app_user,
                    roster_status="PROPOSED", operating_position=fill_position,
                    prefetch=prefetch, audit_trials=False)
                if result.status == "ALLOWED":
                    filled_id = crew_id
                    break
                reasons.append(_reject_reason(crew_id, result))

            if filled_id is not None:
                duty_counts[fill_position][filled_id] = duty_counts[fill_position].get(filled_id, 0) + 1
                summary.filled.append(SeatResult(
                    rotation_instance_id=instance_id, rotation_code=rotation_code,
                    rotation_date=reference_date, operating_position=fill_position, crew_id=filled_id,
                ))
            else:
                reason = "; ".join(reasons) if reasons else "No candidates in pool"
                _record_uncovered(instance_id, rotation_code, reference_date, fill_position, reason, app_user)
                summary.uncovered.append(SeatResult(
                    rotation_instance_id=instance_id, rotation_code=rotation_code,
                    rotation_date=reference_date, operating_position=fill_position, reason=reason,
                ))
            continue

        # Neither seat filled -- fresh pair search.
        commander_candidates = [
            Candidate(crew_id=row["crew_id"], duty_count=duty_counts["COMMANDER"].get(row["crew_id"], 0),
                      age=_age_of(crew_by_id, row["crew_id"], reference_date))
            for _, row in pools["COMMANDER"].iterrows()
        ]
        ordered_commanders = order_candidates(commander_candidates, domestic=domestic, partner_age=None)

        # Built ONCE per rotation, not once per commander. The list's
        # CONTENTS never vary with the commander — only the ordering
        # (partner_age) and which single candidate is excluded — so
        # rebuilding it inside the loop bought nothing and cost S
        # round-trips per commander while _age_of() still queried.
        second_pilot_candidates = [
            Candidate(crew_id=row["crew_id"], duty_count=duty_counts["SECOND_PILOT"].get(row["crew_id"], 0),
                      age=_age_of(crew_by_id, row["crew_id"], reference_date))
            for _, row in pools["SECOND_PILOT"].iterrows()
        ]
        commander_ages = {
            candidate.crew_id: candidate.age for candidate in commander_candidates
        }

        filled_pair = None
        all_reasons = []
        for commander_candidate_id in ordered_commanders:
            # Already computed when the commander list was built.
            commander_age = commander_ages.get(commander_candidate_id)
            # Self-exclusion in memory: a CPT eligible for both seats
            # must not be paired with themselves.
            ordered_second_pilots = order_candidates(
                [c for c in second_pilot_candidates if c.crew_id != commander_candidate_id],
                domestic=domestic, partner_age=commander_age)

            for second_pilot_candidate_id in ordered_second_pilots:
                # audit_trials=False: an option the search considered and
                # discarded is not a decision, and the audit trail records
                # decisions. This loop runs C x S times per uncovered
                # rotation and previously wrote one row per rejection —
                # 2,954 of them into production in a morning, 94% of the
                # whole trail (operator decision, 2026-08-26). What a
                # regulator actually asks — why this crew was legal, why
                # that flight went uncovered — is still answerable:
                # ASSIGNMENT_CREATED is unconditional and
                # _record_uncovered() below is untouched.
                pair_result = assignment_service.assign_pair_to_duty(
                    commander_candidate_id, second_pilot_candidate_id, flight_ids,
                    app_user=app_user, roster_status="PROPOSED", prefetch=prefetch,
                    audit_trials=False)
                if pair_result.status == "ALLOWED":
                    filled_pair = (commander_candidate_id, second_pilot_candidate_id)
                    break
                all_reasons.append(
                    _pair_reject_reason(commander_candidate_id, second_pilot_candidate_id, pair_result))
            if filled_pair is not None:
                break

        if filled_pair is not None:
            commander_id, second_pilot_id = filled_pair
            duty_counts["COMMANDER"][commander_id] = duty_counts["COMMANDER"].get(commander_id, 0) + 1
            duty_counts["SECOND_PILOT"][second_pilot_id] = duty_counts["SECOND_PILOT"].get(second_pilot_id, 0) + 1
            summary.filled.append(SeatResult(
                rotation_instance_id=instance_id, rotation_code=rotation_code,
                rotation_date=reference_date, operating_position="COMMANDER", crew_id=commander_id,
            ))
            summary.filled.append(SeatResult(
                rotation_instance_id=instance_id, rotation_code=rotation_code,
                rotation_date=reference_date, operating_position="SECOND_PILOT", crew_id=second_pilot_id,
            ))
        else:
            reason = "; ".join(all_reasons) if all_reasons else "No candidates in pool"
            for position in SEATS:
                _record_uncovered(instance_id, rotation_code, reference_date, position, reason, app_user)
                summary.uncovered.append(SeatResult(
                    rotation_instance_id=instance_id, rotation_code=rotation_code,
                    rotation_date=reference_date, operating_position=position, reason=reason,
                ))

    return summary


def publish_window(date_from: dt.date, date_to: dt.date, app_user: Optional[str] = None) -> int:
    """
    The mechanical PROPOSED -> PLANNED flip, gated per-rotation
    (2026-08-12, flight-deck crew package): a rotation only publishes
    if BOTH seats are currently filled (active COMMANDER and
    SECOND_PILOT rows exist) AND the actual assigned pair still passes
    re-validation right now — crew data or other duties may have
    changed since the PROPOSED rows were written, so this is a fresh
    check, not a trust of the original assignment-time result. A
    rotation failing either check is skipped entirely (left as
    PROPOSED); other fully-valid rotations in the same window still
    publish. Each rotation's own flip is atomic (both pilots, all
    sectors, together or not at all) — same "no orphan" atomicity
    philosophy already used elsewhere in this codebase, applied at the
    rotation level here.

    Scoped by roster.duty_date (the duty's own calendar date), matching
    the window framing generate_for_window() itself uses. Returns the
    total number of roster rows flipped (a raw row/sector count, same
    unit as before this piece — publish_window()'s return value has
    always meant "rows," not "duties").
    """
    engine = get_engine()

    with engine.connect() as conn:
        proposed_rotations = conn.execute(text("""
            SELECT DISTINCT f.rotation_instance_id
            FROM roster r JOIN flights f ON r.flight_id = f.flight_id
            WHERE r.status = 'PROPOSED' AND r.duty_date >= :date_from AND r.duty_date <= :date_to
              AND f.rotation_instance_id IS NOT NULL
        """), {"date_from": date_from, "date_to": date_to}).scalars().all()

    published_count = 0
    for rotation_instance_id in proposed_rotations:
        flight_ids = rotation_template_service.get_promoted_flight_ids(rotation_instance_id)
        if not flight_ids:
            continue

        commander_id = _seat_occupant(flight_ids, "COMMANDER")
        second_pilot_id = _seat_occupant(flight_ids, "SECOND_PILOT")
        if commander_id is None or second_pilot_id is None:
            continue

        try:
            pair_result = assignment_service.validate_pair(commander_id, second_pilot_id, flight_ids)
        except ValueError:
            # A data-integrity surprise (e.g. a crew record deactivated
            # between PROPOSED and now in a way validate_pair() itself
            # rejects) skips this one rotation, same as an ordinary
            # ILLEGAL/NEEDS_MANUAL_REVIEW re-validation result -- it
            # must not take down the rest of the window's publish.
            continue
        if pair_result.status in (AlertStatus.ILLEGAL.value, AlertStatus.NEEDS_MANUAL_REVIEW.value):
            continue

        with engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE roster SET status = 'PLANNED'
                WHERE status = 'PROPOSED' AND duty_date >= :date_from AND duty_date <= :date_to
                  AND flight_id = ANY(:flight_ids)
            """), {"date_from": date_from, "date_to": date_to, "flight_ids": flight_ids})
            published_count += result.rowcount

    if published_count:
        log_audit(
            action_type="ROSTER_WINDOW_PUBLISHED",
            reason=f"{date_from} to {date_to}",
            changed_state=f"{published_count} roster row(s) published",
            app_user=app_user,
        )

    return published_count
