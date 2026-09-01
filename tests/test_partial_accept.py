"""
tests/test_partial_accept.py

What accept_preview() does when SOME rotations pass re-validation and
others do not.

WHY THIS FILE EXISTS. The design decision is that a refused rotation
costs a controller that rotation and nothing else — 35 good rotations
must not be thrown away because the 36th went stale between Generate and
Accept. That is a claim about transaction scope and about control flow,
and until now the only thing testing it was a real-Postgres page test
whose fixture happened to be wrong (2026-09-01: it expired a pilot who
turned out to be crewed on BOTH rotations, so every rotation was
correctly refused and the test read that as "partial accept writes
nothing"). A fixture that can produce a false alarm about a designed
behaviour is a reason to test the behaviour directly, in a place where
crew selection cannot drift into it.

NO DATABASE NEEDED, deliberately — same isolate_from_database() net as
tests/test_generation_round_trips.py, plus a recording engine that
captures every statement and every transaction boundary. The
transaction boundaries are the point: an implementation that wrapped the
whole window in one engine.begin() would still show the right INSERTs
here (a fake cannot roll back), so counting BEGINs is what actually
distinguishes per-rotation atomicity from all-or-nothing.

THE REFUSAL IS REAL. The crew snapshot is mutated between the preview
and the accept so that one pilot's medical has expired — accept re-reads
crew fresh, so the refusal is produced by the actual qualification gate,
the same gate that approved the proposal a moment earlier. Nothing here
stubs a REJECTED result into place.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import pytest

from test_generation_round_trips import CREW_COLUMNS, isolate_from_database

_WINDOW_START = dt.date(2026, 9, 7)
_ROTATIONS = 3
_COMMANDERS = 3
_SECOND_PILOTS = 3
_FAR_FUTURE = dt.date(2099, 1, 1)


def _crew_row(crew_id, role):
    row = {column: None for column in CREW_COLUMNS}
    row.update({"crew_id": crew_id, "name": f"Test {crew_id}", "role": role,
                "base": "KHI", "is_active": True,
                "date_of_birth": dt.date(1980, 1, 1)})
    for column in CREW_COLUMNS:
        if column.endswith("_expiry"):
            row[column] = _FAR_FUTURE
    return row


def _rotation_day(n):
    return _WINDOW_START + dt.timedelta(days=n - 1)


def _flight_row(flight_id):
    """Air Eagle's real two-sector domestic rotation, one per day."""
    n, leg = divmod(flight_id, 10)
    day = _rotation_day(n)
    if leg == 0:
        origin, destination, dep, arr = "KHI", "LHE", dt.time(19, 0), dt.time(20, 45)
    else:
        origin, destination, dep, arr = "LHE", "KHI", dt.time(22, 0), dt.time(23, 45)
    return {"flight_id": flight_id, "flight_no": f"EPE {700 + n}",
            "origin": origin, "destination": destination,
            "dep_time_planned": dt.datetime.combine(day, dep),
            "arr_time_planned": dt.datetime.combine(day, arr),
            "dep_time_actual": None, "arr_time_actual": None,
            "domestic": True, "status": "PLANNED", "meal_provided": True,
            "snack_provided": True, "cargo_dg": False}


class _Recorder:
    """Every statement and every transaction boundary the accept issues."""

    def __init__(self):
        self.transactions = 0
        self.inserted_roster_rows = []   # (crew_id, flight_id)

    def note(self, statement, params):
        text = str(statement).strip().upper()
        if text.startswith("INSERT INTO ROSTER") and params:
            self.inserted_roster_rows.append((params.get("crew_id"), params.get("flight_id")))


class _Result:
    rowcount = 1

    def scalar(self):
        return 1

    def scalars(self):
        return self

    def all(self):
        return []


class _Conn:
    def __init__(self, recorder):
        self.recorder = recorder

    def execute(self, statement, params=None, *a, **k):
        self.recorder.note(statement, params)
        return _Result()


class _Txn:
    def __init__(self, recorder):
        self.recorder = recorder

    def __enter__(self):
        return _Conn(self.recorder)

    def __exit__(self, *a):
        return False


class _RecordingEngine:
    def __init__(self, recorder):
        self.recorder = recorder

    def begin(self):
        self.recorder.transactions += 1
        return _Txn(self.recorder)

    def connect(self):
        self.recorder.transactions += 1
        return _Txn(self.recorder)


