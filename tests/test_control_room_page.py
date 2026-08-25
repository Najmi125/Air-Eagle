"""
tests/test_control_room_page.py

Rebuilt for the flight-deck crew package (2026-08-13): Commander/Second
Pilot selectboxes live OUTSIDE the form (read before submission, same
reasoning as every other live-updating control in this codebase).

Updated 2026-08-20. The "Crew type" radio and the single-crew
(LM/ENGR/Other) path are GONE — there is no such thing as a flight
operated by one crew member, and the one combination that still worked
created a flight with no flight deck. Crew is now OPTIONAL instead: an
"Assign a flight-deck pair now" checkbox, ticked by default, gates the
pair selectors. Unticked, only the flight is created.

The tests below split into two groups. The DB-backed ones at the top
exercise the legality gate and need real Postgres. The DB-free ones at
the bottom fake the service layer — added because every operational
finding on this page so far was found on a live deployment rather than
by this suite, which skips entirely wherever Postgres is absent.
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

    at = authed_app_test("pages/1_Control_Room.py")
    yield at

    get_engine.cache_clear()


_FAR_FUTURE = dt.date(2099, 1, 1)
_QUAL_DEFAULTS = {
    "license_expiry": _FAR_FUTURE, "medical_expiry": _FAR_FUTURE,
    "sim_expiry": _FAR_FUTURE, "route_check_expiry": _FAR_FUTURE,
    "ir_expiry": _FAR_FUTURE, "sep_expiry": _FAR_FUTURE,
    "crm_expiry": _FAR_FUTURE, "dg_expiry": _FAR_FUTURE,
}


def _seed_crew(role="LM", **overrides):
    from services import crew_service
    # All qualification expiry fields set far in the future so these
    # page tests exercise page/FDP/rest mechanics, not the
    # qualification gate (2026-07-31) — that gate has its own
    # dedicated tests in test_assignment_service.py.
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUAL_DEFAULTS)
    crew_data.update(overrides)
    from services import crew_service as cs
    return cs.add_crew(crew_data)


def _seed_pair(commander_dob=dt.date(1980, 1, 1), second_pilot_dob=dt.date(1985, 1, 1)):
    cpt = _seed_crew("CPT", date_of_birth=commander_dob)
    fo = _seed_crew("FO", date_of_birth=second_pilot_dob)
    return cpt, fo


def test_page_loads_without_exception(page_app):
    _seed_pair()
    at = page_app.run()
    assert not at.exception


def test_no_active_crew_is_informational_not_fatal(page_app):
    """Was a warning + st.stop() until 2026-08-20. An empty crew list is
    the state a fresh deployment is in, and a flight with crew TBC must
    still be recordable then — so it is now an st.info and the form
    still renders."""
    at = page_app.run()
    assert not at.exception
    assert any("No active crew" in i.value for i in at.info)
    assert any(b.label == "Check legality and save" for b in at.button)


def test_no_eligible_pair_shows_warning(page_app):
    """Only an LM on file — no CPT/FO at all — the pair path (the
    default crew type) must warn rather than error, since there's no
    eligible Commander or Second Pilot pool to build a form from."""
    _seed_crew("LM")
    at = page_app.run()
    assert not at.exception
    assert any("active CPT" in w.value for w in at.warning)


def test_legal_pair_assignment_saves_flight_and_shows_success(page_app):
    cpt, fo = _seed_pair()
    at = page_app.run()

    # Pair is the default crew type — no radio interaction needed.
    # Every lookup here is by LABEL, not position. The 2026-08-21
    # restructure shifted text_input, time_input, date_input and button
    # indices on this page all at once, and positional versions of these
    # three tests broke in a file that had not been touched.
    _select_pair(at, cpt, fo)
    _fill_flight_form(at, dep="0545", arr="0745")
    _set_flight_dates(at, dt.date(2026, 7, 20))
    at = _click_save(at)

    assert not at.exception
    assert any("ALLOWED" in s.value for s in at.success)

    from services import flight_service, assignment_service
    assert len(flight_service.get_all_flights()) == 1
    assert len(assignment_service.get_roster_for_crew(cpt)) == 1
    assert len(assignment_service.get_roster_for_crew(fo)) == 1


def test_illegal_pair_assignment_shows_rejection_and_saves_nothing(page_app):
    cpt, fo = _seed_pair()

    # Seed a prior 8h-FDP duty for the Commander directly — an 8h duty
    # now correctly triggers NEEDS_MANUAL_REVIEW through the real
    # assign_pair_to_new_flights() call (no meal/snack data
    # populated, tested separately), so it's seeded here as GIVEN
    # history instead, same pattern as test_assignment_service.py's
    # own _seed_duty helper.
    from services import flight_service
    from sqlalchemy import text
    prior_flight = flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 7, 20, 5, 0),
        "arr_time_planned": dt.datetime(2026, 7, 20, 12, 0),
        "domestic": True,
    })
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                report_time, debrief_time, fdp_hours, role_assigned)
            VALUES (:crew_id, :flight_id, 'SEEDED-PRIOR', :duty_date,
                :report_time, :debrief_time, :fdp_hours, :role_assigned)
        """), {
            "crew_id": cpt, "flight_id": prior_flight,
            "duty_date": dt.date(2026, 7, 20),
            "report_time": dt.datetime(2026, 7, 20, 4, 15),
            "debrief_time": dt.datetime(2026, 7, 20, 12, 15),
            "fdp_hours": 8.0, "role_assigned": "CPT",
        })

    at = page_app.run()
    _select_pair(at, cpt, fo)
    _fill_flight_form(at, dep="1745", arr="1945")   # only 5h after prior debrief (12:15)
    _set_flight_dates(at, dt.date(2026, 7, 20))
    at = _click_save(at)

    assert not at.exception
    assert any("REJECTED" in e.value for e in at.error)

    # Only the ONE prior (legal) flight should exist — the illegal
    # ad-hoc attempt must not have saved a second one, for either seat.
    assert len(flight_service.get_all_flights()) == 1


