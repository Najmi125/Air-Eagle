"""
tests/test_roster_generation.py

Pure logic — no database needed. Covers core/roster_generation.py's
order_candidates(): plain fewest-duties ordering, the unconditional
international under-65-first fix, and the CONDITIONAL domestic fix
(only kicks in once the first-filled seat is confirmed 65+) — including
the explicit regression case proving ordinary domestic ordering is
unaffected when the first pick is under 65, per the empirical correction
recorded in HANDOVER.md's 2026-08-04 roster-generator entry.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.roster_generation import Candidate, order_candidates

CANDS = [
    Candidate(crew_id="A", duty_count=3, age=40),
    Candidate(crew_id="B", duty_count=1, age=67),
    Candidate(crew_id="C", duty_count=2, age=30),
]


def test_domestic_first_seat_is_plain_fewest_duties():
    """partner_age=None (first seat being filled) -> no age awareness,
    ordered purely by duty_count regardless of any candidate's own age."""
    assert order_candidates(CANDS, domestic=True) == ["B", "C", "A"]


def test_domestic_regression_unaffected_when_first_pick_under_65():
    """The conditional fix must NOT change ordinary domestic behavior
    when the seat already filled is under 65 — plain fewest-duties,
    identical to the no-partner case."""
    assert order_candidates(CANDS, domestic=True, partner_age=40) == ["B", "C", "A"]


def test_domestic_switches_to_under_65_first_when_partner_is_65_plus():
    """The empirically-confirmed fix: once the first-filled domestic
    seat's occupant turns out to be 65+, the second seat orders
    under-65 candidates first, then fewest-duties within each group."""
    assert order_candidates(CANDS, domestic=True, partner_age=67) == ["C", "A", "B"]


def test_domestic_partner_exactly_65_counts_as_65_plus():
    assert order_candidates(CANDS, domestic=True, partner_age=65) == ["C", "A", "B"]


def test_international_is_unconditionally_under_65_first_with_no_partner():
    assert order_candidates(CANDS, domestic=False) == ["C", "A", "B"]


def test_international_is_unconditionally_under_65_first_even_when_partner_under_65():
    """International never falls back to plain fewest-duties, unlike
    domestic — the asymmetry is deliberate (see module docstring)."""
    assert order_candidates(CANDS, domestic=False, partner_age=30) == ["C", "A", "B"]


def test_missing_age_is_pushed_later_never_earlier_and_never_excluded():
    cands = [
        Candidate(crew_id="X", duty_count=5, age=None),
        Candidate(crew_id="Y", duty_count=1, age=70),
    ]
    result = order_candidates(cands, domestic=False)
    assert result == ["Y", "X"]
    assert set(result) == {"X", "Y"}


def test_tie_in_duties_and_age_eligibility_is_deterministic():
    """Python's sort is stable: when duty_count and under-65 status are
    both equal, input order is preserved — a deterministic tiebreak,
    not an arbitrary one, which matters for idempotency (Q7)."""
    cands = [
        Candidate(crew_id="Z", duty_count=2, age=30),
        Candidate(crew_id="Y", duty_count=2, age=25),
    ]
    assert order_candidates(cands, domestic=True) == ["Z", "Y"]
    assert order_candidates(list(reversed(cands)), domestic=True) == ["Y", "Z"]
