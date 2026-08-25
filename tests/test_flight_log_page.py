"""
tests/test_flight_log_page.py

Uses Streamlit's AppTest framework to actually drive
pages/3_Flight_Log.py.
"""
import os
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from db.db import get_engine
from tests.conftest import authed_app_test


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = authed_app_test("pages/3_Flight_Log.py")
    yield at

    get_engine.cache_clear()


def _by_label(elements, label):
    matches = [e for e in elements if e.label == label]
    assert len(matches) == 1, (
        f"expected exactly one element labeled {label!r}, found "
        f"{[e.label for e in elements]}"
    )
    return matches[0]


def _click(at, label):
    """Click a button BY LABEL and return the resulting AppTest.

    RUNS THE SCRIPT. The return value is the new state, so callers MUST
    capture it:

        at = _click(at, "Save")       # correct
        _click(at, "Save"); at.run()  # WRONG — reruns the stale object
                                      # and loses the click's effect

    That second form is not hypothetical: it shipped on 2026-08-21 and
    turned an IndexError into a silent assertion failure, because the
    cancel never took effect. A helper that runs the script invisibly is
    easy to misuse, so the contract is stated here rather than implied.
    """
    matches = [b for b in at.button if b.label == label]
    assert len(matches) == 1, (
        f"expected exactly one {label!r} button, found {[b.label for b in at.button]}"
    )
    matches[0].click()
    return at.run()


def test_page_loads_without_exception(page_app):
    at = page_app.run()
    assert not at.exception


def test_empty_flight_log_shows_info_message(page_app):
    at = page_app.run()
    assert any("No flights yet" in info.value for info in at.info)


def test_flight_log_no_longer_offers_flight_creation(page_app):
    """REPLACES test_add_flight_via_form_succeeds_and_shows_in_log and
    test_add_flight_missing_origin_shows_error (2026-08-21). Both moved
    to tests/test_control_room_page.py — same coverage, different page.

    This page and Control Room carried identical seven-field add-flight
    forms, so "where do I add a flight?" had two answers. Creation now
    belongs to Control Room (where a controller ACTS); this page is the
    record (what happened, and where actuals are entered).

    Pinned as an absence, the way test_single_crew_path_is_gone pins
    the removed single-crew path — a duplicate form is the kind of
    thing that gets helpfully re-added."""
    at = page_app.run()

    assert not at.exception
    assert not any(b.label == "Add flight" for b in at.button), (
        "flight creation belongs to Control Room, not here"
    )
    assert not any("Add flight" in h.value for h in at.subheader)


def test_cancel_flight_via_form_marks_cancelled_and_stays_visible(page_app):
    # Seed one flight directly through the service layer first —
    # this test is about the cancel action, not re-testing add.
    from services import flight_service
    flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 0),
        "arr_time_planned": dt.datetime(2026, 7, 20, 7, 0),
        "domestic": True,
    })

    at = page_app.run()
    # Label-based, not positional. This filled at.text_input[4] and
    # clicked at.button[2] until 2026-08-21, when removing the
    # add-flight section shifted every index on the page — the same
    # breakage the Control Room pair tests hit in the same restructure.
    # Indices describe a layout; labels describe intent.
    _by_label(at.text_input, "Cancellation reason (if cancelling)").input("Aircraft AOG")
    at = _click(at, "Cancel this flight")   # _click runs the script; capture it

    assert not at.exception
    assert any("Cancelled flight" in s.value for s in at.success)

    at = at.run()
    df = at.dataframe[0].value
    # Permanent log requirement, verified at the page level too:
    # the cancelled flight must still be visible, not disappear.
    assert "CANCELLED" in list(df["status"])


# ------------------------------------------------------------------
# Aircraft field (2026-08-21) — DB-free
# ------------------------------------------------------------------
#
# Aircraft was settable only at creation until this change: it is in
# flight_service.UPDATABLE_FIELDS but no page exposed it afterwards, so
# a flight recorded without one could never be corrected.

import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from tests.conftest import page_path  # noqa: E402

_FLIGHT_COLUMNS = ["flight_id", "flight_no", "origin", "destination", "aircraft",
                   "dep_time_planned", "arr_time_planned", "status", "domestic",
                   "other_occupants_operating", "other_occupants_non_operating"]


def _fake_flight(aircraft=None, status="PLANNED", dep_actual=None, arr_actual=None):
    return {
        "flight_id": 1, "flight_no": "EPE 786", "origin": "KHI", "destination": "LHE",
        "aircraft": aircraft,
        "dep_time_planned": dt.datetime(2026, 8, 20, 19, 0),
        "arr_time_planned": dt.datetime(2026, 8, 20, 20, 45),
        "dep_time_actual": dep_actual, "arr_time_actual": arr_actual,
        "status": status, "domestic": True,
        "other_occupants_operating": None, "other_occupants_non_operating": None,
    }