def test_needs_review_pair_assignment_shows_warning_not_success(page_app):
    """Regression guard for a real bug already fixed once on this
    page (pre-pair-model): a held assignment must never fall into the
    ALLOWED branch, which references flight_ids[0] — genuinely empty
    for a held assignment.

    Trigger: a missing qualification-expiry field on the Commander
    (AE-CREW-QUAL-001_LICENSE_EXPIRY_MISSING)."""
    from services import crew_service
    cpt, fo = _seed_pair()
    crew_service.update_crew(cpt, {"license_expiry": None})

    at = page_app.run()
    _select_pair(at, cpt, fo)
    _fill_flight_form(at, dep="0500", arr="0700")
    _set_flight_dates(at, dt.date(2026, 7, 20))
    at = _click_save(at)

    assert not at.exception
    assert any("HELD FOR MANUAL REVIEW" in w.value for w in at.warning)
    assert not any("ALLOWED" in s.value for s in at.success)

    from services import flight_service as fs
    assert len(fs.get_all_flights()) == 0  # nothing saved


def test_single_crew_path_is_gone(page_app):
    """REPLACES test_single_crew_path_still_works_for_lm (2026-08-20).

    That test asserted the LM/ENGR/Other single-crew path worked. It is
    deliberately removed: there is no such thing as a flight operated by
    one crew member. The path predated the pair model, and its one live
    combination — an "Other" crew member assigned role "Other" — created
    a flight with no flight deck at all.

    Kept as an assertion of the new rule rather than deleted, so the
    absence is pinned and can't quietly come back."""
    _seed_crew("LM")
    at = page_app.run()

    assert not at.exception
    assert not any("Crew type" in r.label for r in at.radio), (
        "the Crew type radio should be gone — there is no single-crew mode"
    )
    assert not any("Role" in s.label for s in at.selectbox), (
        "a bare Role selectbox means the single-crew path is still reachable"
    )


# ------------------------------------------------------------------
# Crew-optional and single-crew removal (2026-08-20) — DB-free
# ------------------------------------------------------------------
#
# No database: crew_service/flight_service are faked, so these run
# everywhere. Deliberate — every other test of this page is DB-gated,
# and the operational findings that produced these changes were all
# found on a live deployment rather than by the suite.

