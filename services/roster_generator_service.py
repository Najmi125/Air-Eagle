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

# What accept_preview() writes into roster.status. PROPOSED, not
# PLANNED, DELIBERATELY: the preview adds a review stage BEFORE the
# first write, it does not remove the one that already exists after it.
# publish_window() and its fresh re-validation are untouched, and a
# roster still becomes visible to crew only by being published.
ACCEPTED_ROSTER_STATUS = "PROPOSED"


@dataclass
class PreviewSeat:
    """One seat's provisional occupant. `already_real` marks a seat that
    was already committed in the database before this preview ran — it
    is shown, and it constrains the other seat, but accept does not
    rewrite it."""
    operating_position: str
    crew_id: Optional[str] = None
    role_assigned: Optional[str] = None
    duty_id: Optional[str] = None
    report_time: Optional[object] = None
    debrief_time: Optional[object] = None
    already_real: bool = False
    reason: Optional[str] = None      # set when the seat could not be filled


# PreviewRotation.outcome — every rotation carries one, so the same
# object is both the pre-accept proposal and the post-accept report.
OUTCOME_PROPOSED = "PROPOSED"      # not accepted yet
OUTCOME_WRITTEN = "WRITTEN"        # accept committed it
OUTCOME_REJECTED = "REJECTED"      # accept re-validated it and refused
OUTCOME_UNCOVERED = "UNCOVERED"    # no legal pair found during the preview
OUTCOME_ALREADY_COVERED = "ALREADY_COVERED"  # both seats already real


@dataclass
class PreviewRotation:
    rotation_instance_id: int
    rotation_code: str
    rotation_date: dt.date
    flight_ids: List[int] = field(default_factory=list)
    seats: dict = field(default_factory=dict)   # position -> PreviewSeat
    outcome: str = OUTCOME_PROPOSED
    outcome_reason: Optional[str] = None

    @property
    def is_writable(self) -> bool:
        """A rotation accept would actually write: at least one seat
        provisionally filled by this preview."""
        return any(s.crew_id is not None and not s.already_real
                   for s in self.seats.values())


@dataclass
class GenerationPreview:
    """What generate_preview() decided, having written NOTHING.

    Held in st.session_state between the Generate click and the Accept
    click. Unlike the GenerationSummary it replaces, losing this on a
    refresh loses real work rather than just a display — which is the
    honest cost of not writing speculatively, and is why the page says
    so rather than quietly persisting it.
    """
    date_from: dt.date
    date_to: dt.date
    rotations: List[PreviewRotation] = field(default_factory=list)
    generated_at: Optional[dt.datetime] = None
    accepted_at: Optional[dt.datetime] = None

    @property
    def is_accepted(self) -> bool:
        return self.accepted_at is not None

    def by_outcome(self, outcome: str) -> List[PreviewRotation]:
        return [r for r in self.rotations if r.outcome == outcome]

    def seat_count(self, outcome: str) -> int:
        return sum(
            sum(1 for s in r.seats.values() if s.crew_id is not None and not s.already_real)
            for r in self.by_outcome(outcome)
        )


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


def _duty_flight_rows(flight_ids: List[int], flights: List) -> List[dict]:
    """The per-sector facts a provisional duty row needs, in the shape
    assignment_service.ProvisionalDuties.add_duty() expects.

    Actual times win over planned — the same "ground truth wins" rule
    the duty-history query itself applies — so a provisional duty and a
    committed one are built from the same numbers. A provisional row
    that disagreed with the row it will become is worse than no
    provisional row at all: the preview would enforce one set of rest
    gaps and accept would enforce another.
    """
    rows = []
    for fid, flight in zip(flight_ids, flights):
        rows.append({
            "flight_id": fid,
            "dep_time": (flight["dep_time_actual"] if pd.notna(flight["dep_time_actual"])
                         else flight["dep_time_planned"]),
            "arr_time": (flight["arr_time_actual"] if pd.notna(flight["arr_time_actual"])
                         else flight["arr_time_planned"]),
            "origin": flight["origin"], "destination": flight["destination"],
            "meal_provided": bool(flight["meal_provided"]),
            "snack_provided": bool(flight["snack_provided"]),
        })
    return rows


