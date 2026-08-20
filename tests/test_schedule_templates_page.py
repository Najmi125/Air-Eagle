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
import re
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
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


def _ct(elements, field):
    """A create-form widget by key, ignoring the generation segment.

    Label-based lookup is ambiguous here: the "create new version" form
    renders widgets with identical labels ("Description",
    "Days of week *"), so any test that creates a template and then
    interacts with the create form again hits two matches. Keys are
    "ct_{generation}_{field}" and the cv form's are
    "cv_{code}_{generation}_{field}", so they are distinguishable."""
    pattern = re.compile(rf"^ct_\d+_{re.escape(field)}$")
    matches = [e for e in elements if e.key and pattern.match(e.key)]
    assert len(matches) == 1, (
        f"expected exactly one create-form widget matching {pattern.pattern!r}, "
        f"found {[e.key for e in elements if e.key]}"
    )
    return matches[0]


def _leg(at, field, index, prefix="ct"):
    """A leg widget, located WITHOUT hardcoding the form-generation
    segment of its key.

    Leg keys are "{prefix}_{generation}_{field}_{index}" as of
    2026-08-19 — the generation is what forces fresh widgets after each
    save (see the page's own comment for the bug that made it
    necessary). Matching on a pattern rather than a literal key means
    these tests keep working across a generation bump, which is exactly
    what a test that submits two templates in a row needs.

    Every leg field is a text_input now; the times became HHMM text at
    the operator's request, replacing st.time_input."""
    pattern = re.compile(rf"^{re.escape(prefix)}_\d+_{re.escape(field)}_{index}$")
    matches = [e for e in at.text_input if e.key and pattern.match(e.key)]
    assert len(matches) == 1, (
        f"expected exactly one leg widget matching {pattern.pattern!r}, "
        f"found {[e.key for e in at.text_input if e.key]}"
    )
    return matches[0]


def _fill_leg(at, index, flight_no, origin, destination, dep, arr, prefix="ct"):
    _leg(at, "flightno", index, prefix).input(flight_no)
    _leg(at, "origin", index, prefix).input(origin)
    _leg(at, "dest", index, prefix).input(destination)
    _leg(at, "dep", index, prefix).input(dep)
    _leg(at, "arr", index, prefix).input(arr)


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
    _ct(at.text_input, "rotation_code").input("EPE-786-787")
    _ct(at.text_input, "description").input("KHI-LHE-KHI domestic")
    _ct(at.multiselect, "days").select("Mon").select("Tue")
    _fill_leg(at, 0, "EPE 786", "KHI", "LHE", "1900", "2045")
    _fill_leg(at, 1, "EPE 787", "LHE", "KHI", "2200", "2345")

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
    _ct(at.text_input, "rotation_code").input("BAD-ROTATION")
    _ct(at.multiselect, "days").select("Mon")
    _leg(at, "flightno", 0).input("EPE 999")  # origin/destination/times left blank

    at = _click(at, "Create template")
    assert not at.exception
    assert any("partially filled" in e.value for e in at.error)
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


# ------------------------------------------------------------------
# Workflow 1: the widget-key regression (2026-08-19)
# ------------------------------------------------------------------

def test_second_template_created_without_reload_does_not_inherit_the_first(page_app):
    """The reported bug, reproduced end to end.

    An operator created EPE-786-787 (2 legs), then EPE-802-804-805
    (3 legs) without reloading the page. The second template saved
    leg 2 carrying the FIRST template's values — LHE->KHI 2200-2345
    domestic instead of LHE->DWC 0430-0800 international — and leg 3
    vanished entirely.

    Both halves are asserted: every leg of the second template matches
    what was typed for the second template, and leg 3 exists at all.
    """
    from services import rotation_template_service as rts

    at = page_app.run()
    _ct(at.text_input, "rotation_code").input("EPE-786-787")
    _ct(at.multiselect, "days").select("Mon")
    _fill_leg(at, 0, "EPE 786", "KHI", "LHE", "1900", "2045")
    _fill_leg(at, 1, "EPE 787", "LHE", "KHI", "2200", "2345")
    at = _click(at, "Create template")
    assert not at.exception

    # Second template, no reload — a genuinely different route, and one
    # more leg than the first.
    _ct(at.text_input, "rotation_code").input("EPE-802-804-805")
    _ct(at.multiselect, "days").select("Tue")
    _fill_leg(at, 0, "EPE 802", "KHI", "LHE", "0100", "0300")
    _fill_leg(at, 1, "EPE 804", "LHE", "DWC", "0430", "0800")
    _fill_leg(at, 2, "EPE 805", "DWC", "KHI", "0900", "1100")
    at = _click(at, "Create template")
    assert not at.exception

    versions = rts.get_versions("EPE-802-804-805")
    assert len(versions) == 1
    legs = rts.get_template_legs(int(versions.iloc[0]["id"]))

    assert len(legs) == 3, (
        "leg 3 was dropped — a row whose text fields were left stale-empty "
        "must not be silently treated as blank"
    )
    assert list(legs["flight_no"]) == ["EPE 802", "EPE 804", "EPE 805"]
    assert list(legs["origin"]) == ["KHI", "LHE", "DWC"]
    assert list(legs["destination"]) == ["LHE", "DWC", "KHI"]
    # The exact leg that carried the first template's values.
    assert legs.iloc[1]["dep_time"] == dt.time(4, 30)
    assert legs.iloc[1]["arr_time"] == dt.time(8, 0)

    # And the first template is untouched by any of this.
    first = rts.get_template_legs(int(rts.get_versions("EPE-786-787").iloc[0]["id"]))
    assert list(first["flight_no"]) == ["EPE 786", "EPE 787"]