import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from tests.conftest import page_path  # noqa: E402

_CREW_COLUMNS = ["crew_id", "name", "role", "is_active"]
_PAIR_CREW = pd.DataFrame([
    {"crew_id": "CPT-01", "name": "Alpha", "role": "CPT", "is_active": True},
    {"crew_id": "FO-01", "name": "Bravo", "role": "FO", "is_active": True},
], columns=_CREW_COLUMNS)


@pytest.fixture
def faked_services(monkeypatch):
    """crew list + a capturing add_flight. Returns the list of flights
    written, so a test can assert exactly what reached the service.

    test_connection is patched to report the database DOWN, which makes
    section 1 (the operational status board) skip itself entirely.
    These tests are about sections 2 and 3; without this they would
    attempt real connections for the board and sit in retries until
    AppTest times out. The board has its own fixture, `status_board`.
    """
    import db.db as db
    from services import crew_service as cs
    from services import flight_service as fs

    monkeypatch.setattr(db, "test_connection", lambda: "no database (test)")

    written = []
    monkeypatch.setattr(cs, "get_all_crew", lambda active_only=True: _PAIR_CREW.copy())
    monkeypatch.setattr(fs, "add_flight",
                        lambda data, app_user=None: (written.append(data), 4242)[1])
    return written


def _render(assign_pair=True):
    at = AppTest.from_file(str(page_path("pages/1_Control_Room.py")))
    at.session_state["app_user"] = "occ1"
    at.session_state["control_room_assign_pair"] = assign_pair
    at.run()
    return at


def _fill_flight(at, operating="", non_operating=""):
    """Exact-prefix matching, NOT substring: the non-operating label
    contains the operating one ("...non-operating (aboard" contains
    "operating (aboard"), so a substring match fills both fields with
    the same value."""
    for t in at.text_input:
        if t.label.startswith("Origin"):
            t.input("KHI")
        elif t.label.startswith("Destination"):
            t.input("LHE")
        elif t.label.startswith("Other occupants — non-operating"):
            t.input(non_operating)
        elif t.label.startswith("Other occupants — operating"):
            t.input(operating)


def test_no_single_crew_controls_in_any_mode(faked_services):
    """Item A: the single-crew path must be unreachable regardless of
    the crew-optional toggle."""
    for assign_pair in (True, False):
        at = _render(assign_pair=assign_pair)
        assert not at.exception
        assert not any("Crew type" in r.label for r in at.radio)
        assert not any("Role" in s.label for s in at.selectbox), (
            f"Role selectbox present with assign_pair={assign_pair}"
        )


def test_pair_selectors_render_when_assigning_and_vanish_when_not(faked_services):
    """Item C: the pair controls are the thing being made optional."""
    labels = [s.label for s in _render(assign_pair=True).selectbox]
    assert any(l.startswith("Commander") for l in labels)
    assert any(l.startswith("Second Pilot") for l in labels)

    assert _render(assign_pair=False).selectbox == []


def test_flight_saves_with_no_crew_when_pair_is_not_assigned(faked_services):
    """The whole point of item C: 'charter confirmed, crew TBC' now has
    a path that doesn't go through Flight Log."""
    at = _render(assign_pair=False)
    _fill_flight(at, operating="Abdulghani (LM), 2x AME")
    [b for b in at.button if "Check legality" in b.label][0].click()
    at = at.run()

    assert not at.exception
    assert any("no crew assigned" in s.value for s in at.success)
    assert len(faked_services) == 1
    assert faked_services[0]["origin"] == "KHI"


def test_occupant_fields_are_recorded_and_independent(faked_services):
    """Item B: both columns have existed since migrations/010 and
    roster_coverage() has always displayed them, but nothing wrote
    them. Asserted independently because the two labels overlap."""
    at = _render(assign_pair=False)
    _fill_flight(at, operating="OPERATING-VALUE", non_operating="NONOP-VALUE")
    [b for b in at.button if "Check legality" in b.label][0].click()
    at = at.run()

    written = faked_services[0]
    assert written["other_occupants_operating"] == "OPERATING-VALUE"
    assert written["other_occupants_non_operating"] == "NONOP-VALUE"


