"""
tests/test_assistant_page.py

AppTest coverage for pages/5_Assistant.py, same pattern as
test_roster_page.py/test_control_room_page.py — real crew/flight/
roster data seeded via the actual services, not synthetic Dataset
objects. Dates are computed relative to the real "today" the test
actually runs on (the page has no injectable `today`, unlike
query_parser.parse() itself, which defaults to dt.date.today()) —
"yesterday" rather than a hardcoded date, so these tests don't go
stale.
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

    at = authed_app_test("pages/5_Assistant.py")
    yield at

    get_engine.cache_clear()


_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY, "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY, "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY, "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY, "dg_expiry": _FAR_FUTURE_EXPIRY,
}


def _add_crew(name, role="CPT", **overrides):
    from services import crew_service
    crew_data = {"name": name, "role": role, "base": "KHI", "date_of_birth": dt.date(1980, 1, 1)}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _seed_duty_yesterday(crew_id):
    """A real duty, dated "yesterday" relative to whenever this test
    actually runs — a domestic, comfortably-legal 2h flight, no
    history to conflict with.

    Seeded directly via SQL rather than through assign_crew_to_duty()
    (2026-08-14, flight-deck crew package): these tests are about the
    OCC Assistant's reporting on one named crew member's duties, not
    about pairing, and a CPT can no longer be assigned solo through
    the real assignment API — assign_pair_to_duty() would need a
    disposable second pilot for a scenario that has nothing to do with
    pairing. Mirrors the same "seed given state via raw SQL" pattern
    tests/test_assignment_service.py's own _seed_duty() helper uses for
    exactly this reason."""
    import uuid
    from sqlalchemy import text
    from services import flight_service
    yesterday = dt.date.today() - dt.timedelta(days=1)
    flight_id = flight_service.add_flight({
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime.combine(yesterday, dt.time(5, 45)),
        "arr_time_planned": dt.datetime.combine(yesterday, dt.time(7, 45)),
        "domestic": True,
    })
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO roster (crew_id, flight_id, duty_id, duty_date,
                report_time, debrief_time, fdp_hours, role_assigned)
            VALUES (:crew_id, :flight_id, :duty_id, :duty_date,
                :report_time, :debrief_time, :fdp_hours, 'CPT')
        """), {
            "crew_id": crew_id, "flight_id": flight_id,
            "duty_id": f"SEEDED-{uuid.uuid4().hex[:8]}",
            "duty_date": yesterday,
            "report_time": dt.datetime.combine(yesterday, dt.time(5, 0)),
            "debrief_time": dt.datetime.combine(yesterday, dt.time(8, 0)),
            "fdp_hours": 3.0,
        })
    return flight_id


def test_page_loads_without_exception(page_app):
    at = page_app.run()
    assert not at.exception
    assert any("What did Waqar fly last week" in m.value for m in at.markdown)


def test_resolved_question_shows_interpretation_before_table(page_app):
    crew_id = _add_crew("MUHAMMAD WAQAR")
    _seed_duty_yesterday(crew_id)

    at = page_app.run()
    at.text_input[0].input(f"show {crew_id} duties yesterday")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("Understood as" in i.value and "MUHAMMAD WAQAR" in i.value for i in at.info)
    assert len(at.dataframe) == 1

    # Ordering is the actual requirement, not just presence.
    kinds = [type(el).__name__ for el in at.main]
    info_idx = next(i for i, k in enumerate(kinds) if k == "Info")
    table_idx = next(i for i, k in enumerate(kinds) if k == "Dataframe")
    assert info_idx < table_idx


def test_empty_result_still_shows_interpretation_before_no_matching_records(page_app):
    """Approved addition: an empty result is exactly when a controller
    most needs to see what was understood, since the likelier cause is
    a misparse than a genuinely empty period. Seed a real, unrelated
    crew member with zero duties -- the query resolves cleanly but
    finds nothing."""
    crew_id = _add_crew("MUHAMMAD SALEEM")

    at = page_app.run()
    at.text_input[0].input(f"show {crew_id} duties yesterday")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    info_values = [i.value for i in at.info]
    assert any("Understood as" in v for v in info_values)
    assert any("No matching records" in v for v in info_values)
    understood_idx = next(i for i, v in enumerate(info_values) if "Understood as" in v)
    empty_idx = next(i for i, v in enumerate(info_values) if "No matching records" in v)
    assert understood_idx < empty_idx
    assert len(at.dataframe) == 0


