"""Does the "Select flight" actuals selector really list every flight?

Reported from the live trial: it "isn't listing all flights". Reading
the code says otherwise — no date filter, no default status filter, no
LIMIT, and the selector is fed the same DataFrame the table above it
counts. But "I read the code and it looks fine" is not an answer to
"I used it and it wasn't there", so this measures it instead.

DB-FREE, at production's real size (103 flights, 20 Aug - 21 Sep, all
rotation-linked). If the count matches, the report is about a 103-item
dropdown being hard to search rather than about rows being excluded,
and the fix is scoping rather than a filter to remove.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from services import flight_service
from services.display_labels import flight_label
from tests.conftest import authed_app_test

# Production shape as of 2026-08-31, so the measurement is taken at the
# size the operator actually sees rather than a toy one.
FLIGHT_COUNT = 103
_FIRST_DEP = dt.datetime(2026, 8, 20, 1, 45)

FLIGHT_COLUMNS = [
    "flight_id", "flight_no", "origin", "destination",
    "dep_time_planned", "arr_time_planned", "dep_time_actual", "arr_time_actual",
    "status", "domestic", "aircraft", "other_occupants_operating",
    "other_occupants_non_operating", "remarks", "rotation_instance_id",
]


# Two rotations, five legs a day between them — production's actual
# shape, so each flight number appears once per day and the labels are
# distinguishable. A fixture flying the same number three times a day
# would produce duplicate labels and measure a problem production does
# not have.
_LEGS = [("EPE 786", "LHE", "KHI"), ("EPE 787", "KHI", "LHE"),
         ("EPE 802", "KHI", "DXB"), ("EPE 804", "DXB", "BAH"),
         ("EPE 805", "BAH", "KHI")]


def _flights(n=FLIGHT_COUNT):
    rows = []
    for i in range(n):
        flight_no, origin, destination = _LEGS[i % len(_LEGS)]
        dep = (_FIRST_DEP + dt.timedelta(days=i // len(_LEGS))
               + dt.timedelta(hours=3 * (i % len(_LEGS))))
        rows.append({
            "flight_id": i + 1,
            "flight_no": flight_no,
            "origin": origin,
            "destination": destination,
            "dep_time_planned": dep,
            "arr_time_planned": dep + dt.timedelta(hours=2),
            "dep_time_actual": None, "arr_time_actual": None,
            "status": "PLANNED", "domestic": True, "aircraft": "AP-BNW",
            "other_occupants_operating": "", "other_occupants_non_operating": "",
            "remarks": "", "rotation_instance_id": i + 1,
        })
    # The page sorts CHRONOLOGICALLY in SQL (ORDER BY dep_time_planned
    # ASC, changed from DESC 2026-09-05); the fake read has to
    # reproduce that, not the insertion order, or the test measures a
    # list the page never receives.
    return pd.DataFrame(rows, columns=FLIGHT_COLUMNS).sort_values(
        "dep_time_planned", ascending=True).reset_index(drop=True)


@pytest.fixture
def flt_schedule(monkeypatch):
    frame = _flights()
    monkeypatch.setattr(flight_service, "get_all_flights",
                        lambda **kwargs: frame.copy())
    monkeypatch.setattr(
        flight_service, "get_flight",
        lambda fid: frame[frame["flight_id"] == fid].iloc[0])
    return frame


def _selector(at):
    matching = [s for s in at.selectbox if s.label == "Select flight"]
    assert len(matching) == 1, [s.label for s in at.selectbox]
    return matching[0]


def _widen(at, frame):
    """Open the date window wide enough to include every fixture flight,
    so the selector is measured against the whole table."""
    first = frame["dep_time_planned"].min().date() - dt.timedelta(days=1)
    last = frame["dep_time_planned"].max().date() + dt.timedelta(days=1)
    at.date_input(key="actuals_from").set_value(first).run()
    at.date_input(key="actuals_to").set_value(last).run()
    return at


def test_the_actuals_selector_offers_every_flight_in_its_window(flt_schedule):
    """THE measurement, and the one that settled the diagnosis.

    Reported as "not listing all flights". With the window opened to
    cover the whole table, the selector offers every row it holds — no
    filter, no cap, nothing excluded. That is what made the report a
    findability problem rather than a missing-rows problem, and it is
    why the fix is a date window rather than a filter to remove.

    If this ever fails, the diagnosis was wrong and the cause is a
    filter or a cap after all."""
    at = _widen(authed_app_test("pages/3_Flight_Log.py").run(), flt_schedule)
    assert not at.exception, at.exception

    # AppTest reports a selectbox's options as the FORMATTED labels, not
    # the underlying values, so the comparison is against labels built
    # the same way the page builds them.
    options = list(_selector(at).options)
    assert len(options) == FLIGHT_COUNT, (
        f"the selector offers {len(options)} of {FLIGHT_COUNT} flights with "
        f"the window fully open — rows ARE being excluded, so the cause is "
        f"a filter or a cap and not the size of the dropdown"
    )
    expected = [flight_label(row, include_route=True)
                for _, row in flt_schedule.iterrows()]
    assert options == expected, (
        "the selector's options are not the table's flights, in the "
        "table's order"
    )


def test_both_ends_of_the_list_are_reachable(flt_schedule):
    """A row cap takes one end or the other, so BOTH ends must be
    present — that is the specific way "not listing all flights" would
    have been literally true.

    Written when ordering was newest-first and a cap would have taken
    the tail. Ordering is chronological now (2026-09-05), which moves
    which end a cap would eat — so the test asserts both ends by
    POSITION rather than by direction, and does not have to be rewritten
    the next time the sort changes."""
    at = _widen(authed_app_test("pages/3_Flight_Log.py").run(), flt_schedule)
    options = list(_selector(at).options)

    last = flight_label(flt_schedule.iloc[-1], include_route=True)
    first = flight_label(flt_schedule.iloc[0], include_route=True)
    assert first in options
    assert last in options, (
        "one end of the list is missing — consistent with a row cap"
    )


def test_the_list_runs_earliest_first(flt_schedule):
    """The PAGE does not re-order what the service hands it.

    Deliberately NOT a test of the ORDER BY: the fake read is already
    sorted, so this would keep passing if the SQL clause were deleted.
    What it catches is a page-level sort or a dict round-trip quietly
    rearranging the options between the query and the selectbox —
    which is the failure that would make Flt Schedule disagree with the
    pair form on Roster, where duties are built in departure order.

    The direction itself is measured in test_flight_service.py, against
    a real database, because that is where ORDER BY actually runs."""
    at = _widen(authed_app_test("pages/3_Flight_Log.py").run(), flt_schedule)
    options = list(_selector(at).options)

    chronological = [flight_label(row, include_route=True)
                     for _, row in flt_schedule.sort_values(
                         "dep_time_planned").iterrows()]
    assert options == chronological, (
        "the selector is not in departure order"
    )


def test_the_window_hides_nothing_by_default(flt_schedule):
    """A window defaulting to "the last week or so" reads as helpful and
    is the same mistake in a new place. Recording actuals is what you do
    about a flight that ALREADY operated, sometimes weeks late, so a
    controller chasing a missing actual from three weeks ago would meet
    "No flights in this date range" — which looks like the flight is
    gone, and is worse than a long dropdown.

    Narrowing stays available; it is just never the default."""
    at = authed_app_test("pages/3_Flight_Log.py").run()
    assert not at.exception, at.exception

    assert len(list(_selector(at).options)) == FLIGHT_COUNT, (
        "the page opens already hiding flights"
    )
    assert not any("outside" in c.value for c in at.caption), (
        "nothing should be reported as hidden on first render"
    )


def test_flights_outside_the_window_are_counted_not_silently_dropped(flt_schedule):
    """Narrowing a view to hide work looks like tidying and isn't. The
    window is a convenience, so it has to say what it is leaving out —
    otherwise "not listing all flights" becomes true, and this time by
    design."""
    at = authed_app_test("pages/3_Flight_Log.py").run()
    at.date_input(key="actuals_from").set_value(dt.date(2026, 8, 25)).run()
    at.date_input(key="actuals_to").set_value(dt.date(2026, 8, 26)).run()

    shown = len(list(_selector(at).options))
    assert 0 < shown < FLIGHT_COUNT, shown
    hidden = FLIGHT_COUNT - shown
    assert any(f"{shown} of {FLIGHT_COUNT} flights shown" in c.value
               for c in at.caption), [c.value for c in at.caption]
    assert any(str(hidden) in c.value and "outside" in c.value
               for c in at.caption)


def test_an_empty_window_says_so_rather_than_showing_an_empty_selector(flt_schedule):
    """A window with nothing in it must explain itself; an empty
    dropdown is the same dead end as a disabled button."""
    at = authed_app_test("pages/3_Flight_Log.py").run()
    at.date_input(key="actuals_from").set_value(dt.date(2027, 1, 1)).run()
    at.date_input(key="actuals_to").set_value(dt.date(2027, 1, 2)).run()

    assert not at.exception, at.exception
    assert any("No flights in this date range" in i.value for i in at.info)


def test_the_status_filter_is_the_only_thing_that_narrows_the_list(flt_schedule):
    """The one filter that DOES exist defaults to All. If that default
    ever changes, the selector silently starts hiding flights and this
    is the test that says so."""
    at = authed_app_test("pages/3_Flight_Log.py").run()
    status = [s for s in at.selectbox if s.label == "Filter by status"]
    assert len(status) == 1
    assert status[0].value == "All", (
        f"the status filter defaults to {status[0].value!r}, so the page "
        f"opens already hiding flights"
    )