def test_page_still_renders_with_no_crew_on_file(monkeypatch):
    """Previously st.stop()'d on an empty crew list, which is the state
    a fresh deployment is in. A flight with crew TBC must still be
    recordable then — that is when it is most likely."""
    import db.db as db
    from services import crew_service as cs
    # Section 1 skipped: see faked_services' docstring.
    monkeypatch.setattr(db, "test_connection", lambda: "no database (test)")
    monkeypatch.setattr(cs, "get_all_crew",
                        lambda active_only=True: pd.DataFrame(columns=_CREW_COLUMNS))

    at = _render(assign_pair=True)

    assert not at.exception
    assert any(b.label == "Check legality and save" for b in at.button), (
        "the flight form must still be reachable with no crew on file"
    )


def test_requesting_a_pair_that_cannot_be_formed_saves_nothing(monkeypatch):
    """Must not silently downgrade to an uncrewed flight: the
    controller asked for crew. Refuse, and say how to proceed."""
    import db.db as db
    from services import crew_service as cs
    from services import flight_service as fs

    written = []
    monkeypatch.setattr(db, "test_connection", lambda: "no database (test)")
    monkeypatch.setattr(cs, "get_all_crew",
                        lambda active_only=True: pd.DataFrame(columns=_CREW_COLUMNS))
    monkeypatch.setattr(fs, "add_flight",
                        lambda data, app_user=None: (written.append(data), 1)[1])

    at = _render(assign_pair=True)
    _fill_flight(at)
    [b for b in at.button if "Check legality" in b.label][0].click()
    at = at.run()

    assert not at.exception
    assert any("Nothing was saved" in e.value for e in at.error)
    assert written == [], "an uncrewed flight must not be created when a pair was requested"


# ------------------------------------------------------------------
# Add-flight coverage, MOVED here from test_flight_log_page.py
# (2026-08-21) — same coverage, different page
# ------------------------------------------------------------------
#
# Both pages carried identical seven-field forms until the restructure.
# Creation now belongs here (Control Room is where a controller ACTS);
# Flt Schedule is the record. test_flight_log_page.py pins the absence.
#
# Label-based, not index-based: the form gained "Flight No" and the
# times became HHMM text, so positional lookups from the old page would
# silently address the wrong widget.

def _select_pair(at, commander, second_pilot):
    """Crew selectors by label. They were at.selectbox[0]/[1] until
    2026-08-21 — correct only while section 1 rendered no selectbox of
    its own, which is not a property any of these tests assert."""
    _by_label(at.selectbox, "Commander (must be CPT) *").select(commander)
    _by_label(at.selectbox, "Second Pilot (CPT or FO) *").select(second_pilot)


def _set_flight_dates(at, day):
    _by_label(at.date_input, "Departure date *").set_value(day)
    _by_label(at.date_input, "Arrival date *").set_value(day)


def _by_label(elements, label):
    matches = [e for e in elements if e.label == label]
    assert len(matches) == 1, (
        f"expected exactly one element labeled {label!r}, found "
        f"{[e.label for e in elements]}"
    )
    return matches[0]


def _fill_flight_form(at, origin="KHI", destination="LHE",
                      dep="0500", arr="0700", flight_no=None):
    for t in at.text_input:
        if t.label.startswith("Flight No") and flight_no is not None:
            t.input(flight_no)
        elif t.label.startswith("Origin"):
            t.input(origin)
        elif t.label.startswith("Destination"):
            t.input(destination)
        elif t.label.startswith("Departure time"):
            t.input(dep)
        elif t.label.startswith("Arrival time"):
            t.input(arr)


def test_add_flight_without_crew_succeeds_and_appears_in_the_record(page_app):
    """MOVED from test_flight_log_page.py::
    test_add_flight_via_form_succeeds_and_shows_in_log."""
    from services import flight_service

    at = page_app.run()
    at.session_state["control_room_assign_pair"] = False
    at = at.run()

    _fill_flight_form(at, flight_no="EPE 786")
    at = _click_save(at)   # _click_save runs the script; capture it

    flights = flight_service.get_all_flights()
    assert len(flights) == 1
    assert flights.iloc[0]["origin"] == "KHI"
    assert flights.iloc[0]["flight_no"] == "EPE 786"