def _render_with_flight(monkeypatch, aircraft=None, status="PLANNED",
                        dep_actual=None, arr_actual=None):
    """Renders the page against one faked flight. Returns
    (AppTest, calls) where calls records what reached the service."""
    from services import flight_service as fs
    row = _fake_flight(aircraft, status, dep_actual, arr_actual)
    calls = {"update": [], "disrupt": [], "clear": []}
    monkeypatch.setattr(fs, "get_all_flights",
                        lambda **kw: pd.DataFrame([row], columns=list(row)))
    monkeypatch.setattr(fs, "get_flight", lambda fid: row)
    monkeypatch.setattr(fs, "update_flight",
                        lambda fid, updates, app_user=None: calls["update"].append(updates))
    monkeypatch.setattr(fs, "set_flight_disrupted",
                        lambda fid, reason, app_user=None: calls["disrupt"].append(reason))
    monkeypatch.setattr(fs, "clear_flight_disruption",
                        lambda fid, reason, app_user=None: (
                            calls["clear"].append(reason),
                            "OPERATED" if (dep_actual and arr_actual) else "PLANNED")[1])

    at = AppTest.from_file(str(page_path("pages/3_Flight_Log.py")))
    at.session_state["app_user"] = "occ1"
    at.run()
    return at, calls


def _aircraft_field(at):
    return _by_label(at.text_input, "Aircraft")


def test_aircraft_defaults_only_when_the_flight_has_none(monkeypatch):
    """Backfills the flights recorded before aircraft was editable."""
    at, _ = _render_with_flight(monkeypatch, aircraft=None)

    assert not at.exception
    assert _aircraft_field(at).value == "AP-BNW"


def test_aircraft_never_overwrites_a_value_already_set(monkeypatch):
    """The default fills EMPTY only. A flight recorded against a
    different airframe — a lease, a substitution — must keep it, and
    must not be quietly reassigned to the fleet default on the next
    unrelated edit."""
    at, _ = _render_with_flight(monkeypatch, aircraft="AP-XYZ")

    assert not at.exception
    assert _aircraft_field(at).value == "AP-XYZ"


def test_editing_aircraft_writes_it(monkeypatch):
    at, calls = _render_with_flight(monkeypatch, aircraft=None)

    _aircraft_field(at).input("AP-ABC")
    at = _click(at, "Save changes")

    assert not at.exception
    assert calls["update"] and calls["update"][-1]["aircraft"] == "AP-ABC"


# ------------------------------------------------------------------
# Status transitions (2026-08-21) — DB-free
# ------------------------------------------------------------------
#
# status could ONLY ever become CANCELLED before this: cancel_flight()
# was its sole writer, so a flight that flew stayed PLANNED forever and
# DISRUPTED was unreachable, while the filter offered all four states.

_DEP_ACTUAL = dt.datetime(2026, 8, 20, 19, 12)
_ARR_ACTUAL = dt.datetime(2026, 8, 20, 20, 58)


def _button_labels(at):
    return [b.label for b in at.button]


def test_planned_flight_offers_only_the_disrupt_control(monkeypatch):
    at, _ = _render_with_flight(monkeypatch, status="PLANNED")

    assert not at.exception
    assert "Mark DISRUPTED" in _button_labels(at)
    assert not any(b.startswith("Clear DISRUPTED") for b in _button_labels(at))


def test_disrupted_flight_that_flew_offers_clearing_to_OPERATED(monkeypatch):
    """The control names its OUTCOME. Removing the label from a flight
    with both actual times yields OPERATED, because the flight flew —
    offering "PLANNED" here would be a control that says one thing and
    does another, since the automatic rule would move it anyway."""
    at, _ = _render_with_flight(monkeypatch, status="DISRUPTED",
                                dep_actual=_DEP_ACTUAL, arr_actual=_ARR_ACTUAL)

    assert "Clear DISRUPTED → OPERATED" in _button_labels(at)
    assert "Clear DISRUPTED → PLANNED" not in _button_labels(at)


def test_disrupted_flight_that_has_not_flown_offers_clearing_to_PLANNED(monkeypatch):
    at, _ = _render_with_flight(monkeypatch, status="DISRUPTED")

    assert "Clear DISRUPTED → PLANNED" in _button_labels(at)
    assert "Clear DISRUPTED → OPERATED" not in _button_labels(at)


def test_operated_and_cancelled_flights_offer_no_manual_status_change(monkeypatch):
    """Both are terminal — a flight that flew is not relabelled, and
    cancellation is a deliberate act."""
    for status in ("OPERATED", "CANCELLED"):
        at, _ = _render_with_flight(monkeypatch, status=status,
                                    dep_actual=_DEP_ACTUAL, arr_actual=_ARR_ACTUAL)
        labels = _button_labels(at)
        assert "Mark DISRUPTED" not in labels, status
        assert not any(b.startswith("Clear DISRUPTED") for b in labels), status
        assert any("is final" in c.value for c in at.caption), status


def test_marking_disrupted_passes_the_reason_through(monkeypatch):
    """A disruption nobody can explain later is the case an auditor asks
    about — the reason reaches the service, which requires it."""
    at, calls = _render_with_flight(monkeypatch, status="PLANNED")

    _by_label(at.text_input, "Disruption reason (required to mark DISRUPTED)").input("Bird strike")
    at = _click(at, "Mark DISRUPTED")

    assert not at.exception
    assert calls["disrupt"] == ["Bird strike"]


def test_clearing_a_disruption_also_carries_a_reason(monkeypatch):
    """The undo is audited like the forward transition. Without it the
    record shows a flight that was never disrupted, when it was labelled
    and then relabelled."""
    at, calls = _render_with_flight(monkeypatch, status="DISRUPTED")

    _by_label(at.text_input,
              "Reason for clearing the DISRUPTED label (required)").input("Labelled in error")
    at = _click(at, "Clear DISRUPTED → PLANNED")

    assert not at.exception
    assert calls["clear"] == ["Labelled in error"]