def generate_preview(date_from: dt.date, date_to: dt.date,
                     app_user: Optional[str] = None) -> GenerationPreview:
    """
    Decides the Commander + Second Pilot pair for every APPROVED
    rotation_instance whose rotation_date falls in [date_from,
    date_to], and WRITES NOTHING — no roster rows, no uncovered_seats
    rows, no audit rows. The result is a GenerationPreview a controller
    reviews before accept_preview() commits any of it.

    WHAT REPLACED WHAT, AND WHY IT MATTERS. This used to write PROPOSED
    roster rows as it walked, which meant cross-rotation legality was
    enforced as a SIDE EFFECT: by the time rotation 2 was validated,
    rotation 1's rows were already in the roster table, so the FTL gate
    picked them up through _fetch_duty_rows() and rest and cumulative
    limits were checked across the window. Removing the writes removes
    that, silently — every rotation would validate against an empty
    history, each would pass on its own, and the SET would be illegal.
    Nothing raises. The seats just fill.

    assignment_service.ProvisionalDuties is what replaces it: each pair
    this run accepts is recorded in memory in the duty-row shape, and
    unioned into the gate's own history read. So the same rules see the
    same duties they saw before; only the storage changed.
    tests/test_cross_rotation_legality.py disables that union and holds
    the whole thing in place — it is the test this change exists for.

    ONE Prefetch FOR THE WHOLE RUN, not one per rotation, and that is
    now safe for the reason it previously was not: the run issues no
    writes, so a database answer read at rotation 1 cannot have been
    invalidated by rotation 35. Provisional duties are unioned on AFTER
    that cache rather than into it (see _fetch_duty_rows), so recording
    one costs no round-trip and invalidates nothing.

    Ad-hoc Control Room flights (rotation_instance_id IS NULL) are out
    of scope, unchanged — those are assigned interactively.

    Idempotent, unchanged: a seat with an existing ACTIVE assignment
    (any status, including PROPOSED from an accepted earlier run) is
    reported already-covered and never re-attempted or replaced.

    Rotations are walked chronologically, same-day ties broken by
    rotation_code. That order is no longer merely deterministic-for-
    idempotency: it is the order the provisional duties accumulate in,
    so it is the order the rest rules are applied in.
    """
    instances = rotation_template_service.get_instances(status="APPROVED")
    if not instances.empty:
        instances = instances[
            (instances["rotation_date"] >= date_from) & (instances["rotation_date"] <= date_to)
        ].sort_values(["rotation_date", "rotation_code"])

    preview = GenerationPreview(date_from=date_from, date_to=date_to,
                                generated_at=dt.datetime.now(dt.timezone.utc))
    if instances.empty:
        return preview

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
    # Its lifetime is this one function call, and this call now writes
    # nothing at all — so bounded staleness is safer than it was under
    # the PROPOSED-rows design: accept_preview() re-validates every
    # rotation against fresh data before committing it, and a crew edit
    # landing mid-preview cannot reach the roster table by any path.
    crew_by_id = {row["crew_id"]: row for _, row in all_crew.iterrows()}

    provisional = assignment_service.ProvisionalDuties()
    prefetch = assignment_service.Prefetch(crew_by_id=crew_by_id, provisional=provisional)

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
        flight_rows = _duty_flight_rows(flight_ids, flights)
        # Accumulated on the run-wide Prefetch rather than a fresh one
        # per rotation — see this function's docstring for why a
        # run-wide cache is now correct.
        prefetch.flights_by_id.update(dict(zip(flight_ids, flights)))

        rotation = PreviewRotation(
            rotation_instance_id=instance_id, rotation_code=rotation_code,
            rotation_date=reference_date, flight_ids=list(flight_ids),
            seats={position: PreviewSeat(operating_position=position) for position in SEATS},
        )
        preview.rotations.append(rotation)

        commander_id = _seat_occupant(flight_ids, "COMMANDER")
        second_pilot_id = _seat_occupant(flight_ids, "SECOND_PILOT")

        for position, occupant in (("COMMANDER", commander_id), ("SECOND_PILOT", second_pilot_id)):
            if occupant is not None:
                rotation.seats[position] = PreviewSeat(
                    operating_position=position, crew_id=occupant, already_real=True,
                    role_assigned=(crew_by_id[occupant]["role"] if occupant in crew_by_id else None),
                )

        if commander_id is not None and second_pilot_id is not None:
            rotation.outcome = OUTCOME_ALREADY_COVERED
            continue

        if commander_id is not None or second_pilot_id is not None:
            # Exactly one seat already real -- fill the remaining one
            # against it.
            if second_pilot_id is None:
                fill_position, other_crew_id = "SECOND_PILOT", commander_id
                pool = pools["SECOND_PILOT"]
            else:
                fill_position, other_crew_id = "COMMANDER", second_pilot_id
                pool = pools["COMMANDER"]

            partner_age = _age_of(crew_by_id, other_crew_id, reference_date)
            candidates = [
                Candidate(crew_id=row["crew_id"], duty_count=duty_counts[fill_position].get(row["crew_id"], 0),
                          age=_age_of(crew_by_id, row["crew_id"], reference_date))
                for _, row in pool.iterrows() if row["crew_id"] != other_crew_id
            ]
            ordered = order_candidates(candidates, domestic=domestic, partner_age=partner_age)

            filled_id, filled_result, reasons = None, None, []
            for crew_id in ordered:
                crew_row = crew_by_id[crew_id]
                # dry_run=True: the whole gate runs, nothing is written.
                # audit_trials=False — same reasoning as the pair search
                # below; this is the one-seat-already-real variant of the
                # same speculative loop.
                result = assignment_service.assign_crew_to_duty(
                    crew_id, flight_ids, crew_row["role"], app_user=app_user,
                    roster_status=ACCEPTED_ROSTER_STATUS, operating_position=fill_position,
                    prefetch=prefetch, audit_trials=False, dry_run=True)
                if result.status == "ALLOWED":
                    filled_id, filled_result = crew_id, result
                    break
                reasons.append(_reject_reason(crew_id, result))

            if filled_id is not None:
                duty_counts[fill_position][filled_id] = duty_counts[fill_position].get(filled_id, 0) + 1
                provisional.add_duty(
                    crew_id=filled_id, duty_id=filled_result.duty_id,
                    report_time=filled_result.computed_report_time,
                    debrief_time=filled_result.computed_debrief_time,
                    role_assigned=crew_by_id[filled_id]["role"],
                    operating_position=fill_position, flights=flight_rows)
                rotation.seats[fill_position] = PreviewSeat(
                    operating_position=fill_position, crew_id=filled_id,
                    role_assigned=crew_by_id[filled_id]["role"],
                    duty_id=filled_result.duty_id,
                    report_time=filled_result.computed_report_time,
                    debrief_time=filled_result.computed_debrief_time,
                )
                rotation.outcome = OUTCOME_PROPOSED
            else:
                reason = "; ".join(reasons) if reasons else "No candidates in pool"
                rotation.seats[fill_position].reason = reason
                rotation.outcome = OUTCOME_UNCOVERED
                rotation.outcome_reason = reason
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

        filled_pair, pair_result_kept = None, None
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
                # decisions. Under dry_run it is belt-and-braces — a dry
                # run writes nothing anyway — but it is left explicit so
                # the flag never silently becomes the only thing standing
                # between this loop and 2,954 audit rows again.
                pair_result = assignment_service.assign_pair_to_duty(
                    commander_candidate_id, second_pilot_candidate_id, flight_ids,
                    app_user=app_user, roster_status=ACCEPTED_ROSTER_STATUS, prefetch=prefetch,
                    audit_trials=False, dry_run=True)
                if pair_result.status == "ALLOWED":
                    filled_pair = (commander_candidate_id, second_pilot_candidate_id)
                    pair_result_kept = pair_result
                    break
                all_reasons.append(
                    _pair_reject_reason(commander_candidate_id, second_pilot_candidate_id, pair_result))
            if filled_pair is not None:
                break

        if filled_pair is not None:
            commander_id, second_pilot_id = filled_pair
            validation = pair_result_kept.validation
            duty_counts["COMMANDER"][commander_id] = duty_counts["COMMANDER"].get(commander_id, 0) + 1
            duty_counts["SECOND_PILOT"][second_pilot_id] = duty_counts["SECOND_PILOT"].get(second_pilot_id, 0) + 1

            # BOTH duties recorded provisionally, before the next
            # rotation is considered. This is the line the whole
            # redesign turns on: skip it and the next rotation validates
            # this pilot as though today were free.
            provisional.add_duty(
                crew_id=commander_id, duty_id=pair_result_kept.commander_duty_id,
                report_time=validation.commander_computed_report_time,
                debrief_time=validation.commander_computed_debrief_time,
                role_assigned="CPT", operating_position="COMMANDER", flights=flight_rows)
            provisional.add_duty(
                crew_id=second_pilot_id, duty_id=pair_result_kept.second_pilot_duty_id,
                report_time=validation.second_pilot_computed_report_time,
                debrief_time=validation.second_pilot_computed_debrief_time,
                role_assigned=crew_by_id[second_pilot_id]["role"],
                operating_position="SECOND_PILOT", flights=flight_rows)

            rotation.seats["COMMANDER"] = PreviewSeat(
                operating_position="COMMANDER", crew_id=commander_id, role_assigned="CPT",
                duty_id=pair_result_kept.commander_duty_id,
                report_time=validation.commander_computed_report_time,
                debrief_time=validation.commander_computed_debrief_time)
            rotation.seats["SECOND_PILOT"] = PreviewSeat(
                operating_position="SECOND_PILOT", crew_id=second_pilot_id,
                role_assigned=crew_by_id[second_pilot_id]["role"],
                duty_id=pair_result_kept.second_pilot_duty_id,
                report_time=validation.second_pilot_computed_report_time,
                debrief_time=validation.second_pilot_computed_debrief_time)
            rotation.outcome = OUTCOME_PROPOSED
        else:
            reason = "; ".join(all_reasons) if all_reasons else "No candidates in pool"
            for position in SEATS:
                rotation.seats[position].reason = reason
            rotation.outcome = OUTCOME_UNCOVERED
            rotation.outcome_reason = reason

    return preview