def test_add_flight_missing_origin_shows_error(page_app):
    """MOVED from test_flight_log_page.py. Origin/destination are
    required here, before anything reaches the service."""
    from services import flight_service

    at = page_app.run()
    at.session_state["control_room_assign_pair"] = False
    at = at.run()

    _fill_flight_form(at, origin="")
    at = _click_save(at)

    assert any("Origin and destination are required" in e.value for e in at.error)
    assert len(flight_service.get_all_flights()) == 0


def test_malformed_hhmm_is_rejected_and_saves_nothing(page_app):
    """The times became text in this restructure; a bad one must be
    named, not silently coerced."""
    from services import flight_service

    at = page_app.run()
    at.session_state["control_room_assign_pair"] = False
    at = at.run()

    _fill_flight_form(at, dep="2465")
    at = _click_save(at)

    assert any("2465" in e.value for e in at.error)
    assert len(flight_service.get_all_flights()) == 0


def _click_save(at):
    """Submit the add-flight form and return the resulting AppTest.

    RUNS THE SCRIPT — the return value is the new state and callers MUST
    capture it (`at = _click_save(at)`). Discarding it and calling
    at.run() separately reruns the STALE object and loses the submit:
    that shipped once on 2026-08-21 in the sibling _click helper, where
    it turned a loud IndexError into a quiet assertion failure.
    """
    [b for b in at.button if b.label == "Check legality and save"][0].click()
    return at.run()


# ------------------------------------------------------------------
# Section 1: operational status board (2026-08-21) — DB-free
# ------------------------------------------------------------------

_TODAY = dt.date.today()

_FLIGHTS_TODAY = pd.DataFrame([
    {"flight_id": 1, "flight_no": "EPE 786", "origin": "KHI", "destination": "LHE",
     "dep_time_planned": dt.datetime.combine(_TODAY, dt.time(19, 0)),
     "arr_time_planned": dt.datetime.combine(_TODAY, dt.time(20, 45)),
     "status": "PLANNED", "domestic": True},
    {"flight_id": 2, "flight_no": None, "origin": "LHE", "destination": "DWC",
     "dep_time_planned": dt.datetime.combine(_TODAY, dt.time(22, 0)),
     "arr_time_planned": dt.datetime.combine(_TODAY, dt.time(23, 45)),
     "status": "PLANNED", "domestic": False},
])

# Flight 1 fully crewed; flight 2 has nothing. Note the Commander of
# flight 1 is a CPT in the COMMANDER seat and the Second Pilot is ALSO
# a CPT — legitimate under the pair model, and the reason seat coverage
# must be read from operating_position rather than role_assigned.
_ROSTER_TODAY = pd.DataFrame([
    {"flight_id": 1, "crew_id": "CPT-01", "operating_position": "COMMANDER",
     "role_assigned": "CPT", "status": "CONFIRMED"},
    {"flight_id": 1, "crew_id": "CPT-02", "operating_position": "SECOND_PILOT",
     "role_assigned": "CPT", "status": "CONFIRMED"},
])


@pytest.fixture
def status_board(monkeypatch):
    """Everything section 1 reads, faked. Returns a dict the test can
    mutate before rendering."""
    import db.db as db
    from services import assignment_service as asg
    from services import crew_service as cs
    from services import flight_service as fs
    from services import roster_generator_service as rgs

    state = {
        "connected": True,
        "flights": _FLIGHTS_TODAY.copy(),
        "roster": _ROSTER_TODAY.copy(),
        "uncovered": pd.DataFrame([{"x": 1}]),
        "expiry": {"expired": 2, "expiring": 1, "horizon_days": 7},
    }
    monkeypatch.setattr(db, "test_connection", lambda: state["connected"])
    monkeypatch.setattr(cs, "get_all_crew", lambda active_only=True: _PAIR_CREW.copy())
    monkeypatch.setattr(fs, "get_all_flights",
                        lambda **kw: state["flights"])
    monkeypatch.setattr(asg, "search_roster", lambda **kw: state["roster"])
    monkeypatch.setattr(rgs, "get_open_uncovered_seats", lambda a, b: state["uncovered"])
    monkeypatch.setattr(asg, "qualification_expiry_counts",
                        lambda *a, **k: state["expiry"])
    return state