def test_interpretation_shows_unbounded_date_range_when_no_date_mentioned(page_app):
    crew_id = _add_crew("MUHAMMAD WAQAR")

    at = page_app.run()
    at.text_input[0].input(f"show {crew_id} duties")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("not specified" in i.value for i in at.info)


def test_editing_date_range_reruns_report_with_new_range(page_app):
    crew_id = _add_crew("MUHAMMAD WAQAR")
    _seed_duty_yesterday(crew_id)

    at = page_app.run()
    at.text_input[0].input(f"show {crew_id} duties yesterday")
    at.button[0].click()
    at = at.run()
    assert len(at.dataframe) == 1  # the seeded duty is found

    # Edit both date_input widgets to a window with no data at all.
    at.date_input[0].set_value(dt.date(2020, 1, 1))
    at.date_input[1].set_value(dt.date(2020, 1, 2))
    at = at.run()

    assert not at.exception
    assert len(at.dataframe) == 0
    assert any("No matching records" in i.value for i in at.info)


def test_decision_question_shows_refusal_message(page_app):
    at = page_app.run()
    at.text_input[0].input("who can fly tonight's 786")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("Roster page" in w.value for w in at.warning)
    assert len(at.dataframe) == 0
    assert len(at.date_input) == 0


def test_ambiguous_crew_name_shows_both_real_names(page_app):
    # Both Mahmoods MUST be seeded before the run that submits the
    # question -- crew_directory is built fresh from whatever crew
    # exist at that point, so seeding after (or seeding only one)
    # would silently resolve instead of triggering the ambiguity this
    # test is for.
    _add_crew("SYED FAHIM MAHMOOD")
    _add_crew("TAHIR MAHMOOD RAJA")

    at = page_app.run()
    at.text_input[0].input("show mahmood duties last month")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("Which crew member did you mean" in i.value for i in at.info)
    combined = " ".join(m.value for m in at.markdown)
    assert "SYED FAHIM MAHMOOD" in combined
    assert "TAHIR MAHMOOD RAJA" in combined
    assert len(at.dataframe) == 0


def test_ambiguous_between_templates_shows_candidates(page_app):
    """'duty rotation' is a confirmed genuine tie (verified directly
    against score_templates() before being used here, not assumed):
    crew_duty_history and roster_coverage both score 2 from 'duty' and
    'rotation' respectively, with nothing else scoring at all."""
    at = page_app.run()
    at.text_input[0].input("duty rotation")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("more than one thing" in i.value for i in at.info)
    combined = " ".join(m.value for m in at.markdown)
    assert "crew_duty_history" in combined
    assert "roster_coverage" in combined


def test_unparseable_question_shows_capabilities(page_app):
    at = page_app.run()
    at.text_input[0].input("blah blah nonsense")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    combined = " ".join(m.value for m in at.markdown)
    assert "crew_duty_history" in combined
    assert "regulation" in combined


def test_notes_are_shown_for_crew_duty_history(page_app):
    crew_id = _add_crew("MUHAMMAD WAQAR")
    _seed_duty_yesterday(crew_id)

    at = page_app.run()
    at.text_input[0].input(f"show {crew_id} duties yesterday")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("do not sum" in w.value.lower() or "duty-level" in w.value for w in at.warning)


def test_download_button_appears_for_resolved_query_with_results(page_app):
    """This Streamlit version's AppTest has no first-class
    download_button element type (confirmed directly: .get() returns
    UnknownElement()), so this is presence-only -- the button's actual
    byte content is already covered by test_reporting_export.py's
    existing dataset_to_xlsx()/report_filename() tests."""
    crew_id = _add_crew("MUHAMMAD WAQAR")
    _seed_duty_yesterday(crew_id)

    at = page_app.run()
    at.text_input[0].input(f"show {crew_id} duties yesterday")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert len(at.get("download_button")) == 1


def test_regulation_question_resolves_via_question_passthrough(page_app):
    """run_report() needs the raw question text for the regulation
    template specifically (ReportRequest can't carry it) — this proves
    the page actually passes it through, not just that some table
    appears."""
    at = page_app.run()
    at.text_input[0].input("D21.1 reporting times")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert len(at.date_input) == 0  # regulation has no date dimension
    assert len(at.dataframe) == 1