def test_create_form_is_blank_after_a_successful_save(page_app):
    """The mechanism behind the fix, asserted directly: once a template
    is saved, the next render's leg widgets must come up empty rather
    than showing what was just submitted."""
    at = page_app.run()
    _ct(at.text_input, "rotation_code").input("EPE-786-787")
    _ct(at.multiselect, "days").select("Mon")
    _fill_leg(at, 0, "EPE 786", "KHI", "LHE", "1900", "2045")
    _fill_leg(at, 1, "EPE 787", "LHE", "KHI", "2200", "2345")

    at = _click(at, "Create template")

    assert not at.exception
    for field in ("flightno", "origin", "dest", "dep", "arr"):
        assert _leg(at, field, 0).value == "", (
            f"leg 1 {field} still shows the saved template's value"
        )


def test_leg_with_times_but_no_route_is_reported_not_silently_dropped(page_app):
    """The silent-skip half of the bug. "Filled" used to mean
    flight_no/origin/destination only, so a row with just times read as
    blank and disappeared without a word — which is what made leg 3's
    disappearance so hard to see."""
    from services import rotation_template_service as rts

    at = page_app.run()
    _ct(at.text_input, "rotation_code").input("PARTIAL-ROW")
    _ct(at.multiselect, "days").select("Mon")
    _fill_leg(at, 0, "EPE 786", "KHI", "LHE", "1900", "2045")
    _leg(at, "dep", 1).input("0900")   # times only, no route
    _leg(at, "arr", 1).input("1100")

    at = _click(at, "Create template")

    assert not at.exception
    assert any("partially filled" in e.value for e in at.error), (
        "a row with times but no route must be reported, not skipped"
    )
    assert rts.get_all_rotation_codes() == []


def test_invalid_hhmm_is_rejected_with_a_specific_message(page_app):
    from services import rotation_template_service as rts

    at = page_app.run()
    _ct(at.text_input, "rotation_code").input("BAD-TIME")
    _ct(at.multiselect, "days").select("Mon")
    _fill_leg(at, 0, "EPE 786", "KHI", "LHE", "2465", "2045")

    at = _click(at, "Create template")

    assert not at.exception
    assert any("2465" in e.value for e in at.error)
    assert rts.get_all_rotation_codes() == []


def test_times_accept_hhmm_without_a_separator(page_app):
    """The operator's actual ask: controllers write 0905, not 09:05."""
    from services import rotation_template_service as rts

    at = page_app.run()
    _ct(at.text_input, "rotation_code").input("HHMM-FORM")
    _ct(at.multiselect, "days").select("Mon")
    _fill_leg(at, 0, "EPE 786", "KHI", "LHE", "0905", "1130")

    at = _click(at, "Create template")
    assert not at.exception

    legs = rts.get_template_legs(int(rts.get_versions("HHMM-FORM").iloc[0]["id"]))
    assert legs.iloc[0]["dep_time"] == dt.time(9, 5)
    assert legs.iloc[0]["arr_time"] == dt.time(11, 30)


def test_disconnected_legs_are_rejected_at_creation(page_app):
    """Route continuity, surfaced where the mistake was made rather
    than days later at expansion."""
    from services import rotation_template_service as rts

    at = page_app.run()
    _ct(at.text_input, "rotation_code").input("BROKEN-ROUTE")
    _ct(at.multiselect, "days").select("Mon")
    _fill_leg(at, 0, "EPE 802", "KHI", "LHE", "0100", "0300")
    _fill_leg(at, 1, "EPE 804", "DWC", "KHI", "0430", "0800")

    at = _click(at, "Create template")

    assert not at.exception
    assert any("two places at once" in e.value for e in at.error)
    assert rts.get_all_rotation_codes() == []


# ------------------------------------------------------------------
# Workflow 1: delete an unused template
# ------------------------------------------------------------------

def test_unused_template_can_be_deleted_from_the_page(page_app):
    from services import rotation_template_service as rts

    _seed_domestic_template("EPE-786-787")
    at = page_app.run()

    at = _click(at, "Delete template")

    assert not at.exception
    assert rts.get_all_rotation_codes() == []


def test_delete_is_disabled_with_a_reason_once_the_template_is_used(page_app):
    from services import rotation_template_service as rts

    _seed_domestic_template("EPE-786-787")
    rts.expand_and_persist("EPE-786-787", dt.date(2026, 9, 1), dt.date(2026, 9, 7))

    at = page_app.run()

    delete_buttons = [b for b in at.button if b.label == "Delete template"]
    assert len(delete_buttons) == 1
    assert delete_buttons[0].disabled is True
    assert any("rotation instance" in c.value for c in at.caption)
    assert rts.get_all_rotation_codes() == ["EPE-786-787"]


