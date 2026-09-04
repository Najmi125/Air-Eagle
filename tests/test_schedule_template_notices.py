"""The LAST page audited for the st.rerun() message swallow.

st.rerun() ABANDONS the current run, so anything written before it is
discarded and never rendered (HANDOVER 2026-09-03). Schedule Templates
had five such sites, and the two that matter are the bulk review
buttons: "Approve selected" and "Reject selected" loop over the
selection, collect every instance that was REFUSED, write
`Instance 12: <why>` for each — and then rerun.

So a controller who selected five drafts and had two refused saw no
error at all. The list came back two rows shorter with the two refusals
still in it, and nothing on screen said why. That is the same shape as
the swap alert on Control Room (2026-09-05): a partial failure reported
into a run that was already being thrown away.

Scoped, not blanket: "Select all visible" and "Clear selection" also
rerun, and were correct as they stood — they write no messages, they
only stage session_state. This file pins BOTH directions.

DB-free on purpose. Everything covering this page's happy paths is
DB-gated and skips wherever Postgres is absent, which is exactly how a
message-swallow reaches production. And it matters twice over here: an
AppTest assertion that a message merely EXISTS can pass while the
browser shows nothing, because a rerun runs the script twice inside one
at.run() and the discarded first pass survives wherever the second
render is shorter. These assert on the run AFTER the rerun.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import page_path

_VERSIONS = pd.DataFrame([{
    "id": 1, "rotation_code": "EPE-786-787", "version": 1,
    "effective_from": dt.date(2026, 1, 1), "effective_until": None,
    "description": "KHI-LHE", "days_of_week": [1, 2, 3, 4, 5],
    "meal_provided": True, "snack_provided": True, "superseded_by": None,
}])
_LEGS = pd.DataFrame([{
    "leg_order": 1, "flight_no": "EPE 786", "origin": "KHI", "destination": "LHE",
    "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
    "day_offset": 0, "domestic": True,
}])
_DRAFTS = pd.DataFrame([
    {"id": 11, "rotation_code": "EPE-786-787", "rotation_date": dt.date(2026, 9, 14),
     "status": "DRAFT", "template_id": 1, "version": 1},
    {"id": 12, "rotation_code": "EPE-786-787", "rotation_date": dt.date(2026, 9, 15),
     "status": "DRAFT", "template_id": 1, "version": 1},
])


@pytest.fixture
def templates(monkeypatch):
    """Every rotation_template_service call the page makes, answered
    from memory. `approve` / `reject` decide what each instance id
    does, so a PARTIAL failure — some accepted, some refused — is
    expressible, which is the case the queueing exists for."""
    def render(approve=None, reject=None):
        from services import rotation_template_service as rts

        def _approve(iid, app_user=None):
            outcome = (approve or {}).get(iid, [901])
            if isinstance(outcome, str):
                raise ValueError(outcome)
            return outcome

        def _reject(iid, reason, app_user=None):
            outcome = (reject or {}).get(iid)
            if isinstance(outcome, str):
                raise ValueError(outcome)

        monkeypatch.setattr(rts, "get_all_rotation_codes", lambda: ["EPE-786-787"])
        monkeypatch.setattr(rts, "get_versions", lambda code: _VERSIONS.copy())
        monkeypatch.setattr(rts, "get_template_legs", lambda tid: _LEGS.copy())
        monkeypatch.setattr(rts, "get_instance_legs", lambda iid: _LEGS.copy())
        monkeypatch.setattr(rts, "get_instances", lambda **kw: (
            _DRAFTS.copy() if kw.get("status") == "DRAFT"
            else _DRAFTS.iloc[0:0].copy()))
        monkeypatch.setattr(rts, "compute_duty_window", lambda legs: ("1815z", "0000z"))
        # Full shape, not a plausible subset: the page reads
        # deletability["reason"], and a fake missing that key fails the
        # test with a KeyError from the PAGE, which reads as a page bug
        # rather than as an under-specified fixture.
        monkeypatch.setattr(rts, "get_template_deletability", lambda tid: {
            "deletable": False, "reason": "2 rotation instances exist",
            "instance_count": 2, "is_superseded": False,
        })
        monkeypatch.setattr(rts, "approve_instance", _approve)
        monkeypatch.setattr(rts, "reject_instance", _reject)

        at = AppTest.from_file(str(page_path("pages/7_Schedule_Templates.py")))
        at.session_state["app_user"] = "occ1"
        at.run()
        return at
    return render


def _click(at, label):
    matching = [b for b in at.button if b.label == label]
    assert len(matching) == 1, f"expected one {label!r}, found {[b.label for b in at.button]}"
    matching[0].click()
    return at.run()


def _queued(at, marker):
    """Everything rendered ABOVE the control named by `marker` — i.e.
    by the queue drain near the top of the page.

    POSITION, NOT PRESENCE, and the difference decides whether these
    tests measure anything at all. Asserting that a message merely
    EXISTS passes either way: st.rerun() runs the script twice inside
    one at.run(), and the discarded first pass survives in the element
    tree wherever the second render is shorter.

    Verified directly (2026-09-06) rather than reasoned about: with
    queue_*_notice() mutated to write immediately — exactly the
    pre-fix behaviour — every presence-based assertion in this file
    still passed while nothing rendered above the control. The
    narrower mutation (one call site converted back) DID fail, which
    is why the weakness was not obvious; a test that catches a
    one-line regression but not a wholesale one is worth knowing about
    rather than trusting.

    A queued notice can only appear above the control, because the
    drain runs before it. A discarded one cannot.
    """
    out = []
    for element in at.main:
        text = str(getattr(element, "label", "") or getattr(element, "value", ""))
        if marker in text and type(element).__name__ in ("Button", "Subheader", "Header"):
            break
        out.append(str(getattr(element, "value", "")))
    return out


def _select_both(at):
    for iid in (11, 12):
        [c for c in at.checkbox if c.key == f"select_{iid}"][0].check()
    return at.run()


# ------------------------------------------------------------------
# The messages that were being thrown away
# ------------------------------------------------------------------

def test_the_approval_confirmation_survives_the_rerun(templates):
    at = _select_both(templates())
    at = _click(at, "Approve selected")

    assert not at.exception, at.exception
    assert any("2 rotation(s) approved" in line
               for line in _queued(at, "Approve selected")), (
        "the confirmation was written before st.rerun() and discarded"
    )


def test_a_refused_approval_says_which_one_and_why(templates):
    """THE message this fix exists for. Two drafts selected, one
    refused: the controller must be told which, and what the service
    said — not left with a list that quietly failed to empty."""
    at = _select_both(templates(
        approve={12: "rotation date is in the past"}))
    at = _click(at, "Approve selected")

    assert not at.exception, at.exception
    top = _queued(at, "Approve selected")
    assert any("Instance 12" in line for line in top), (
        "a controller has never seen this: the refusal named no instance"
    )
    assert any("rotation date is in the past" in line for line in top), (
        "the service's own reason must survive, not just the fact of failure"
    )
    assert any("1 rotation(s) approved" in line for line in top), (
        "the partial success must survive alongside the partial failure"
    )


def test_a_refused_rejection_says_which_one_and_why(templates):
    at = _select_both(templates(reject={12: "already approved"}))
    [t for t in at.text_input if t.key == "review_reject_reason"][0].input("route dropped")
    at = at.run()
    at = _click(at, "Reject selected")

    assert not at.exception, at.exception
    top = _queued(at, "Reject selected")
    assert any("Instance 12" in line for line in top)
    assert any("already approved" in line for line in top)
    assert any("1 rotation(s) rejected" in line for line in top)


# ------------------------------------------------------------------
# The other direction: what was NOT broken stays unqueued
# ------------------------------------------------------------------

def test_select_all_writes_no_message_and_needs_no_queue(templates):
    """"Select all visible" reruns too, and was correct as it stood —
    it stages session_state and says nothing. If a later change starts
    writing a confirmation here without queueing it, the message would
    vanish; this pins the current contract so that change is visible."""
    at = _click(templates(), "Select all visible")

    assert not at.exception, at.exception
    assert all(c.value for c in at.checkbox if c.key in ("select_11", "select_12")), (
        "the click had no effect at all"
    )
    assert not at.success, (
        "this path now writes a confirmation; it reruns, so it must queue it"
    )


def test_the_queue_is_popped_not_read(templates):
    """A notice that stays in session_state reappears on the next
    unrelated action — the failure mode of a queue nobody drains.

    Asserted on SESSION STATE rather than on the next render, and
    deliberately so: AppTest keeps stale elements from an earlier
    at.run() wherever the newer render is shorter, so "the message is
    not on screen any more" is not a question the element tree answers
    honestly. Whether the queue is empty is a question with one
    answer.
    """
    at = _select_both(templates())
    at = _click(at, "Approve selected")
    assert any("approved" in line for line in _queued(at, "Approve selected"))

    # `not in`, not `.get(...)`: AppTest's session_state raises
    # KeyError on a missing key rather than returning None, so the
    # absence has to be asked about directly. A drain that READ the
    # queue would leave the key behind with its list intact.
    assert "schedule_template_notices" not in at.session_state, (
        "the drain read the queue instead of popping it"
    )