def _wire(monkeypatch, crew_frame, recorder):
    from services import assignment_service, crew_service, flight_service
    from services import roster_generator_service as rgs
    from services import rotation_template_service as rts

    instances = pd.DataFrame([
        {"id": n, "rotation_code": f"EPE-{n:03d}", "rotation_date": _rotation_day(n),
         "status": "APPROVED", "template_id": 1, "version": 1}
        for n in range(1, _ROTATIONS + 1)])

    isolate_from_database(monkeypatch)
    engine = _RecordingEngine(recorder)
    for module in (assignment_service, rgs):
        monkeypatch.setattr(module, "get_engine", lambda e=engine: e)

    # A LIST so the frame can be swapped between the preview and the
    # accept — accept re-reads crew, and that re-read is what makes the
    # refusal real rather than stubbed.
    monkeypatch.setattr(crew_service, "get_all_crew", lambda **k: crew_frame[0].copy())
    monkeypatch.setattr(
        crew_service, "get_crew",
        lambda cid: next((pd.Series(r) for _, r in crew_frame[0].iterrows()
                          if r["crew_id"] == cid), None))
    monkeypatch.setattr(flight_service, "get_flight",
                        lambda fid: pd.Series(_flight_row(fid)))
    monkeypatch.setattr(assignment_service, "_read_duty_rows",
                        lambda *a, **k: pd.DataFrame(columns=assignment_service.DUTY_ROW_COLUMNS))
    monkeypatch.setattr(assignment_service, "_find_paired_pilot", lambda *a, **k: None)
    monkeypatch.setattr(
        assignment_service, "search_roster",
        lambda **k: pd.DataFrame(columns=["crew_id", "duty_id", "operating_position",
                                          "role_assigned", "duty_date"]))
    monkeypatch.setattr(
        assignment_service, "get_roster_for_flight",
        lambda *a, **k: pd.DataFrame(columns=["roster_id", "crew_id", "flight_id",
                                              "operating_position", "role_assigned", "status"]))
    monkeypatch.setattr(rts, "get_instances", lambda **k: instances.copy())
    monkeypatch.setattr(rts, "get_promoted_flight_ids", lambda iid: [iid * 10, iid * 10 + 1])
    return rgs


def _preview_with_one_pilot_grounded(monkeypatch, recorder):
    """Proposes the whole window, then expires the medical of the pilot
    who is crewed on the FEWEST rotations.

    Fewest, not first: a pilot crewed on every rotation would refuse
    every rotation, and the test would then be asserting that partial
    accept writes nothing — which is exactly the false alarm that
    prompted this file. The caller asserts that the refused and written
    sets are both non-empty, so a fixture that stops producing a genuine
    partial failure says so instead of quietly inverting its own claim.
    """
    crew = ([_crew_row(f"CPT-{i:02d}", "CPT") for i in range(1, _COMMANDERS + 1)]
            + [_crew_row(f"FO-{i:02d}", "FO") for i in range(1, _SECOND_PILOTS + 1)])
    crew_frame = [pd.DataFrame(crew, columns=CREW_COLUMNS)]

    rgs = _wire(monkeypatch, crew_frame, recorder)
    preview = rgs.generate_preview(_WINDOW_START,
                                   _rotation_day(_ROTATIONS), app_user="occ1")

    appearances = {}
    for rotation in preview.rotations:
        for seat in rotation.seats.values():
            if seat.crew_id:
                appearances.setdefault(seat.crew_id, []).append(rotation.rotation_code)
    assert appearances, "nothing was proposed; the fixture is not exercising accept"

    doomed = min(appearances, key=lambda cid: len(appearances[cid]))

    grounded = crew_frame[0].copy()
    grounded.loc[grounded["crew_id"] == doomed, "medical_expiry"] = dt.date(2020, 1, 1)
    crew_frame[0] = grounded

    return rgs, preview, doomed, appearances


