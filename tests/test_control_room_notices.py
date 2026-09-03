"""Control Room's confirmations — and its SWAP ALERTS — have to reach
the browser.

`st.rerun()` abandons the current run, so anything written before it in
that same run is discarded and never rendered (HANDOVER 2026-09-03).
Two paths on Control Room ended that way, and one of them matters a
great deal: the ALLOWED branch of the crew-assignment handler wrote its
success line, its pair alerts, AND its swap alerts — "this assignment
breaks the legality of N already-scheduled future duty(ies)", including
"no legal candidates found" — and then called st.rerun(). **A
controller crewing a flight had never seen any of it.**

Scoped precisely, because not every branch was broken: REJECTED and
NEEDS_REVIEW do NOT rerun, so their messages have always been visible.
Only the two paths ending in st.rerun() needed queueing, and this file
pins both directions so a later "tidy-up" cannot quietly route a
visible message back through the discard.

DB-free, which matters twice over here: an AppTest assertion that a
message merely EXISTS can pass while the browser shows nothing, because
a rerun runs the script twice inside one at.run() and the discarded
first pass survives wherever the second is shorter. These assert on the
run AFTER the rerun.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from core.legality.pcaa_ano012_core import AlertStatus
from services.alert_summary import AlertSummary
from tests.conftest import page_path

D = dt.datetime

_CREW = pd.DataFrame([
    {"crew_id": "CPT-01", "name": "MUHAMMAD WAQAR", "role": "CPT",
     "operator_staff_id": "AE-95", "base": "KHI", "is_active": True},
    {"crew_id": "FO-01", "name": "IBTISAM MUZZAFAR", "role": "FO",
     "operator_staff_id": "AE-134", "base": "KHI", "is_active": True},
])


class _Conflict:
    def __init__(self, duty_id, candidates):
        self.duty_id = duty_id
        self.role_assigned = "CPT"
        self.report_time = D(2026, 9, 20, 4, 15)
        self.candidates = candidates


def _empty_summary():
    """A real AlertSummary, not None: the REJECTED / NEEDS_REVIEW
    branches hand these straight to format_alert_lines(), which reads
    .target_duty_alerts."""
    return AlertSummary(overall_status=AlertStatus.LEGAL)


class _Validation:
    commander_status = "LEGAL"
    second_pilot_status = "LEGAL"
    pair_alerts = ()

    def __init__(self):
        self.commander_alert_summary = _empty_summary()
        self.second_pilot_alert_summary = _empty_summary()


class _PairResult:
    def __init__(self, status="ALLOWED", commander_conflicts=(), sp_conflicts=()):
        self.status = status
        self.validation = _Validation()
        self.commander_downstream_conflicts = list(commander_conflicts)
        self.second_pilot_downstream_conflicts = list(sp_conflicts)


@pytest.fixture
def control_room(monkeypatch):
    def render(pair_result=None):
        import db.db as db
        from services import assignment_service as asg
        from services import crew_service as cs
        from services import flight_service as fs

        written = []
        monkeypatch.setattr(db, "test_connection", lambda: "no database (test)")
        monkeypatch.setattr(cs, "get_all_crew", lambda active_only=True: _CREW.copy())
        monkeypatch.setattr(fs, "add_flight",
                            lambda data, app_user=None: (written.append(data), 4242)[1])
        monkeypatch.setattr(
            asg, "assign_pair_to_new_flights",
            lambda c, s, flights, app_user=None: (
                pair_result or _PairResult(), [4242]))

        at = AppTest.from_file(str(page_path("pages/1_Control_Room.py")))
        at.session_state["app_user"] = "occ1"
        at.session_state["control_room_assign_pair"] = pair_result is not None
        at.run()
        return at, written
    return render


def _fill(at, flight_no="EPE 786"):
    for t in at.text_input:
        if t.label.startswith("Flight No"):
            t.input(flight_no)
        elif t.label.startswith("Origin"):
            t.input("KHI")
        elif t.label.startswith("Destination"):
            t.input("LHE")
        elif t.label.startswith("Departure time"):
            t.input("0500")
        elif t.label.startswith("Arrival time"):
            t.input("0700")


def _save(at):
    [b for b in at.button if "Check legality" in b.label][0].click()
    return at.run()


# ------------------------------------------------------------------
# The messages that were being thrown away
# ------------------------------------------------------------------

def test_the_flight_saved_confirmation_survives_the_rerun(control_room):
    at, written = control_room()
    _fill(at)
    at = _save(at)

    assert not at.exception, at.exception
    assert written, "the flight was not saved at all"
    assert any("saved with no crew assigned" in s.value for s in at.success), (
        "the confirmation was written before st.rerun() and discarded"
    )


def test_the_swap_alert_survives_the_rerun(control_room):
    """THE message this fix exists for. It says the assignment just made
    breaks duties already on the roster — and it was being written into
    a run that st.rerun() then threw away."""
    result = _PairResult(commander_conflicts=[_Conflict("DUTY-9", candidates=[])])
    at, _ = control_room(pair_result=result)
    _fill(at)
    at = _save(at)

    assert not at.exception, at.exception
    assert any("Swap alert" in e.value for e in at.error), (
        "a controller has never seen this"
    )
    assert any("no legal candidates found" in w.value for w in at.markdown), (
        "the detail lines must survive too, not just the headline"
    )


def test_the_allowed_confirmation_survives_the_rerun(control_room):
    at, _ = control_room(pair_result=_PairResult())
    _fill(at)
    at = _save(at)

    assert not at.exception, at.exception
    assert any("ALLOWED" in s.value for s in at.success)


def test_a_rejected_pair_still_reports_without_queueing(control_room):
    """The other direction, and the reason this fix is scoped rather
    than blanket: REJECTED does NOT rerun, so its message was always
    visible. If a later tidy-up routes it through the queue it would
    still work — but if a tidy-up adds an st.rerun() here without
    queueing, this test is what notices."""
    at, _ = control_room(pair_result=_PairResult(status="REJECTED"))
    _fill(at)
    at = _save(at)

    assert not at.exception, at.exception
    assert any("REJECTED" in e.value for e in at.error)


# ------------------------------------------------------------------
# Flight No. required in the UI, nullable in the schema
# ------------------------------------------------------------------

def test_a_blank_flight_number_is_refused(control_room):
    at, written = control_room()
    _fill(at, flight_no="")
    at = _save(at)

    assert not at.exception, at.exception
    assert any("Flight No. is required" in e.value for e in at.error)
    assert not written, "a numberless flight was saved anyway"


def test_the_field_is_marked_required_like_every_other_mandatory_one(control_room):
    at, _ = control_room()

    labels = [t.label for t in at.text_input]
    assert "Flight No. *" in labels, labels
    assert not any("optional" in label for label in labels), (
        "the field still describes itself as optional"
    )
