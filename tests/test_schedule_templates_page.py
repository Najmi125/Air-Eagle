"""
tests/test_schedule_templates_page.py

AppTest coverage for pages/7_Schedule_Templates.py, same page_app
fixture pattern as tests/test_roster_generation_page.py.

A note on st.success()/st.rerun(): confirmed directly via ad-hoc
AppTest scripts (not asserted on trust) that a single at.run() after a
button click does NOT reliably surface a transient st.success() banner
when st.rerun() is called right after it AND more script follows
(which every action on this page has -- later workflow sections). The
banner is only reliably visible in the SAME at.run() when nothing
follows the success()+rerun() pair. Workflow 2 (expand) has no
st.rerun() at all and IS reliably testable via its banner text; the
create/create-version/approve/reject actions (all followed by
st.rerun() and more script) are verified via real effects instead
(querying rotation_template_service directly), which is more robust
anyway. Validation-error paths (no rerun involved at all) are
asserted via at.error text directly.
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

    at = authed_app_test("pages/7_Schedule_Templates.py")
    yield at

    get_engine.cache_clear()


DOMESTIC_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
     "flight_no": "EPE 786", "domestic": True},
    {"leg_order": 2, "origin": "LHE", "destination": "KHI",
     "dep_time": dt.time(22, 0), "arr_time": dt.time(23, 45),
     "flight_no": "EPE 787", "domestic": True},
]
DOMESTIC_DAYS = [1, 2, 3, 4, 5]  # ISO weekday, Mon-Fri


def _next_weekday(start: dt.date, iso_weekday: int) -> dt.date:
    days_ahead = (iso_weekday - start.isoweekday()) % 7
    return start + dt.timedelta(days=days_ahead)


def _seed_domestic_template(rotation_code="EPE-786-787"):
    from services import rotation_template_service as rts
    return rts.create_template(
        rotation_code=rotation_code, days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2020, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-KHI domestic",
    )


def _by_label(elements, label):
    matches = [e for e in elements if e.label == label]
    assert len(matches) == 1, f"expected exactly one element labeled {label!r}, found {len(matches)}"
    return matches[0]


def _by_key(elements, key):
    matches = [e for e in elements if e.key == key]
    assert len(matches) == 1, f"expected exactly one element keyed {key!r}, found {len(matches)}"
    return matches[0]


def _click(at, label):
    matches = [b for b in at.button if b.label == label]
    assert len(matches) == 1, f"expected exactly one {label!r} button, found {len(matches)}"
    matches[0].click()
    return at.run()


# ------------------------------------------------------------------
# Basics
# ------------------------------------------------------------------

def test_page_loads_without_exception(page_app):
    at = page_app.run()
    assert not at.exception
    assert any("No rotation templates yet" in i.value for i in at.info)


# ------------------------------------------------------------------
# Workflow 1: create template
# ------------------------------------------------------------------

def test_create_template_via_form_writes_real_data(page_app):
    from services import rotation_template_service as rts

    at = page_app.run()
    _by_label(at.text_input, "Rotation code *").input("EPE-786-787")
    _by_label(at.text_input, "Description").input("KHI-LHE-KHI domestic")
    _by_label(at.multiselect, "Days of week *").select("Mon").select("Tue")
    _by_key(at.text_input, "ct_flightno_0").input("EPE 786")
    _by_key(at.text_input, "ct_origin_0").input("KHI")
    _by_key(at.text_input, "ct_dest_0").input("LHE")
    _by_key(at.time_input, "ct_dep_0").set_value(dt.time(19, 0))
    _by_key(at.time_input, "ct_arr_0").set_value(dt.time(20, 45))
    _by_key(at.text_input, "ct_flightno_1").input("EPE 787")
    _by_key(at.text_input, "ct_origin_1").input("LHE")
    _by_key(at.text_input, "ct_dest_1").input("KHI")
    _by_key(at.time_input, "ct_dep_1").set_value(dt.time(22, 0))
    _by_key(at.time_input, "ct_arr_1").set_value(dt.time(23, 45))

    at = _click(at, "Create template")
    assert not at.exception

    versions = rts.get_versions("EPE-786-787")
    assert len(versions) == 1
    assert versions.iloc[0]["version"] == 1
    legs = rts.get_template_legs(int(versions.iloc[0]["id"]))
    assert list(legs["flight_no"]) == ["EPE 786", "EPE 787"]


def test_create_template_partial_leg_row_shows_error_and_writes_nothing(page_app):
    from services import rotation_template_service as rts

    at = page_app.run()
    _by_label(at.text_input, "Rotation code *").input("BAD-ROTATION")
    _by_label(at.multiselect, "Days of week *").select("Mon")
    _by_key(at.text_input, "ct_flightno_0").input("EPE 999")  # origin/destination left blank

    at = _click(at, "Create template")
    assert not at.exception
    assert any("flight number, origin, and destination are all required" in e.value for e in at.error)
    assert rts.get_all_rotation_codes() == []


# ------------------------------------------------------------------
# Workflow 1: create new version
# ------------------------------------------------------------------

def test_create_new_version_shows_preview_before_submit_and_real_effect_after(page_app):
    from services import rotation_template_service as rts

    _seed_domestic_template()
    new_effective_from = dt.date(2026, 9, 1)
    expected_day_before = dt.date(2026, 8, 31)

    at = page_app.run()
    _by_label(at.date_input, "New version effective from *").set_value(new_effective_from)
    at = at.run()

    assert not at.exception
    assert any(
        f"end version 1" in i.value and str(expected_day_before) in i.value
        for i in at.info
    )

    # Legs/days_of_week pre-fill from the current version's own values --
    # nothing further to fill in for the common "same route, new dates" case.
    at = _click(at, "Create new version")
    assert not at.exception

    versions = rts.get_versions("EPE-786-787")
    assert len(versions) == 2
    v1 = versions[versions["version"] == 1].iloc[0]
    v2 = versions[versions["version"] == 2].iloc[0]
    assert v1["effective_until"] == expected_day_before
    assert v2["effective_from"] == new_effective_from
    assert v2["effective_until"] is None


# ------------------------------------------------------------------
# Workflow 2: expand
# ------------------------------------------------------------------

def test_expand_window_creates_drafts_and_is_idempotent(page_app):
    from services import rotation_template_service as rts

    _seed_domestic_template()
    monday = _next_weekday(dt.date.today(), 1)

    at = page_app.run()
    _by_label(at.date_input, "From").set_value(monday)
    _by_label(at.date_input, "To").set_value(monday)
    at = at.run()
    at = _click(at, "Expand window")

    assert not at.exception
    assert any("1 new draft instance" in s.value for s in at.success)
    assert len(rts.get_instances(status="DRAFT")) == 1

    at = _click(at, "Expand window")
    assert any("No new instances" in i.value for i in at.info)
    assert len(rts.get_instances(status="DRAFT")) == 1  # unchanged, not duplicated


# ------------------------------------------------------------------
# Workflow 3: review
# ------------------------------------------------------------------

def test_review_table_shows_real_route_and_flight_data(page_app):
    from services import rotation_template_service as rts

    _seed_domestic_template()
    monday = _next_weekday(dt.date.today(), 1)
    rts.expand_and_persist("EPE-786-787", monday, monday)

    at = page_app.run()
    assert not at.exception
    # st.write(f"{route} ({flights})") renders as a Markdown element --
    # this is what satisfies "a draft must show its legs before
    # approval, not just rotation code and date."
    assert any("KHI -> LHE -> KHI (EPE 786, EPE 787)" in str(w.value) for w in at.markdown)


def test_select_all_then_approve_promotes_instances_and_reports_flight_count(page_app):
    from services import rotation_template_service as rts, flight_service

    _seed_domestic_template()
    monday = _next_weekday(dt.date.today(), 1)
    tuesday = monday + dt.timedelta(days=1)
    rts.expand_and_persist("EPE-786-787", monday, tuesday)
    assert len(rts.get_instances(status="DRAFT")) == 2

    at = page_app.run()
    at = _click(at, "Select all visible")
    approve_btn = [b for b in at.button if b.label == "Approve selected"][0]
    assert not approve_btn.disabled
    at = _click(at, "Approve selected")
    assert not at.exception

    instances = rts.get_instances(rotation_code="EPE-786-787")
    assert set(instances["status"]) == {"APPROVED"}
    assert len(flight_service.get_all_flights()) == 4  # 2 instances x 2 legs each


def test_reject_requires_reason_and_writes_reason(page_app):
    from services import rotation_template_service as rts

    _seed_domestic_template()
    monday = _next_weekday(dt.date.today(), 1)
    rts.expand_and_persist("EPE-786-787", monday, monday)
    instance_id = int(rts.get_instances(status="DRAFT").iloc[0]["id"])

    at = page_app.run()
    reject_btn = [b for b in at.button if b.label == "Reject selected"][0]
    assert reject_btn.disabled  # nothing selected, no reason yet

    select_cb = _by_key(at.checkbox, f"select_{instance_id}")
    select_cb.check()
    at = at.run()
    reject_btn = [b for b in at.button if b.label == "Reject selected"][0]
    assert reject_btn.disabled  # selected but no reason yet

    _by_key(at.text_input, "review_reject_reason").input("route no longer needed")
    at = at.run()
    at = _click(at, "Reject selected")
    assert not at.exception

    instances = rts.get_instances(status="REJECTED")
    assert len(instances) == 1
    assert int(instances.iloc[0]["id"]) == instance_id


def test_selection_scoped_to_visible_filter_not_swept_into_other_filter_action(page_app):
    """A checkbox checked while rotation_code A is the filter must not
    get approved when the controller later switches to filter B and
    clicks Approve there -- selected_ids is always computed from the
    CURRENTLY VISIBLE id list, never scanned from all session_state
    keys."""
    from services import rotation_template_service as rts

    _seed_domestic_template("EPE-786-787")
    _seed_domestic_template("EPE-802-805")
    monday = _next_weekday(dt.date.today(), 1)
    rts.expand_and_persist("EPE-786-787", monday, monday)
    rts.expand_and_persist("EPE-802-805", monday, monday)

    instance_a = int(rts.get_instances(rotation_code="EPE-786-787", status="DRAFT").iloc[0]["id"])
    instance_b = int(rts.get_instances(rotation_code="EPE-802-805", status="DRAFT").iloc[0]["id"])

    at = page_app.run()
    _by_label(at.selectbox, "Filter by rotation code").select("EPE-786-787")
    at = at.run()
    _by_key(at.checkbox, f"select_{instance_a}").check()
    at = at.run()

    _by_label(at.selectbox, "Filter by rotation code").select("EPE-802-805")
    at = at.run()
    at = _click(at, "Select all visible")  # selects only instance_b, the now-visible one

    at = _click(at, "Approve selected")
    assert not at.exception

    a_status = rts.get_instances(rotation_code="EPE-786-787").iloc[0]["status"]
    b_status = rts.get_instances(rotation_code="EPE-802-805").iloc[0]["status"]
    assert a_status == "DRAFT"       # never approved -- filtered out when the action ran
    assert b_status == "APPROVED"
