"""Only PLANNED flights can be crewed (operator decision, 2026-09-05).

Until this rule existed, `assign_crew_to_duty()` and
`assign_pair_to_duty()` validated crew legality exhaustively and NEVER
ASKED WHAT STATE THE FLIGHT WAS IN. So crew could be written onto a
CANCELLED flight — where `cancel_flight()` has already cascaded
CANCELLED to that flight's roster rows, making the assignment dead the
moment it is made, with nobody told — or onto one that had already
OPERATED, which is a record of what happened rather than a plan anyone
can still change.

ENFORCED AT THE GATE, NOT IN THE PICKER, and the distinction is the
whole point of this file. `pages/4_Roster.py` stopped OFFERING
non-PLANNED flights on 2026-09-06; that guarded exactly one caller. The
generator, a future page, a script and a console session all reach
these functions directly. A guard in front of one door is not a guard
on the room, so these tests call the SERVICE.

DB-free. Every case here refuses before any connection is opened, which
is what makes that true — and it matters on this machine specifically,
where `.env` points at production.

DB-FREE MEANS get_engine() TOO, and that is the correction this file
needed (2026-09-05). These reached `engine = get_engine()` at the top
of the entry point before any guard ran. On a machine with a .env they
passed; on one without, all four failed with "DATABASE_URL not set" —
so the tests were not DB-free, they were .env-dependent. Same class as
the round-trip guards of 2026-08-27.

`isolate_from_database()` is the existing net for exactly this: it
replaces `get_engine` and `log_audit` on EVERY service module, because
`from db.db import get_engine` binds a COPY into each module's
namespace and patching one module does nothing to another. The engine
it installs raises on any use, so a guard that let something through
fails loudly here rather than opening a connection — which, against
this machine's .env, would mean opening one to production.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# tests/ as well as the repo root: isolate_from_database() lives in
# test_generation_round_trips and is imported by bare module name, the
# same way tests/test_partial_accept.py and
# tests/test_cross_rotation_legality.py already reach it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import pytest

from services import assignment_service, flight_service
from services.assignment_service import (CREWABLE_FLIGHT_STATUSES,
                                          _refuse_uncrewable_flights)
from test_generation_round_trips import isolate_from_database


def _flight(flight_id, status):
    return pd.Series({
        "flight_id": flight_id, "flight_no": f"EPE {700 + flight_id}",
        "origin": "KHI", "destination": "LHE", "status": status,
        "domestic": True,
    })


@pytest.fixture
def flights(monkeypatch):
    """`by_id` maps flight_id -> status; anything absent does not
    exist."""
    def install(by_id):
        isolate_from_database(monkeypatch)
        monkeypatch.setattr(
            flight_service, "get_flight",
            lambda fid: (_flight(fid, by_id[fid]) if fid in by_id else None))
    return install


# ------------------------------------------------------------------
# The rule itself
# ------------------------------------------------------------------

def test_planned_is_the_only_crewable_status():
    """Stated once, in one place. The operator may revisit this —
    DISRUPTED is the plausible next candidate — and when they do, this
    is the line that changes."""
    assert CREWABLE_FLIGHT_STATUSES == frozenset({"PLANNED"})


def test_a_planned_flight_passes(flights):
    flights({1: "PLANNED"})
    _refuse_uncrewable_flights([1])  # does not raise


@pytest.mark.parametrize("status", ["CANCELLED", "OPERATED", "DISRUPTED"])
def test_every_other_status_is_refused(flights, status):
    flights({1: status})
    with pytest.raises(ValueError) as exc:
        _refuse_uncrewable_flights([1])
    assert status in str(exc.value)


def test_the_refusal_names_every_offending_leg(flights):
    """A duty is a list of sectors. Naming only the first would send a
    controller to fix one leg and hit the same refusal again."""
    flights({1: "PLANNED", 2: "CANCELLED", 3: "OPERATED"})
    with pytest.raises(ValueError) as exc:
        _refuse_uncrewable_flights([1, 2, 3])

    message = str(exc.value)
    assert "2 is CANCELLED" in message
    assert "3 is OPERATED" in message


def test_the_refusal_says_why_not_just_what(flights):
    """A controller reading this has to know whether to fix the flight
    or take the duty elsewhere. "Refused" alone tells them neither."""
    flights({1: "CANCELLED"})
    with pytest.raises(ValueError) as exc:
        _refuse_uncrewable_flights([1])

    message = str(exc.value)
    assert "Only PLANNED flights can be crewed" in message
    assert "outside the system" in message, (
        "the operator's actual instruction — OCC handles these off-system "
        "— is the part that tells a controller what to do next"
    )


def test_a_missing_flight_is_left_to_the_caller_to_report(flights):
    """Deliberately silent. "No such flight" is a different error with
    its own message in each caller, and reporting it twice, differently,
    from two places is worse than reporting it once."""
    flights({})
    _refuse_uncrewable_flights([999])  # does not raise


# ------------------------------------------------------------------
# Both write doors, called as a caller would
# ------------------------------------------------------------------

def test_assign_pair_to_duty_refuses_at_the_service(flights):
    flights({1: "CANCELLED"})
    with pytest.raises(ValueError, match="Only PLANNED flights can be crewed"):
        assignment_service.assign_pair_to_duty("CPT-01", "FO-01", [1])


def test_assign_crew_to_duty_refuses_at_the_service(flights):
    """The LM/ENGR door. Both doors, or the rule is a suggestion."""
    flights({1: "OPERATED"})
    with pytest.raises(ValueError, match="Only PLANNED flights can be crewed"):
        assignment_service.assign_crew_to_duty("LM-01", [1], "LM")


def test_the_refusal_comes_before_the_partner_lookup(flights, monkeypatch):
    """Ordering, not just outcome. `_find_paired_pilot()` queries the
    database looking for a colleague on the other seat — on a flight
    nobody may be assigned to at all."""
    flights({1: "CANCELLED"})

    def must_not_run(*a, **k):
        raise AssertionError("looked for a partner on an uncrewable flight")

    monkeypatch.setattr(assignment_service, "_find_paired_pilot", must_not_run)
    with pytest.raises(ValueError, match="Only PLANNED"):
        assignment_service.assign_crew_to_duty(
            "CPT-01", [1], "CPT", operating_position="SECOND_PILOT")


def test_the_refusal_writes_no_audit_row(flights, monkeypatch):
    """A refusal that never reached validation is not a legality
    decision, and the audit trail records decisions. The 2,954-row
    incident (2026-08-26) is what that principle costs when it slips."""
    flights({1: "CANCELLED"})
    written = []
    monkeypatch.setattr(assignment_service, "log_audit",
                        lambda **kwargs: written.append(kwargs))

    with pytest.raises(ValueError):
        assignment_service.assign_pair_to_duty("CPT-01", "FO-01", [1])

    assert not written, written


# ------------------------------------------------------------------
# What must NOT start refusing
# ------------------------------------------------------------------

def test_the_generator_reads_the_same_constant():
    """One rule, one place. The generator has to make the same decision
    a rotation early — to mark a rotation UNCOVERED rather than let the
    gate raise C x S times through a loop with no try/except — and a
    second copy of "PLANNED" would be a rule that could drift."""
    source = Path(__file__).resolve().parent.parent / "services" / "roster_generator_service.py"
    text = source.read_text(encoding="utf-8")
    assert "assignment_service.CREWABLE_FLIGHT_STATUSES" in text
    assert '"PLANNED"' not in text.split("uncrewable = [")[1].split("]")[0], (
        "the generator restates the rule instead of reading it"
    )


def test_the_read_only_pair_check_is_untouched():
    """`validate_pair()` is a QUESTION — "could this seat be filled by
    someone else" — and it is what the swap-alert scan asks through.
    Making a question raise on a cancelled flight would turn a report
    into a crash, so the guard sits in the two write entry points and
    NOT in `_validate_pair_internal()` which they share with it.

    Asserted structurally because the alternative is a DB-gated test
    that skips exactly where this would break."""
    source = Path(__file__).resolve().parent.parent / "services" / "assignment_service.py"
    body = source.read_text(encoding="utf-8").split("def _validate_pair_internal(")[1]
    body = body.split("\ndef ")[0]
    assert "_refuse_uncrewable_flights" not in body, (
        "the shared read-only core now refuses, which makes the swap-alert "
        "scan raise on a flight that has been cancelled since"
    )