def test_a_refused_rotation_costs_only_itself(monkeypatch):
    """THE guarantee: rotations that pass are written and STAY written,
    even though a later one in the same accept is refused.

    Asserted against the statements actually issued, not against the
    summary the function returns — a return value can report a write
    that never reached the database.
    """
    from services.roster_generator_service import OUTCOME_WRITTEN, OUTCOME_REJECTED

    recorder = _Recorder()
    rgs, preview, doomed, appearances = _preview_with_one_pilot_grounded(monkeypatch, recorder)

    rgs.accept_preview(preview, app_user="occ1")

    written = preview.by_outcome(OUTCOME_WRITTEN)
    rejected = preview.by_outcome(OUTCOME_REJECTED)

    # Both non-empty, or this is not a PARTIAL accept and proves nothing.
    assert written, (
        f"no rotation was written — grounding {doomed} refused the whole "
        f"window, so this is not a partial accept. Appearances: {appearances}")
    assert rejected, f"no rotation was refused — {doomed} was not crewed anywhere"

    # The refused rotations are exactly the ones crewing the grounded
    # pilot, and no others were harmed by proximity to them.
    assert {r.rotation_code for r in rejected} == set(appearances[doomed])

    written_flight_ids = {fid for r in written for fid in r.flight_ids}
    rejected_flight_ids = {fid for r in rejected for fid in r.flight_ids}
    inserted_flight_ids = {fid for _, fid in recorder.inserted_roster_rows}

    assert inserted_flight_ids == written_flight_ids, (
        f"roster INSERTs do not match the rotations reported written: "
        f"inserted={sorted(inserted_flight_ids)} written={sorted(written_flight_ids)}")
    assert not (inserted_flight_ids & rejected_flight_ids), (
        "a refused rotation wrote roster rows")

    # Two seats x two sectors for every written rotation.
    assert len(recorder.inserted_roster_rows) == len(written) * 4
    assert doomed not in {cid for cid, _ in recorder.inserted_roster_rows}


def test_a_spent_preview_refuses_a_second_accept_and_writes_nothing(monkeypatch):
    """The safety rule behind "no second Accept button".

    The page expresses this by not rendering the control once a preview
    is accepted, and a Streamlit widget that is not rendered cannot be
    clicked — but that is the UI's expression of the rule, not the rule.
    The rule is here, and it holds regardless of what any page does with
    it: a preview whose provisional decisions were made against a
    database that the accept itself has since changed cannot be replayed.

    Asserted with the recorder, so "refuses" means no transaction was
    opened and no row was written — not merely that an exception came
    back after the damage.
    """
    recorder = _Recorder()
    rgs, preview, _, _ = _preview_with_one_pilot_grounded(monkeypatch, recorder)

    rgs.accept_preview(preview, app_user="occ1")
    after_first = (recorder.transactions, len(recorder.inserted_roster_rows))
    assert after_first[1] > 0, "the first accept wrote nothing; this proves nothing"

    with pytest.raises(ValueError, match="already been accepted"):
        rgs.accept_preview(preview, app_user="occ1")

    assert (recorder.transactions, len(recorder.inserted_roster_rows)) == after_first, (
        "the refused second accept still touched the database")


def test_accept_uses_one_transaction_per_rotation_not_one_for_the_window(monkeypatch):
    """The scoping question, asked directly.

    A recording engine cannot roll anything back, so the INSERT assertions
    above would pass unchanged against an implementation that wrapped the
    entire window in a single engine.begin() and lost every write when the
    refused rotation aborted it. Counting transaction boundaries is what
    tells those two implementations apart — and all-or-nothing is
    precisely the failure mode this design rejected: one pilot's changed
    circumstances must not cost a controller the rest of the window.

    One transaction per WRITTEN rotation, and none for a refused one:
    a refusal happens during validation, before any transaction opens.
    """
    from services.roster_generator_service import OUTCOME_WRITTEN

    recorder = _Recorder()
    rgs, preview, _, _ = _preview_with_one_pilot_grounded(monkeypatch, recorder)

    # The preview must not have opened one either — it writes nothing.
    assert recorder.transactions == 0, (
        f"generate_preview() opened {recorder.transactions} transaction(s)")

    rgs.accept_preview(preview, app_user="occ1")

    written = len(preview.by_outcome(OUTCOME_WRITTEN))
    assert written > 0
    assert recorder.transactions == written, (
        f"expected one transaction per written rotation ({written}); saw "
        f"{recorder.transactions}. One for the whole window would mean a "
        f"single refusal rolls back every rotation before it.")
