"""The flag has to be findable and closable, or it is not a safety
feature.

Added with crew-change revalidation (2026-09-05). Until then nothing
could clear a NEEDS_REVIEW flag — the only writer was
_recompute_one_duty_after_delay(), nothing reversed it, and no page
even listed the flagged duties. Adding a second flagger, one that can
flag many duties from a single crew correction, without an exit would
have made correcting a crew record something people avoid.

DB-free: both pages are driven over fakes, so these run wherever
Postgres is absent — which is where a page-level defect on this path
would otherwise hide.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

import services.assignment_service as asg
import services.crew_service as crew_service
from tests.conftest import authed_app_test

D = dt.datetime

REVIEW_COLUMNS = ["duty_id", "crew_id", "duty_date", "report_time", "debrief_time",
                  "role_assigned", "operating_position", "flight_id",
                  "flight_no", "origin", "destination"]

CREW = [{"crew_id": "CPT-03", "name": "SYED FAHIM MAHMOOD", "role": "CPT",
         "operator_staff_id": "AE-143", "base": "KHI", "is_active": True}]


def _flagged_row(duty_id="DUTY-1", crew_id="CPT-03"):
    return {"duty_id": duty_id, "crew_id": crew_id,
            "duty_date": dt.date(2026, 9, 12),
            "report_time": D(2026, 9, 12, 18, 15),
            "debrief_time": D(2026, 9, 13, 0, 0),
            "role_assigned": "CPT", "operating_position": "COMMANDER",
            "flight_id": 53, "flight_no": "EPE 786",
            "origin": "KHI", "destination": "LHE"}


@pytest.fixture
def roster_page(monkeypatch):
    def render(flagged_rows, cleared=1, raises=None):
        calls = {"clear": []}

        def duties_needing_review(**kwargs):
            if raises is not None:
                raise raises
            return pd.DataFrame(flagged_rows, columns=REVIEW_COLUMNS)

        def clear(duty_id, reason, app_user=None):
            calls["clear"].append((duty_id, reason))
            if not reason or not reason.strip():
                raise ValueError("A reason is required to clear a review flag.")
            return cleared

        monkeypatch.setattr(asg, "duties_needing_review", duties_needing_review)
        monkeypatch.setattr(asg, "clear_duty_review_flag", clear)
        monkeypatch.setattr(crew_service, "get_all_crew",
                            lambda **k: pd.DataFrame(CREW))
        monkeypatch.setattr(asg, "get_roster_for_flight",
                            lambda fid, **k: pd.DataFrame(
                                columns=["crew_id", "role_assigned",
                                         "operating_position", "duty_id",
                                         "fdp_hours", "status"]))
        from services import flight_service
        monkeypatch.setattr(flight_service, "get_all_flights",
                            lambda **k: pd.DataFrame(columns=[
                                "flight_id", "flight_no", "origin", "destination",
                                "dep_time_planned", "arr_time_planned", "status",
                                "domestic"]))
        return authed_app_test("pages/4_Roster.py").run(), calls
    return render


def _button(at, label):
    return [b for b in at.button if b.label == label]


# ------------------------------------------------------------------
# Findable
# ------------------------------------------------------------------

def test_a_flagged_duty_is_listed_with_who_and_when(roster_page):
    at, _ = roster_page([_flagged_row()])

    assert not at.exception, at.exception
    assert any("need a human to look at them" in w.value for w in at.warning)
    table = at.dataframe[-1].value
    assert "DUTY-1" in list(table["Duty"])
    # Named, not bare-id'd. Deliberately NOT asserting the exact display
    # rule: the naming rule is itself queued to change (the hardcoded
    # lookup table), and this section's contract is "a human can tell
    # who this is", not "the rule spells it this way".
    assert "CPT-03" not in list(table["Crew"]), (
        f"the crew column shows a raw crew_id: {list(table['Crew'])}"
    )
    assert any("Mahmood" in c or "Fahim" in c for c in table["Crew"]), (
        list(table["Crew"])
    )


def test_no_flags_says_so_rather_than_showing_an_empty_table(roster_page):
    at, _ = roster_page([])

    assert not at.exception, at.exception
    assert any("No duties are currently flagged" in c.value for c in at.caption)
    assert not _button(at, "Clear review flag")


def test_the_section_cannot_take_the_page_down(roster_page):
    """A listing added on top of the page's real job must degrade, not
    crash — the Schedule Templates delete control DID take its page
    down on 2026-08-19 before it was wrapped."""
    at, _ = roster_page([], raises=RuntimeError("boom"))

    assert not at.exception, at.exception
    assert any("Flagged duties unavailable" in c.value for c in at.caption)
    # The page's actual job survives.
    assert any("Current assignments" in s.value for s in at.subheader)


# ------------------------------------------------------------------
# Closable
# ------------------------------------------------------------------

def test_clearing_requires_a_reason_and_says_so(roster_page):
    at, calls = roster_page([_flagged_row()])

    _button(at, "Clear review flag")[0].click()
    at = at.run()

    assert not at.exception, at.exception
    assert any("reason is required" in e.value for e in at.error), (
        "a refusal must explain itself, not raise"
    )


def test_clearing_with_a_reason_passes_it_through_to_the_service(roster_page):
    """The reason is the point of the control: the audit row has to say
    who decided this duty was fine and why."""
    at, calls = roster_page([_flagged_row()])

    [i for i in at.text_input if "What did you check" in i.label][0].input(
        "Checked with the Chief Pilot")
    _button(at, "Clear review flag")[0].click()
    at = at.run()

    assert not at.exception, at.exception
    assert calls["clear"] == [("DUTY-1", "Checked with the Chief Pilot")]
    assert any("Review flag cleared" in s.value for s in at.success), (
        "the confirmation must survive the st.rerun() that follows"
    )


def test_clearing_a_duty_that_was_not_flagged_says_nothing_changed(roster_page):
    at, _ = roster_page([_flagged_row()], cleared=0)

    [i for i in at.text_input if "What did you check" in i.label][0].input("looked")
    _button(at, "Clear review flag")[0].click()
    at = at.run()

    assert not at.exception, at.exception
    assert any("nothing changed" in w.value for w in at.warning)
