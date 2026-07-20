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
from streamlit.testing.v1 import AppTest

from db.db import get_engine


@pytest.fixture
def page_app(migrated_db, monkeypatch):
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", test_url)
    get_engine.cache_clear()

    at = AppTest.from_file("pages/3_Flight_Log.py")
    yield at

    get_engine.cache_clear()


def test_page_loads_without_exception(page_app):
    at = page_app.run()
    assert not at.exception


def test_empty_flight_log_shows_info_message(page_app):
    at = page_app.run()
    assert any("No flights yet" in info.value for info in at.info)


def test_add_flight_via_form_succeeds_and_shows_in_log(page_app):
    at = page_app.run()

    at.text_input[1].input("KHI")   # Origin *
    at.text_input[2].input("LHE")   # Destination *
    at.date_input[0].set_value(dt.date(2026, 7, 20))   # Departure date
    at.time_input[0].set_value(dt.time(5, 0))           # Departure time
    at.date_input[1].set_value(dt.date(2026, 7, 20))   # Arrival date
    at.time_input[1].set_value(dt.time(7, 0))           # Arrival time
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("Added flight" in s.value for s in at.success)

    at = at.run()
    df = at.dataframe[0].value
    assert "KHI" in list(df["origin"])
    assert "LHE" in list(df["destination"])


def test_add_flight_missing_origin_shows_error(page_app):
    at = page_app.run()

    # Destination filled, origin left blank
    at.text_input[2].input("LHE")
    at.date_input[0].set_value(dt.date(2026, 7, 20))
    at.time_input[0].set_value(dt.time(5, 0))
    at.date_input[1].set_value(dt.date(2026, 7, 20))
    at.time_input[1].set_value(dt.time(7, 0))
    at.button[0].click()
    at = at.run()

    assert any("Missing required flight field" in e.value for e in at.error)


def test_cancel_flight_via_form_marks_cancelled_and_stays_visible(page_app):
    # Seed one flight directly through the service layer first —
    # this test is about the cancel action, not re-testing add.
    from services import flight_service
    flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 0),
        "arr_time_planned": dt.datetime(2026, 7, 20, 7, 0),
    })

    at = page_app.run()
    at.text_input[4].input("Aircraft AOG")  # Cancellation reason
    at.button[2].click()                     # Cancel this flight
    at = at.run()

    assert not at.exception
    assert any("Cancelled flight" in s.value for s in at.success)

    at = at.run()
    df = at.dataframe[0].value
    # Permanent log requirement, verified at the page level too:
    # the cancelled flight must still be visible, not disappear.
    assert "CANCELLED" in list(df["status"])
