"""Which flights the Roster page offers to crew — and whether the
messages it writes afterwards survive.

TWO THINGS, in one file because they are the same form.

1. THE PICKER'S SCOPE. It offered all 103 flights, 20 Aug – 21 Sep, so
   reaching tomorrow meant scrolling a month of history. The narrowing
   that suggests itself — "only show what's in Current assignments" —
   was checked and rejected: this form is the ONLY UI path to crew an
   existing flight, and Control Room points the controller at it by
   name ("Both cockpit seats will show as UNCOVERED until assigned in
   Roster"), so scoping to already-crewed flights would remove exactly
   the flights that still need crew. It would not have fixed the date
   range either — Current assignments spans the same dates.

   So: scoped BY TIME, and by PLANNED. The second half is a
   correctness fix on its own — assign_pair_to_duty() does not check
   flight status, so the old picker would crew a CANCELLED flight and
   nothing downstream would object.

   The window REACHES BACKWARDS, which is the part that keeps this
   from repeating the mistake it replaced: a PLANNED flight in the
   past that was never crewed must stay reachable.

2. THE THREE st.rerun() SITES THIS PAGE'S FIRST AUDIT MISSED
   (2026-09-05). That audit converted only the flagged-for-review
   section and left both assignment handlers and the unassign
   confirmation writing into the discarded run. The swap alert —
   "this assignment breaks the legality of N already-scheduled future
   duty(ies)" — was fixed on Control Room the same day and left broken
   HERE, on the page a controller actually crews scheduled flights
   from.

DB-free: the DB-gated roster tests skip wherever Postgres is absent,
which is how both of these survived. And these assert on the run AFTER
the rerun, because an AppTest assertion that a message merely EXISTS
can pass while the browser shows nothing.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from core.legality.pcaa_ano012_core import AlertStatus
from services import assignment_service, crew_service, flight_service
from services.alert_summary import AlertSummary
from services.display_labels import flight_label
from tests.conftest import authed_app_test

D = dt.datetime

# Fixed, so the test does not drift with the clock: the page computes
# its default window from pd.Timestamp.now("UTC").date(), and these
# dates sit either side of a TODAY that is patched to match.
TODAY = dt.date(2026, 9, 15)

CREW = [
    {"crew_id": "CPT-01", "role": "CPT", "name": "MUHAMMAD WAQAR",
     "operator_staff_id": "AE-95", "base": "KHI", "is_active": True},
    {"crew_id": "FO-01", "role": "FO", "name": "IBTISAM MUZZAFAR",
     "operator_staff_id": "AE-134", "base": "KHI", "is_active": True},
]

FLIGHT_COLUMNS = ["flight_id", "flight_no", "origin", "destination",
                  "dep_time_planned", "arr_time_planned", "status", "domestic",
                  "aircraft", "other_occupants_operating",
                  "other_occupants_non_operating", "remarks", "rotation_instance_id"]

# One of each case the scope has to decide about.
PAST_PLANNED = 1      # never crewed, never flown — must stay REACHABLE
FUTURE_PLANNED = 2    # the ordinary case
FUTURE_CANCELLED = 3  # crewing it writes roster rows that are already dead
PAST_OPERATED = 4     # it flew; there is nothing to crew


def _flight(flight_id, day_offset, status, flight_no="EPE 786"):
    dep = D.combine(TODAY + dt.timedelta(days=day_offset), dt.time(19, 0))
    return {"flight_id": flight_id, "flight_no": flight_no, "origin": "KHI",
            "destination": "LHE", "dep_time_planned": dep,
            "arr_time_planned": dep + dt.timedelta(hours=2), "status": status,
            "domestic": True, "aircraft": "AP-BNW",
            "other_occupants_operating": "", "other_occupants_non_operating": "",
            "remarks": "", "rotation_instance_id": flight_id}


ALL_FLIGHTS = [
    _flight(PAST_PLANNED, -20, "PLANNED", "EPE 701"),
    _flight(FUTURE_PLANNED, +3, "PLANNED", "EPE 786"),
    _flight(FUTURE_CANCELLED, +4, "CANCELLED", "EPE 787"),
    _flight(PAST_OPERATED, -2, "OPERATED", "EPE 788"),
]


class _Conflict:
    def __init__(self, duty_id, candidates):
        self.duty_id = duty_id
        self.role_assigned = "CPT"
        self.report_time = D(2026, 9, 20, 4, 15)
        self.candidates = candidates


class _Validation:
    commander_status = "LEGAL"
    second_pilot_status = "LEGAL"
    pair_alerts = ()

    def __init__(self):
        self.commander_alert_summary = AlertSummary(overall_status=AlertStatus.LEGAL)
        self.second_pilot_alert_summary = AlertSummary(overall_status=AlertStatus.LEGAL)


class _PairResult:
    def __init__(self, status="ALLOWED", commander_conflicts=()):
        self.status = status
        self.validation = _Validation()
        self.commander_duty_id = "DUTY-9"
        self.second_pilot_duty_id = "DUTY-10"
        self.commander_downstream_conflicts = list(commander_conflicts)
        self.second_pilot_downstream_conflicts = []


@pytest.fixture
def roster(monkeypatch):
    """The REAL page over fake reads, with TODAY pinned."""
    def render(flights=None, pair_result=None):
        frame = pd.DataFrame(
            flights if flights is not None else ALL_FLIGHTS, columns=FLIGHT_COLUMNS)

        class _FixedTimestamp(pd.Timestamp):
            @classmethod
            def now(cls, tz=None):
                return pd.Timestamp(D.combine(TODAY, dt.time(9, 0)), tz=tz)

        monkeypatch.setattr(pd, "Timestamp", _FixedTimestamp)
        monkeypatch.setattr(flight_service, "get_all_flights", lambda **k: frame.copy())
        monkeypatch.setattr(
            assignment_service, "get_roster_for_flight",
            lambda fid, **k: pd.DataFrame(
                columns=["crew_id", "role_assigned", "operating_position",
                         "duty_id", "fdp_hours", "status"]))
        monkeypatch.setattr(crew_service, "get_all_crew", lambda **k: pd.DataFrame(CREW))
        monkeypatch.setattr(
            assignment_service, "duties_needing_review",
            lambda **k: pd.DataFrame(columns=[
                "duty_id", "crew_id", "duty_date", "report_time", "debrief_time",
                "role_assigned", "operating_position", "flight_id",
                "flight_no", "origin", "destination"]))
        monkeypatch.setattr(
            assignment_service, "assign_pair_to_duty",
            lambda c, s, ids, app_user=None: pair_result or _PairResult())

        # Recorded so _offered() can translate the picker's LABELS back
        # into flight_ids. A multiselect's .options are the formatted
        # strings, not the values behind them, so comparing an id
        # against .options directly is a comparison that can never be
        # true — an assertion that something is ABSENT would then pass
        # no matter what the page did.
        global _RENDERED
        _RENDERED = frame
        return authed_app_test("pages/4_Roster.py").run()
    return render


_RENDERED = None


def _picker(at, key="pair_flights"):
    matching = [m for m in at.multiselect if m.key == key]
    assert len(matching) == 1, [m.key for m in at.multiselect]
    return matching[0]


def _queued(at):
    """Everything rendered ABOVE "Current assignments" — i.e. by the
    queue drain at the top of the page.

    POSITION, NOT PRESENCE, and the difference decides whether these
    tests measure anything. Asserting that a message merely EXISTS
    passes either way: st.rerun() runs the script twice inside one
    at.run(), and the discarded first pass survives in the element tree
    wherever the second render is shorter — which for this page it is,
    because the form that wrote the message collapses after a
    successful submit. Verified directly: with the queue mutated to
    write immediately, `any("Swap alert" in e.value for e in at.error)`
    still passed while nothing rendered above the table.

    A queued notice can only appear here, because the drain is the
    first thing after st.title(). A discarded one cannot.
    """
    out = []
    for element in at.main:
        if type(element).__name__ == "Subheader" and "Current assignments" in element.value:
            break
        out.append(str(getattr(element, "value", "")))
    return out


def _offered(at, key="pair_flights"):
    """The flight_ids the picker is offering, recovered from its
    labels."""
    options = set(_picker(at, key).options)
    offered = {int(row["flight_id"]) for _, row in _RENDERED.iterrows()
               if flight_label(row, include_route=True) in options}
    assert len(offered) == len(options), (
        f"a label was offered that no fixture flight produces: {options}"
    )
    return offered


# ------------------------------------------------------------------
# What the picker offers
# ------------------------------------------------------------------

def test_a_past_flight_that_already_flew_is_not_offered(roster):
    """The reported complaint: a month of history to scroll through."""
    assert PAST_OPERATED not in _offered(roster())


def test_a_cancelled_flight_is_not_offered(roster):
    """Not tidying — a correctness fix. assign_pair_to_duty() does not
    check flight status, and cancelling a flight cascades CANCELLED to
    its roster rows, so crew assigned to a cancelled flight is written
    and immediately meaningless. Nothing else refuses this."""
    assert FUTURE_CANCELLED not in _offered(roster())


def test_the_ordinary_future_flight_is_offered(roster):
    assert FUTURE_PLANNED in _offered(roster())


def test_an_uncrewed_flight_is_still_offered(roster):
    """THE case that ruled out scoping to "Current assignments". Every
    flight here has an empty roster, so a crewed-ness filter would
    offer nothing at all — and this form is the only UI path to crew an
    existing flight."""
    assert _offered(roster()), (
        "no flight is offered even though all of them need crew"
    )


def test_the_page_says_what_it_left_out(roster):
    """Nothing hidden silently: a picker that quietly drops flights is
    how "not listing all flights" gets reported again."""
    at = roster()
    assert any("only PLANNED flights can be crewed" in c.value for c in at.caption)


# ------------------------------------------------------------------
# The window reaches backwards
# ------------------------------------------------------------------

def test_a_past_planned_flight_is_out_of_the_default_window(roster):
    """Correct, and only half the story — see the next two tests. It is
    out of the DEFAULT, not out of reach."""
    assert PAST_PLANNED not in _offered(roster())


def test_widening_the_window_reaches_a_past_planned_flight(roster):
    """The stranding this scope must not cause. A PLANNED flight in the
    past was never flown and never cancelled — it is uncrewed work, and
    there is no other UI that can crew it."""
    at = roster()
    at.date_input(key="pair_window_from").set_value(TODAY - dt.timedelta(days=30))
    at = at.run()

    assert not at.exception, at.exception
    assert PAST_PLANNED in _offered(at)


def test_when_every_planned_flight_is_in_the_past_they_are_offered_anyway(roster):
    """A database whose only uncrewed work is overdue must not open on
    an empty picker. The default starts at today only when there is
    something on or after today to start with."""
    at = roster(flights=[_flight(PAST_PLANNED, -20, "PLANNED"),
                         _flight(PAST_OPERATED, -2, "OPERATED")])

    assert not at.exception, at.exception
    assert PAST_PLANNED in _offered(at)


def test_no_planned_flights_at_all_says_so(roster):
    at = roster(flights=[_flight(PAST_OPERATED, -2, "OPERATED"),
                         _flight(FUTURE_CANCELLED, +4, "CANCELLED")])

    assert not at.exception, at.exception
    assert any("No PLANNED flights to crew" in i.value for i in at.info)


def test_both_forms_share_the_scope(roster):
    """The LM/ENGR picker had the identical problem. Two pickers onto
    one flight list must not disagree about which flights exist."""
    at = roster()
    assert _offered(at, "other_flights") == _offered(at, "pair_flights")


# ------------------------------------------------------------------
# The three messages the first audit left in the discarded run
# ------------------------------------------------------------------

def test_the_swap_alert_survives_the_rerun(roster):
    """Fixed on Control Room on 2026-09-05 and left broken here, on the
    page a controller actually crews scheduled flights from."""
    at = roster(pair_result=_PairResult(
        commander_conflicts=[_Conflict("DUTY-9", candidates=[])]))
    _picker(at).select(FUTURE_PLANNED)
    at = at.run()
    [b for b in at.button if "Check legality and assign pair" in b.label][0].click()
    at = at.run()

    assert not at.exception, at.exception
    top = _queued(at)
    assert any("Swap alert" in line for line in top), (
        "a controller has never seen this on the Roster page"
    )
    assert any("no legal candidates found" in line for line in top), (
        "the detail lines must survive too, not just the headline"
    )


def test_the_allowed_confirmation_survives_the_rerun(roster):
    at = roster(pair_result=_PairResult())
    _picker(at).select(FUTURE_PLANNED)
    at = at.run()
    [b for b in at.button if "Check legality and assign pair" in b.label][0].click()
    at = at.run()

    assert not at.exception, at.exception
    assert any("ALLOWED" in line for line in _queued(at))


def test_a_rejected_pair_still_reports_without_queueing(roster):
    """The other direction, and the reason this is scoped rather than
    blanket: REJECTED does NOT rerun, so its message was always
    visible. If a later tidy-up adds an st.rerun() here without
    queueing, this is what notices."""
    at = roster(pair_result=_PairResult(status="REJECTED"))
    _picker(at).select(FUTURE_PLANNED)
    at = at.run()
    [b for b in at.button if "Check legality and assign pair" in b.label][0].click()
    at = at.run()

    assert not at.exception, at.exception
    assert any("REJECTED" in e.value for e in at.error)
    assert not any("REJECTED" in line for line in _queued(at)), (
        "REJECTED does not rerun, so it must NOT be routed through the "
        "queue — it belongs beside the form the controller just used"
    )
