"""
tests/test_roster_generation_page.py

AppTest coverage for pages/6_Roster_Generation.py, same fixture
pattern as tests/test_roster_page.py / test_control_room_page.py /
test_assistant_page.py. First page test to exercise Phase 7's whole
chain end to end: create_template() -> expand_and_persist() ->
approve_instance() -> the page's own Generate/Publish, not synthetic
shortcuts — same discipline tests/test_roster_generator_service.py
already established for the service layer this page presents.

Dates are computed relative to dt.date.today() (never hardcoded) so
this file stays valid indefinitely, same discipline as every other
page test file this session.

Rebuilt for the flight-deck crew package (2026-08-13): seats are
Commander/Second Pilot, not CPT/FO. Uncovered is now shown in its own
always-current "Currently uncovered seats" panel (durable, reads
uncovered_seats directly), rendered BEFORE the Generate button — so
its dataframe, when present, is dataframe[0], ahead of the proposal
and fairness dataframes below it.

REBUILT AGAIN for the preview/accept redesign. Generate no longer
writes anything, so every test here that needs rows in the database
clicks Generate AND THEN "Accept and publish". The tests that pinned the
old one-click flow were rewritten rather than deleted: what each was
actually covering — the real rejection reason reaching the page, pair
atomicity at publish, idempotency, the row-vs-duty count — is all still
covered. Three tests are new, because the behaviour they pin did not
exist before: that Generate writes NOTHING (asserted against
search_roster() BETWEEN the clicks, which is the only way to tell this
redesign from a relabelling), what a partial accept leaves on screen,
and that accept offers no further step.

ACCEPT PUBLISHES (operator decision, 2026-09-01): it writes PLANNED, not
PROPOSED, so there is no third click for anything this page produces.
publish_window() survives only for PROPOSED rows written before that
change, and the page renders its control only when such rows exist — so
the two tests covering it manufacture those rows through the service
directly, because the page can no longer create one.
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

    at = authed_app_test("pages/6_Roster_Generation.py")
    yield at

    get_engine.cache_clear()


# ------------------------------------------------------------------
# Grounding data — same real EPE 786/787 (domestic) and EPE 802/804/805
# (international) rotations as tests/test_roster_generator_service.py.
# ------------------------------------------------------------------
DOMESTIC_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
     "flight_no": "EPE 786", "domestic": True},
    {"leg_order": 2, "origin": "LHE", "destination": "KHI",
     "dep_time": dt.time(22, 0), "arr_time": dt.time(23, 45),
     "flight_no": "EPE 787", "domestic": True},
]
DOMESTIC_DAYS = [1, 2, 3, 4, 5]  # ISO weekday, Mon-Fri

INTERNATIONAL_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(1, 45), "arr_time": dt.time(3, 30),
     "flight_no": "EPE 802", "domestic": False},
    {"leg_order": 2, "origin": "LHE", "destination": "DWC",
     "dep_time": dt.time(4, 30), "arr_time": dt.time(8, 0),
     "flight_no": "EPE 804", "domestic": False},
    {"leg_order": 3, "origin": "DWC", "destination": "KHI",
     "dep_time": dt.time(9, 0), "arr_time": dt.time(11, 0),
     "flight_no": "EPE 805", "domestic": False},
]
INTERNATIONAL_DAYS = [2, 4, 5, 6]  # ISO weekday, Tue/Thu/Fri/Sat

_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY, "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY, "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY, "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY, "dg_expiry": _FAR_FUTURE_EXPIRY,
    # Missing date_of_birth triggers AE-CREW-PAIR-AGE-001_DOB_MISSING ->
    # NEEDS_MANUAL_REVIEW for every pairing, so no seat ever fills (found
    # 2026-08-09 in real-Postgres verification). A fixed, clearly-under-65
    # DOB keeps these tests exercising rest/FDP/coverage mechanics, not
    # the age-pairing gate.
    "date_of_birth": dt.date(1980, 1, 1),
}


def _next_weekday(start: dt.date, iso_weekday: int) -> dt.date:
    """Next date >= start whose isoweekday() == iso_weekday."""
    days_ahead = (iso_weekday - start.isoweekday()) % 7
    return start + dt.timedelta(days=days_ahead)


def _add_crew(role, **overrides):
    from services import crew_service
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _make_domestic_instance(date):
    from services import rotation_template_service as rts
    rts.create_template(
        rotation_code="EPE-786-787", days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2020, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-KHI domestic",
    )
    created = rts.expand_and_persist("EPE-786-787", date, date)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created[0]


def _make_domestic_instance_range(date_from, date_to):
    """The domestic template expanded across a RANGE, for the tests that
    need two rotations on different days with different crew — a partial
    accept has no meaning against a single rotation."""
    from services import rotation_template_service as rts
    rts.create_template(
        rotation_code="EPE-786-787", days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2020, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-KHI domestic",
    )
    created = rts.expand_and_persist("EPE-786-787", date_from, date_to)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created


def _make_international_instances(date_from, date_to):
    from services import rotation_template_service as rts
    rts.create_template(
        rotation_code="EPE-802-805", days_of_week=INTERNATIONAL_DAYS, legs=INTERNATIONAL_LEGS,
        effective_from=dt.date(2020, 1, 1), meal_provided=True, snack_provided=True,
        description="KHI-LHE-DWC-KHI international",
    )
    created = rts.expand_and_persist("EPE-802-805", date_from, date_to)
    for instance_id in created:
        rts.approve_instance(instance_id)
    return created


def _set_window(at, date_from, date_to):
    at.date_input[0].set_value(date_from)
    at.date_input[1].set_value(date_to)
    return at.run()


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


def test_no_approved_rotations_shows_templates_page_pointer(page_app):
    at = page_app.run()
    assert any("Schedule Templates page" in i.value for i in at.info)


def test_currently_uncovered_panel_shows_success_when_nothing_open(page_app):
    at = page_app.run()
    assert any("No open uncovered seats in this window" in s.value for s in at.success)


# ------------------------------------------------------------------
# Generate proposes; Accept writes. The two-step flow is the contract
# this file was rewritten for (preview/accept redesign) — every test
# below that needs rows in the database now clicks BOTH buttons, and
# the first test below exists to prove the first click alone writes
# nothing.
# ------------------------------------------------------------------

def test_generate_writes_nothing_and_accept_is_what_writes(page_app):
    """THE new contract, asserted against the database rather than the
    screen.

    Generation used to write PROPOSED rows as it walked the window. It
    no longer writes at all — the proposal exists only in the page's
    session state until Accept. Checking search_roster() between the
    two clicks is the only way to tell the redesign from a cosmetic
    relabelling of the old one.
    """
    from services import assignment_service

    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("CPT")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")
    assert not at.exception

    between = assignment_service.search_roster(
        date_from=date, date_to=date, include_proposed=True, include_cancelled=True)
    assert between.empty, (
        f"Generate wrote {len(between)} roster row(s) — the preview is "
        f"supposed to write nothing at all"
    )
    # The proposal is on screen even though nothing is stored.
    assert any("Nothing has been written" in i.value for i in at.info)

    at = _click(at, "Accept and publish")
    assert not at.exception

    after = assignment_service.search_roster(date_from=date, date_to=date, include_proposed=True)
    assert len(after) == 4  # 2 legs x 2 seats
    assert set(after["status"]) == {"PLANNED"}


def test_uncovered_is_not_recorded_durably_until_accept(page_app):
    """The mirror of the test above, for the other write path.

    uncovered_seats is the durable record of what is open. A preview a
    controller walked away from must not have edited it — otherwise
    merely LOOKING at a window would leave permanent findings behind.
    """
    from services import roster_generator_service

    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("FO")  # no CPT at all -- guaranteed uncovered on both seats

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")

    assert roster_generator_service.get_open_uncovered_seats(date, date).empty

    at = _click(at, "Accept and publish")
    open_now = roster_generator_service.get_open_uncovered_seats(date, date)
    assert set(open_now["operating_position"]) == {"COMMANDER", "SECOND_PILOT"}


def test_proposal_shows_the_pair_and_the_fairness_counts(page_app):
    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")

    assert not at.exception
    # No uncovered dataframe renders when nothing's open, so the
    # proposal table is dataframe[0] and the two fairness tables follow.
    proposal = at.dataframe[0].value
    assert proposal["Commander"].iloc[0].startswith(cpt_id)
    assert proposal["Second Pilot"].iloc[0].startswith(fo_id)

    commander_df = at.dataframe[1].value
    second_pilot_df = at.dataframe[2].value
    assert commander_df["Crew"].iloc[0].startswith(cpt_id)
    assert int(commander_df["Duties proposed"].iloc[0]) == 1
    assert second_pilot_df["Crew"].iloc[0].startswith(fo_id)
    assert int(second_pilot_df["Duties proposed"].iloc[0]) == 1

    at = _click(at, "Accept and publish")
    assert any("were written" in s.value for s in at.success)


# ------------------------------------------------------------------
# uncovered — the real rule-derived reason, and the structural
# no-candidates case. Both are now findings ON THE PROPOSAL first and
# durable records only after Accept.
# ------------------------------------------------------------------

def test_uncovered_no_candidates_reason_reaches_the_proposal(page_app):
    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("FO")  # no CPT at all -- guaranteed, clean uncovered on both seats

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")

    assert not at.exception
    assert any("could not be crewed" in e.value for e in at.error)
    assert any("No candidates in pool" in c.value for c in at.caption)

    # And after Accept it is in the durable panel, with both seats.
    at = _click(at, "Accept and publish")
    uncovered_df = at.dataframe[0].value
    assert "No candidates in pool" in uncovered_df["Reason"].iloc[0]
    assert set(uncovered_df["Position"]) == {"COMMANDER", "SECOND_PILOT"}


def test_uncovered_real_rejection_reason_reaches_page(page_app):
    """Distinct from the no-candidates case above: this proves the
    actual legality-gate rejection string a controller would act on
    reaches the page unaltered, not just that the section renders in
    the right place. Back-to-back international duty for the sole CPT
    candidate is a real, already-established rest-math rejection
    (tests/test_roster_generator_service.py's own grounding case) —
    under the pair model, BOTH seats show uncovered together (pair
    atomicity), not just Commander.

    UNDER THE PREVIEW DESIGN this rejection is now produced by the
    PROVISIONAL row for Thursday's duty rather than by a committed one,
    since the preview writes nothing between the two rotations. That
    makes this test a real-Postgres companion to
    tests/test_cross_rotation_legality.py: same mechanism, exercised
    through the page against the actual database.
    """
    thu = _next_weekday(dt.date.today(), 4)
    fri = thu + dt.timedelta(days=1)
    _make_international_instances(thu, fri)
    _add_crew("CPT")  # sole candidate -- no fairness escape route
    _add_crew("FO")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, thu, fri)
    at = _click(at, "Generate")
    at = _click(at, "Accept and publish")

    assert not at.exception
    uncovered_df = at.dataframe[0].value
    fri_commander = uncovered_df[(uncovered_df["Date"] == fri) & (uncovered_df["Position"] == "COMMANDER")]
    assert len(fri_commander) == 1
    reason = fri_commander["Reason"].iloc[0]
    assert "REJECTED" in reason or "NEEDS_MANUAL_REVIEW" in reason
    assert "No candidates in pool" not in reason


# ------------------------------------------------------------------
# Partial accept — the interaction this redesign had to decide rather
# than discover.
# ------------------------------------------------------------------

def test_a_rotation_refused_at_accept_keeps_its_crew_and_its_reason(page_app):
    """Several written, one refused: what the page shows, and what it
    does NOT offer.

    Engineered by expiring one pilot's medical between Generate and
    Accept — a real qualification-gate refusal, produced by the same gate
    that approved the proposal moments earlier, not a faked result.

    THE DOOMED PILOT IS THE ONE CREWED ON THE FEWEST ROTATIONS, and that
    choice is the whole fixture. An earlier version of this test doomed
    Tuesday's Commander after checking only that the two days had
    DIFFERENT Commanders — which they did, while the same pilot was also
    sitting as Monday's Second Pilot, because the Second Pilot pool
    includes CPTs and a CPT with no Second Pilot duties sorts as
    under-used for that seat. Expiring them correctly refused BOTH
    rotations, nothing was written, and the test read that as "partial
    accept writes nothing" — a false alarm about a designed behaviour
    (2026-09-01). Checking Commanders is not enough; what matters is
    whether the doomed pilot appears ANYWHERE in another rotation.

    The written and refused sets are both asserted non-empty, so a
    fixture that stops producing a genuine partial failure says so
    rather than quietly inverting its own claim. The scoping guarantee
    itself is pinned without a database in
    tests/test_partial_accept.py — this test is about the SCREEN.

    Three things must hold, and each is a decision:
      * the rotations that passed are WRITTEN, and stay written;
      * the refused rotation is still named, still shows the crew that
        were proposed for it, and shows why it was refused — discarding
        the proposal would destroy the only record of what happened;
      * there is NO second Accept. The rotations that just committed
        changed what is legal for the one that did not, so replaying the
        proposal would be proposing from stale information — which is
        the whole defect this redesign exists to prevent.
    """
    from services import assignment_service, crew_service

    mon = _next_weekday(dt.date.today(), 1)
    fri = mon + dt.timedelta(days=4)
    _make_domestic_instance_range(mon, fri)
    for _ in range(3):
        _add_crew("CPT")
    for _ in range(3):
        _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, mon, fri)
    at = _click(at, "Generate")
    assert not at.exception

    proposal = at.dataframe[0].value
    assert len(proposal) == 5, proposal

    # Which dates each pilot is crewed on, across BOTH seats.
    appearances = {}
    for _, row in proposal.iterrows():
        for column in ("Commander", "Second Pilot"):
            crew_id = row[column].split(" ")[0]
            appearances.setdefault(crew_id, set()).add(row["Date"])

    doomed_id = min(appearances, key=lambda cid: len(appearances[cid]))
    doomed_dates = appearances[doomed_id]
    surviving_dates = {row["Date"] for _, row in proposal.iterrows()} - doomed_dates
    assert surviving_dates, (
        f"{doomed_id} is crewed on every rotation ({sorted(doomed_dates)}), so "
        f"grounding them refuses the whole window — this fixture needs a "
        f"pilot who is not on every day to produce a PARTIAL accept"
    )

    crew_service.update_crew(doomed_id, {"medical_expiry": dt.date(2020, 1, 1)})

    at = _click(at, "Accept and publish")
    assert not at.exception

    # The refusal is on screen, naming the crew proposed for it and why.
    assert any("refused on re-check" in e.value for e in at.error)
    assert any(doomed_id in m.value for m in at.markdown)
    assert any("Run Generate again" in i.value for i in at.info)

    # No second Accept is offered.
    assert not [b for b in at.button if b.label == "Accept and publish"]

    # The surviving rotations really did commit; the doomed one did not.
    rows = assignment_service.search_roster(
        date_from=mon, date_to=fri, include_proposed=True, include_cancelled=True)
    assert set(rows["duty_date"]) == surviving_dates, sorted(set(rows["duty_date"]))
    assert doomed_id not in set(rows["crew_id"])


# ------------------------------------------------------------------
# Idempotency
# ------------------------------------------------------------------

def test_generate_twice_is_idempotent(page_app):
    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("CPT")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")
    at = _click(at, "Accept and publish")
    assert any("were written" in s.value for s in at.success)

    at = _click(at, "Generate")
    assert not at.exception
    assert any("already fully crewed" in c.value for c in at.caption)
# ------------------------------------------------------------------
# Accept publishes. There is no third step for anything this page
# produces — the section below only exists for PROPOSED rows written
# before 2026-09-01, so these two tests manufacture such rows directly
# rather than through the page, which can no longer create one.
# ------------------------------------------------------------------

def _legacy_proposed_rotation(date):
    """A rotation crewed as PROPOSED, the way generation used to leave
    it. The page cannot produce this any more, so it is written through
    the service directly — the point of these tests is what happens to
    rows that already exist in that state."""
    from services import assignment_service, rotation_template_service as rts

    instance_id = _make_domestic_instance(date)
    cpt_id = _add_crew("CPT")
    fo_id = _add_crew("FO")
    flight_ids = rts.get_promoted_flight_ids(instance_id)
    result = assignment_service.assign_pair_to_duty(
        cpt_id, fo_id, flight_ids, roster_status="PROPOSED")
    assert result.status == "ALLOWED", result.status
    return cpt_id, fo_id


def test_accept_publishes_directly_and_offers_no_further_step(page_app):
    """The operator's model: preview -> accept -> done.

    Accept writes PLANNED, which is what crew see, so the cleanup section
    must not render at all. A permanent Publish control would reassert
    the three-step flow this change removed, and a controller would
    reasonably read it as accepting not having finished the job.
    """
    from services import assignment_service

    date = _next_weekday(dt.date.today(), 1)
    _make_domestic_instance(date)
    _add_crew("CPT")
    _add_crew("FO")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Generate")
    at = _click(at, "Accept and publish")
    assert not at.exception

    # search_roster() is sector-level: the domestic rotation has 2 legs
    # x 2 crew (Commander+Second Pilot) = 4 rows, not 2 duties.
    roster = assignment_service.search_roster(date_from=date, date_to=date, include_proposed=True)
    assert len(roster) == 4
    assert set(roster["status"]) == {"PLANNED"}

    assert not [b for b in at.button if b.label == "Publish these"]
    assert not any("still" in c.value and "PROPOSED" in c.value for c in at.caption)


def test_legacy_proposed_rows_can_still_be_published(page_app):
    """The rows already in production have a route forward.

    publish_window() is kept for exactly this and nothing else; deleting
    it would strand real roster rows in a status nothing promotes and the
    Roster page does not display.
    """
    from services import assignment_service

    date = _next_weekday(dt.date.today(), 1)
    _legacy_proposed_rotation(date)

    at = page_app.run()
    at = _set_window(at, date, date)
    assert any("still" in c.value and "PROPOSED" in c.value for c in at.caption)

    at = _click(at, "Publish these")
    assert not at.exception
    assert any("Published 4 roster row" in s.value for s in at.success)
    assert not any("remain PROPOSED" in w.value for w in at.warning)

    roster = assignment_service.search_roster(date_from=date, date_to=date, include_proposed=True)
    assert set(roster["status"]) == {"PLANNED"}

    # Section gone once there is nothing left to clean up.
    assert not [b for b in at.button if b.label == "Publish these"]


def test_manual_unassign_before_publish_skips_the_whole_rotation(page_app):
    """A legacy rotation with only one seat still active must not publish
    at all — pair atomicity means BOTH pilots' rows stay as they are, not
    just the unassigned one; the page must surface how many rows remain
    PROPOSED rather than silently reporting a clean publish.

    Unchanged in substance from the pre-2026-09-01 version: publish_window()
    itself is untouched, so the property it guarantees is untouched too.
    Only the way the PROPOSED rows come into existence has changed, since
    the page can no longer create them.
    """
    from services import assignment_service

    date = _next_weekday(dt.date.today(), 1)
    cpt_id, _ = _legacy_proposed_rotation(date)

    proposed = assignment_service.search_roster(date_from=date, date_to=date, include_proposed=True)
    cpt_duty_id = proposed[proposed["crew_id"] == cpt_id].iloc[0]["duty_id"]
    assignment_service.remove_assignment_from_duty(cpt_id, cpt_duty_id, reason="test reject")

    at = page_app.run()
    at = _set_window(at, date, date)
    at = _click(at, "Publish these")
    assert not at.exception
    assert any("Published 0 roster row" in s.value for s in at.success)
    assert any("remain PROPOSED" in w.value for w in at.warning)

    after = assignment_service.search_roster(
        date_from=date, date_to=date, include_proposed=True, include_cancelled=True)
    cpt_after = after[after["crew_id"] == cpt_id]
    fo_after = after[after["crew_id"] != cpt_id]
    assert set(cpt_after["status"]) == {"CANCELLED"}
    assert set(fo_after["status"]) == {"PROPOSED"}  # NOT published -- the pair was incomplete


# ------------------------------------------------------------------
# Window-size warning
# ------------------------------------------------------------------

def test_window_warning_for_wide_window_not_for_default(page_app):
    today = dt.date.today()

    at = page_app.run()
    assert not any("spans" in w.value for w in at.warning)

    at = _set_window(at, today, today + dt.timedelta(days=40))
    assert any("spans" in w.value for w in at.warning)