def accept_preview(preview: GenerationPreview,
                   app_user: Optional[str] = None) -> GenerationPreview:
    """
    Commits a reviewed GenerationPreview, one rotation at a time,
    re-validating each against FRESH data first. Returns the same
    preview object with every rotation's outcome updated — it is the
    proposal before this call and the report after it.

    PER-ROTATION, NOT ALL-OR-NOTHING. A rotation that fails
    re-validation is left unwritten and the rest of the window still
    commits — the same choice publish_window() already makes, for the
    same reason: one pilot's changed circumstances must not cost a
    controller the other thirty-five rotations of work.

    NO PROVISIONAL UNION HERE, deliberately, and no duty-history cache
    that outlives a rotation. Accept re-reads live, and rotation N+1 is
    validated against rotation N's rows because those rows are now
    genuinely committed. The provisional union exists to stand in for
    writes that have not happened yet; during accept they have, so using
    it would mean validating against a prediction when the fact is
    available. A Prefetch per rotation, not one for the run, is what
    keeps that true — the whole point is that each rotation sees the
    ones before it.

    ONE FRESH CREW SNAPSHOT, though, taken here and not carried over
    from the preview. Without it every seat re-read its own crew row:
    2 pilots x 2 reads x 36 rotations = 144 single-row queries to answer
    a question one bulk read answers, which is the same shape as the
    per-candidate birthday lookup removed on 2026-08-22 and would have
    cost minutes against Supabase. Taking it HERE rather than reusing
    the preview's is what preserves "re-validated against fresh data":
    the snapshot is as old as the accept click, not as old as the
    generate click.

    WHAT PARTIAL FAILURE LEAVES BEHIND (the interaction this was
    designed for rather than discovered by):

      * Rotations that wrote are marked WRITTEN. They are real rows now,
        so the preview must stop presenting them as pending — a screen
        that still offered them would invite a second Accept.
      * The rotation that failed is marked REJECTED and KEEPS its
        proposed crew and gains the re-validation reason. Discarding it
        would destroy the only record of what was refused and why, which
        is the one thing on the screen the controller has to act on.
      * The preview is then spent: accepted_at is set and a second
        accept raises. This is not tidiness. The 35 rotations that just
        committed have CHANGED the legality context the rejected
        rotation was proposed in, so its provisional decision is stale
        in exactly the way this whole redesign exists to prevent.
        Re-running generate_preview() rebuilds it against the 35 rows
        that are now real; replaying it would not.

    uncovered_seats is written HERE, not during the preview: a seat is
    recorded as durably uncovered only once the controller has accepted
    the roster that leaves it uncovered. A preview a controller
    abandoned must not have edited the durable record of what is open.
    """
    if preview.is_accepted:
        raise ValueError(
            "This preview has already been accepted. Its provisional "
            "decisions were made against a database that has since "
            "changed — re-run Generate to rebuild them against what is "
            "now committed."
        )

    all_crew = crew_service.get_all_crew(active_only=True)
    crew_by_id = {row["crew_id"]: row for _, row in all_crew.iterrows()}

    for rotation in preview.rotations:
        if rotation.outcome == OUTCOME_UNCOVERED:
            for position in SEATS:
                seat = rotation.seats[position]
                if seat.crew_id is None and seat.reason:
                    _record_uncovered(rotation.rotation_instance_id, rotation.rotation_code,
                                      rotation.rotation_date, position, seat.reason, app_user)
            continue

        if rotation.outcome != OUTCOME_PROPOSED or not rotation.is_writable:
            continue

        to_write = [s for s in rotation.seats.values()
                    if s.crew_id is not None and not s.already_real]
        # Per ROTATION, sharing only the crew snapshot: duty history and
        # flights are read live for each rotation, so the rows written
        # for the previous one are seen by this one.
        prefetch = assignment_service.Prefetch(crew_by_id=crew_by_id)
        try:
            if len(to_write) == 2:
                result = assignment_service.assign_pair_to_duty(
                    rotation.seats["COMMANDER"].crew_id,
                    rotation.seats["SECOND_PILOT"].crew_id,
                    rotation.flight_ids, app_user=app_user,
                    roster_status=ACCEPTED_ROSTER_STATUS, prefetch=prefetch)
                status, reason = result.status, _pair_reject_reason(
                    rotation.seats["COMMANDER"].crew_id,
                    rotation.seats["SECOND_PILOT"].crew_id, result)
            else:
                seat = to_write[0]
                result = assignment_service.assign_crew_to_duty(
                    seat.crew_id, rotation.flight_ids, seat.role_assigned,
                    app_user=app_user, roster_status=ACCEPTED_ROSTER_STATUS,
                    operating_position=seat.operating_position, prefetch=prefetch)
                status, reason = result.status, _reject_reason(seat.crew_id, result)
        except ValueError as exc:
            # A data-integrity surprise between preview and accept (a
            # crew member deactivated, a seat taken by someone else in
            # the meantime, a flight removed). Same treatment as an
            # ordinary re-validation refusal: this rotation only.
            rotation.outcome = OUTCOME_REJECTED
            rotation.outcome_reason = str(exc)
            continue

        if status == "ALLOWED":
            rotation.outcome = OUTCOME_WRITTEN
        else:
            rotation.outcome = OUTCOME_REJECTED
            rotation.outcome_reason = reason

    preview.accepted_at = dt.datetime.now(dt.timezone.utc)

    written = [r for r in preview.rotations if r.outcome == OUTCOME_WRITTEN]
    rejected = [r for r in preview.rotations if r.outcome == OUTCOME_REJECTED]
    if written or rejected:
        log_audit(
            action_type="ROSTER_PREVIEW_ACCEPTED",
            reason=f"{preview.date_from} to {preview.date_to}",
            changed_state=f"{len(written)} rotation(s) written, {len(rejected)} rejected on re-validation",
            warning_or_failure_reason="; ".join(
                f"{r.rotation_code} {r.rotation_date}: {r.outcome_reason}" for r in rejected) or None,
            app_user=app_user,
        )

    return preview


