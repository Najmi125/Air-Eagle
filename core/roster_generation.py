"""
core/roster_generation.py

Pure candidate-ordering logic for Phase 7's roster generator
(2026-08-04). No DB, no Streamlit — same placement principle as
core/duty_builder.py/core/duty_summary.py/core/rotation_expansion.py.

This module NEVER decides legality. It only decides which order to
TRY candidates in — the actual yes/no for any candidate is still
decided exclusively by services/assignment_service.py's
assign_crew_to_duty(), every single time. If this module ever appears
to "know" an FTL or pairing rule, that's a second rule source and it's
wrong; it only sorts on crew_id/duty_count/age, the same category
"fewest duties so far" already is — and it doesn't even compute age
itself: `core/` modules don't import from `services/` (the reverse
already holds throughout this codebase), so the caller
(services/roster_generator_service.py) computes each candidate's age
via the existing, already-tested assignment_service.age_on() and
passes already-computed ints in — this module only ever sorts numbers
it's handed, reusing the real age math rather than duplicating it.

Two real, empirically-confirmed coverage-loss failure modes motivate
the age-aware ordering below (see HANDOVER.md's dated entry for the
full derivation and the real gate output that proved each one):

- International: a 65+ pilot in EITHER seat makes the pair
  unconditionally illegal ("illegal if EITHER is 65+") — no partner
  can ever fix it. Plain fewest-duties ordering can pick a 65+
  candidate for the first seat and guarantee the second seat
  unfillable, even when a fully-legal crewing was reachable by
  picking a different, still-fair first candidate.
- Domestic: the SAME failure mode, just rarer, since domestic's
  "illegal only if BOTH 65+" is a looser constraint — confirmed
  empirically with an FO pool entirely 65+ (66, 68): picking a 67yo
  CPT first still loses the seat, picking a 60yo CPT first (still a
  fair, legitimate choice) recovers it.

The fixes are deliberately DIFFERENT, not the same rule applied twice:
international avoidance is unconditional (a 65+ pilot genuinely can't
fly international at all once paired, so avoiding them costs nothing
real); domestic avoidance is CONDITIONAL on the seat actually already
filled turning out to be 65+ (applying it unconditionally on domestic
would systematically under-assign legal 65+ pilots and break the
even-duty-counts fairness goal for no real benefit in the common case).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class Candidate:
    crew_id: str
    duty_count: int
    # Already computed by the caller as of the rotation's own reference
    # date (assignment_service.age_on()) — None only when the crew
    # member has no recorded date_of_birth at all.
    age: Optional[int]


def order_candidates(
    candidates: Sequence[Candidate],
    domestic: bool,
    partner_age: Optional[int] = None,
) -> List[str]:
    """
    Returns crew_ids in the order the generator should TRY them —
    first entry is the first candidate to attempt via
    assign_crew_to_duty(), moving to the next on REJECTED/NEEDS_REVIEW.

    partner_age: the actual age of whoever is already filling the
    OTHER seat of this same rotation, or None when this IS the first
    seat being filled — nothing to condition on yet.

    Age-aware (under-65-first) ordering applies when:
      - domestic is False (international — unconditional, both seats), or
      - partner_age is not None and partner_age >= 65 (domestic,
        conditional on the seat actually already filled)
    Otherwise, plain fewest-duties-first — the ordinary case, and the
    ONLY case for domestic when the first seat isn't yet filled or its
    occupant is under 65.

    A candidate with age=None (no recorded date_of_birth) is treated as
    NOT under 65 for sorting purposes only (pushed later, never
    earlier) — this never blocks an assignment outright,
    assign_crew_to_duty()'s own AE-CREW-PAIR-AGE-001_DOB_MISSING check
    (NEEDS_MANUAL_REVIEW) is still what actually decides that, same as
    always; this is purely about which order to try people in when
    their age can't be used to prioritize them.
    """
    age_aware = (not domestic) or (partner_age is not None and partner_age >= 65)

    def sort_key(candidate: Candidate):
        if not age_aware:
            return (candidate.duty_count,)
        under_65 = candidate.age is not None and candidate.age < 65
        return (0 if under_65 else 1, candidate.duty_count)

    ordered = sorted(candidates, key=sort_key)
    return [c.crew_id for c in ordered]
