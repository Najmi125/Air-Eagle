"""One seat, one pilot — enforced at the service (2026-09-05).

NOTHING REFUSED A SECOND HOLDER OF THE SAME SEAT. Not the database:
migrations/005's partial unique index is on
`(crew_id, flight_id, role_assigned)`, so two DIFFERENT Captains both
written as COMMANDER on one flight collide on nothing. Not the service:
`assign_pair_to_duty()` went from `_validate_pair_internal()` — which
checks crew existence, grade eligibility and legality, and never reads
the roster — straight to `_write_pair_rows()`, which INSERTs
unconditionally.

And it was reachable from the UI in one obvious move. The Roster pair
form offers every PLANNED future flight, crewed ones included, so
selecting an already-crewed flight and assigning a different pair
produced TWO Commanders and TWO Second Pilots, silently. The form is
labelled "Replace crew" as of 2026-09-06 — precisely the operation that
was not implemented. It added.

LATENT, NOT LIVE: checked against production on 2026-09-05, read-only —
no flight had two active holders of one seat. This closes the hole
before it was reached, which is the only good time.

DB-free. `_read_duty_rows()` is the one-line seam every duty read goes
through, so faking it fakes the roster without a database — and on this
machine `.env` points at production, so "without a database" is a
safety property, not a convenience.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from services import assignment_service, flight_service


def _planned_flight(flight_id):
    return pd.Series({
        "flight_id": flight_id, "flight_no": f"EPE {700 + flight_id}",
        "origin": "KHI", "destination": "LHE", "status": "PLANNED",
        "domestic": True,
    })


@pytest.fixture
def roster(monkeypatch):
    """`rows` is a list of (crew_id, flight_id, operating_position,
    status). The fake honours the WHERE clause the real query uses,
    rather than returning everything — a fake that ignores the filters
    would let a test pass on behaviour the database would never
    produce."""
    def install(rows):
        reads = []

        def fake_read(query, engine, params):
            reads.append(params)
            matched = {
                crew_id for crew_id, fid, position, status in rows
                if fid in params["flight_ids"]
                and position == params["operating_position"]
                and status != "CANCELLED"
            }
            return pd.DataFrame({"crew_id": sorted(matched)})

        monkeypatch.setattr(assignment_service, "_read_duty_rows", fake_read)
        monkeypatch.setattr(flight_service, "get_flight", _planned_flight)
        return reads
    return install


# ------------------------------------------------------------------
# The refusal
# ------------------------------------------------------------------

def test_a_second_commander_is_refused(roster):
    """THE defect. Two Captains, one seat, one flight."""
    roster([("CPT-01", 5, "COMMANDER", "PLANNED")])
    with pytest.raises(ValueError) as exc:
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])

    message = str(exc.value)
    assert "Commander is already held by CPT-01" in message, message


def test_a_second_second_pilot_is_refused(roster):
    roster([("FO-01", 5, "SECOND_PILOT", "PLANNED")])
    with pytest.raises(ValueError, match="Second Pilot is already held by FO-01"):
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])


def test_the_refusal_says_who_and_what_to_do_instead(roster):
    """A controller who meant to swap the crew needs to be sent
    somewhere, not just stopped. Unassign is a real control on the same
    page."""
    roster([("CPT-01", 5, "COMMANDER", "PLANNED")])
    with pytest.raises(ValueError) as exc:
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])

    message = str(exc.value)
    assert "CPT-01" in message, "the refusal does not say who is in the seat"
    assert "Unassign" in message
    assert "reason and audit record" in message, (
        "the refusal must say WHY it will not just overwrite — cancelling "
        "somebody's duty is a decision with its own audit trail"
    )


def test_a_multi_sector_duty_is_refused_on_any_sector(roster):
    """A duty is a set of flights. A seat taken on the second leg is
    still a seat taken."""
    roster([("CPT-01", 6, "COMMANDER", "PLANNED")])
    with pytest.raises(ValueError, match="already held"):
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5, 6, 7])


def test_the_single_seat_door_refuses_too(roster):
    """assign_crew_to_duty() fills the REMAINING seat of a real pair. It
    has always confirmed the OTHER seat is taken; nothing confirmed this
    one was free. Both doors, or the rule is a suggestion."""
    roster([("CPT-01", 5, "COMMANDER", "PLANNED")])
    with pytest.raises(ValueError, match="Commander is already held"):
        assignment_service.assign_crew_to_duty(
            "CPT-06", [5], "CPT", operating_position="COMMANDER")


def test_the_refusal_writes_no_audit_row(roster, monkeypatch):
    """It never reached validation, so it is not a legality decision,
    and the audit trail records decisions."""
    roster([("CPT-01", 5, "COMMANDER", "PLANNED")])
    written = []
    monkeypatch.setattr(assignment_service, "log_audit",
                        lambda **kwargs: written.append(kwargs))

    with pytest.raises(ValueError):
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])

    assert not written, written


# ------------------------------------------------------------------
# What must NOT be refused
# ------------------------------------------------------------------

def test_an_empty_seat_is_not_refused(roster):
    """The ordinary case, and the one a wrong guard breaks first."""
    roster([])
    # Gets past the seat guard and on into real validation, which needs
    # a database — so the only honest DB-free assertion is that the
    # refusal is NOT the thing that stops it.
    with pytest.raises(Exception) as exc:
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])
    assert "already held" not in str(exc.value)


def test_the_same_pilot_is_not_refused_from_the_seat_they_hold(roster):
    """Re-running an assignment that is already correct must not become
    impossible. The pilot being assigned is excluded from the holders,
    so only SOMEBODY ELSE blocks the seat."""
    roster([("CPT-06", 5, "COMMANDER", "PLANNED"),
            ("FO-02", 5, "SECOND_PILOT", "PLANNED")])
    with pytest.raises(Exception) as exc:
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])
    assert "already held" not in str(exc.value)


def test_a_cancelled_assignment_does_not_block_the_seat(roster):
    """Unassigning marks CANCELLED rather than deleting — the same
    permanent-record pattern as flights. A cancelled row that still
    blocked the seat would make unassign-then-reassign impossible,
    which is exactly the workflow the refusal above sends people to."""
    roster([("CPT-01", 5, "COMMANDER", "CANCELLED")])
    with pytest.raises(Exception) as exc:
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])
    assert "already held" not in str(exc.value)


def test_a_proposed_assignment_does_block_the_seat(roster):
    """A generator proposal is a real claim on the seat until somebody
    rejects it. Letting a manual assignment land on top of one produces
    exactly the two holders this exists to prevent — and production
    holds 24 PROPOSED rows today, so this is a live case."""
    roster([("CPT-01", 5, "COMMANDER", "PROPOSED")])
    with pytest.raises(ValueError, match="already held by CPT-01"):
        assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])


# ------------------------------------------------------------------
# Cost
# ------------------------------------------------------------------

def test_the_seat_read_is_cached_per_duty(roster):
    """The generator trials every commander against every second pilot,
    so an uncached read here would fire C x S times per rotation — the
    quadratic term generation spent 2026-08-22 removing. Seat occupancy
    cannot change during a preview, because a preview writes nothing,
    so one read per (duty, seat) is correct as well as cheap."""
    reads = roster([("CPT-01", 5, "COMMANDER", "PLANNED")])
    prefetch = assignment_service.Prefetch()

    for _ in range(4):
        with pytest.raises(ValueError):
            assignment_service.assign_pair_to_duty(
                "CPT-06", "FO-02", [5], prefetch=prefetch)

    # TWO, not one: the pair guard asks about both seats, and
    # _refuse_occupied_seats() collects every occupied seat before
    # raising rather than stopping at the first — so a controller who
    # has BOTH seats taken is told both, not sent round twice.
    assert len(reads) == 2, (
        f"{len(reads)} reads for four trials of the same duty — expected "
        f"one per seat, cached; the prefetch cache is not being used"
    )


def test_without_a_prefetch_every_call_reads_fresh(roster):
    """The other direction. A manual assignment from a page passes no
    prefetch and MUST see what is committed now, not what was true
    when some earlier call looked."""
    reads = roster([("CPT-01", 5, "COMMANDER", "PLANNED")])

    for _ in range(3):
        with pytest.raises(ValueError):
            assignment_service.assign_pair_to_duty("CPT-06", "FO-02", [5])

    assert len(reads) == 6, (
        f"{len(reads)} reads for three uncached calls — expected two per "
        f"call, one per seat"
    )