def generate_for_window(date_from: dt.date, date_to: dt.date,
                        app_user: Optional[str] = None) -> GenerationSummary:
    """
    Preview and accept in one call, returning the old GenerationSummary.

    KEPT FOR CALLERS THAT GENUINELY WANT NO REVIEW STEP — scripts, and
    the existing test suite. pages/6_Roster_Generation.py no longer uses
    it: the whole point of the redesign is that a controller sees the
    proposal before any of it is written, and a function that does both
    halves back-to-back cannot offer that.

    It is a wrapper rather than a second implementation so there is no
    second implementation to drift. Note that it therefore validates
    twice — once in the preview, once on accept — which is the honest
    cost of a review step nobody is using here; that is a reason to call
    the two halves separately, not a reason to reimplement this one.
    """
    preview = accept_preview(generate_preview(date_from, date_to, app_user=app_user),
                             app_user=app_user)

    summary = GenerationSummary()
    for rotation in preview.rotations:
        for position in SEATS:
            seat = rotation.seats[position]
            common = dict(rotation_instance_id=rotation.rotation_instance_id,
                          rotation_code=rotation.rotation_code,
                          rotation_date=rotation.rotation_date,
                          operating_position=position)
            if seat.already_real:
                summary.already_covered.append(SeatResult(crew_id=seat.crew_id, **common))
            elif seat.crew_id is not None and rotation.outcome == OUTCOME_WRITTEN:
                summary.filled.append(SeatResult(crew_id=seat.crew_id, **common))
            elif seat.crew_id is None and seat.reason:
                summary.uncovered.append(SeatResult(reason=seat.reason, **common))
            elif rotation.outcome == OUTCOME_REJECTED:
                # Proposed, then refused at accept. Not "filled" — no row
                # exists — and not uncovered_seats' business either, since
                # nothing was committed to leave a durable gap behind.
                summary.uncovered.append(SeatResult(reason=rotation.outcome_reason, **common))
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
