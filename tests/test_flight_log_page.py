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
    _click(at, "Cancel this flight")
    at = at.run()

    assert not at.exception
    assert any("Cancelled flight" in s.value for s in at.success)

    at = at.run()
    df = at.dataframe[0].value
    # Permanent log requirement, verified at the page level too:
    # the cancelled flight must still be visible, not disappear.
    assert "CANCELLED" in list(df["status"])