def _board_rows(at):
    """The Today's flights dataframe, as a list of dicts."""
    assert at.dataframe, "no status board rendered"
    return at.dataframe[0].value.to_dict("records")


def test_status_board_reports_seats_from_operating_position(status_board):
    """The catch this section was nearly built on the wrong column.

    Flight 1's Second Pilot is a CPT by grade. Reading coverage from
    role_assigned would report the Commander seat filled twice and the
    Second Pilot seat empty. operating_position is the seat, and that
    grade-versus-position split is what the flight-deck crew package
    existed to establish."""
    at = _render()

    rows = _board_rows(at)
    crewed = next(r for r in rows if r["Flight"].startswith("EPE 786"))
    assert crewed["Commander"] == "CPT-01"
    assert crewed["Second Pilot"] == "CPT-02"


def test_status_board_shows_uncovered_for_an_empty_seat(status_board):
    """An ad-hoc flight saved with crew TBC — which this page now makes
    easy to create — must be visibly uncovered here, since the
    rotation-seat metric above deliberately does not count it."""
    at = _render()

    rows = _board_rows(at)
    uncrewed = next(r for r in rows if r["Flight"].startswith("#2"))
    assert uncrewed["Commander"] == "UNCOVERED"
    assert uncrewed["Second Pilot"] == "UNCOVERED"


def test_status_board_metrics_render(status_board):
    at = _render()
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Uncovered rotation seats today"] == "1"
    assert metrics["Crew with expired documents"] == "2"
    assert metrics["Crew with documents expiring in 7 days"] == "1"


def test_status_board_is_skipped_when_the_database_is_unreachable(status_board, monkeypatch):
    """Skipped, not merely wrapped. try/except catches a failing query
    but not a hanging one — this cost the home page three seconds to
    render before the same gate was added there (2026-08-20). Any query
    running here is a failure."""
    status_board["connected"] = "connection refused"

    from services import assignment_service as asg
    from services import flight_service as fs
    from services import roster_generator_service as rgs

    def must_not_run(*args, **kwargs):
        raise AssertionError("status board queried an unreachable database")

    monkeypatch.setattr(rgs, "get_open_uncovered_seats", must_not_run)
    monkeypatch.setattr(asg, "qualification_expiry_counts", must_not_run)
    monkeypatch.setattr(fs, "get_all_flights", must_not_run)
    monkeypatch.setattr(asg, "search_roster", must_not_run)

    at = _render()

    assert not at.exception
    assert any("Operational status unavailable" in c.value for c in at.caption)
    # The page's real job must survive.
    assert any(b.label == "Check legality and save" for b in at.button)


def test_status_board_survives_one_query_failing_on_its_own(status_board, monkeypatch):
    """Connection up, one query broken. The rest of the board, and the
    add-flight form, must still render."""
    from services import flight_service as fs
    monkeypatch.setattr(fs, "get_all_flights",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    at = _render()

    assert not at.exception
    assert any("Today's flights unavailable" in c.value for c in at.caption)
    assert {m.label for m in at.metric} >= {"Uncovered rotation seats today"}
    assert any(b.label == "Check legality and save" for b in at.button)


def test_aircraft_prefills_the_fleet_registration(faked_services):
    """Air Eagle operates one B737, so a controller should not retype
    AP-BNW on every charter. Asserted against flight_service's constant
    rather than the literal, so the registration lives in exactly one
    place — and so a second aircraft (which turns this from a default
    into a selector) breaks the page and this test together rather than
    silently attributing flights to the wrong airframe."""
    from services import flight_service

    at = _render(assign_pair=False)

    assert not at.exception
    assert _by_label(at.text_input, "Aircraft").value == flight_service.AIRCRAFT_DEFAULT