# ------------------------------------------------------------------
# The delete affordance must never take the page down (2026-08-19)
# ------------------------------------------------------------------
#
# These need no database: every rotation_template_service call the page
# makes is faked, so they run in any environment. That is deliberate.
# The failure they guard against was found on a LIVE deployment, not by
# the suite, because everything covering this page is DB-gated and skips
# wherever Postgres is absent.

from streamlit.testing.v1 import AppTest  # noqa: E402
from tests.conftest import page_path      # noqa: E402

_FAKE_VERSIONS = pd.DataFrame([{
    "id": 1, "rotation_code": "EPE-786-787", "version": 1,
    "effective_from": dt.date(2026, 1, 1), "effective_until": None,
    "description": "KHI-LHE", "days_of_week": [1, 2, 3, 4, 5],
    "meal_provided": True, "snack_provided": True, "superseded_by": None,
}])
_FAKE_LEGS = pd.DataFrame([{
    "leg_order": 1, "flight_no": "EPE 786", "origin": "KHI", "destination": "LHE",
    "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
    "day_offset": 0, "domestic": True,
}])
_FAKE_INSTANCES = pd.DataFrame(
    columns=["id", "rotation_code", "rotation_date", "status", "template_id"])


@pytest.fixture
def faked_rts(monkeypatch):
    """Every service call the page makes, answered from memory."""
    from services import rotation_template_service as rts

    monkeypatch.setattr(rts, "get_all_rotation_codes", lambda: ["EPE-786-787"])
    monkeypatch.setattr(rts, "get_versions", lambda code: _FAKE_VERSIONS.copy())
    monkeypatch.setattr(rts, "get_template_legs", lambda tid: _FAKE_LEGS.copy())
    monkeypatch.setattr(rts, "get_instances", lambda **kw: _FAKE_INSTANCES.copy())
    monkeypatch.setattr(rts, "get_instance_legs", lambda iid: _FAKE_LEGS.copy())
    return rts


def _render_page():
    at = AppTest.from_file(str(page_path("pages/7_Schedule_Templates.py")))
    at.session_state["app_user"] = "occ1"
    at.run()
    return at


def _assert_page_is_usable(at):
    """The page's actual job: view templates, expand a window, review
    drafts. None of it depends on the delete affordance."""
    assert not at.exception, f"page failed to render: {at.exception}"
    assert any("Schedule Templates" in t.value for t in at.title)
    assert any(b.label == "Expand window" for b in at.button), (
        "the expand workflow must survive a delete-affordance failure"
    )


def test_page_renders_when_the_deletability_lookup_is_missing_entirely(faked_rts, monkeypatch):
    """The live outage, reproduced (2026-08-19).

    Streamlit re-executes the page script on every rerun but keeps
    imported modules in sys.modules for the life of the process, so a
    page updated without a full restart can call into a stale module
    object. The page called a service function added in the same commit,
    got AttributeError, and the ENTIRE page stopped rendering — the
    operator could not view, create, expand or review anything, nor
    clean up the templates this feature exists to remove.

    A greyed-out button is not worth a page."""
    monkeypatch.delattr(faked_rts, "get_template_deletability", raising=False)

    at = _render_page()

    _assert_page_is_usable(at)
    delete = [b for b in at.button if b.label == "Delete template"]
    assert len(delete) == 1 and delete[0].disabled is True
    assert any("could not be determined" in c.value for c in at.caption), (
        "a degraded delete control must say so, not look like an ordinary "
        "undeletable template"
    )


def test_page_renders_against_a_database_without_migration_019(faked_rts, monkeypatch):
    """get_template_deletability() calls migrations/019's
    rotation_template_is_deletable(). Before this degradation existed,
    a pre-019 database made the page fail to RENDER rather than merely
    fail to delete — a missing migration taking a working page off the
    air. It now renders with delete unavailable, which is the right
    behaviour for a database predating the feature."""
    def missing_function(_template_id):
        raise RuntimeError(
            "function rotation_template_is_deletable(integer) does not exist")

    monkeypatch.setattr(faked_rts, "get_template_deletability", missing_function)

    at = _render_page()

    _assert_page_is_usable(at)
    assert any("could not be determined" in c.value for c in at.caption)


def test_delete_control_is_enabled_normally_when_the_lookup_succeeds(faked_rts, monkeypatch):
    """The degradation must not swallow the healthy path — without this,
    a permanently broken lookup would look exactly like a working one."""
    monkeypatch.setattr(faked_rts, "get_template_deletability", lambda tid: {
        "deletable": True, "reason": None, "instance_count": 0, "version_count": 1})

    at = _render_page()

    _assert_page_is_usable(at)
    delete = [b for b in at.button if b.label == "Delete template"]
    assert len(delete) == 1 and delete[0].disabled is False
    assert not any("could not be determined" in c.value for c in at.caption)
