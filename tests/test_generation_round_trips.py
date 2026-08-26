"""
tests/test_generation_round_trips.py

Counts DATABASE ROUND-TRIPS during roster generation, and asserts the
count does not grow quadratically with crew pool size.

WHY THIS EXISTS. On 2026-08-22 roster generation was measured taking
7+ minutes on the deployed app for 7 rotations, against an estimate of
23 seconds. It was not stuck and nothing was slow: it issued **4,822
database round-trips to fill 10 seats**, roughly 480 per seat. Locally
that is 7.2 seconds, because a round-trip to a local database costs
microseconds. Against Supabase from Streamlit Cloud each one carries
50-300ms of network latency, which is where the minutes came from.

647 tests passed throughout. **Nothing in the suite measured round-trip
count**, and count is the only environment-independent measure of this
defect — a timing assertion cannot see it, because locally there is
nothing to see.

NO DATABASE NEEDED, deliberately. The leaf functions that each issue
exactly one query are replaced with counting fakes, and the real
orchestration runs on top of them. The fixture makes every candidate
fail the qualification gate, which means the search performs the FULL
C x S scan (the case that matters) and never reaches a write — so the
whole thing runs in any environment, including the ones where this
defect was invisible.

The authoritative count still comes from a `before_cursor_execute`
listener against real Postgres. This is the guard that fails on the
machine that introduces the regression.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

CREW_COLUMNS = [
    "crew_id", "name", "role", "base", "is_active", "date_of_birth",
    "operator_staff_id", "license_expiry", "medical_expiry", "ir_expiry",
    "sim_expiry", "route_check_expiry", "sep_expiry", "crm_expiry", "dg_expiry",
]

_LONG_AGO = dt.date(2020, 1, 1)          # every document expired
_ROTATION_DATE = dt.date(2026, 9, 1)


def _crew_row(crew_id, role, dob_year=1985):
    """A crew member whose documents have ALL expired, so the gate
    rejects them. That is what makes the search run to exhaustion —
    the worst case, and the one worth budgeting."""
    row = {c: None for c in CREW_COLUMNS}
    row.update({
        "crew_id": crew_id, "name": f"Test {crew_id}", "role": role,
        "base": "KHI", "is_active": True,
        "date_of_birth": dt.date(dob_year, 1, 1),
        "license_expiry": _LONG_AGO, "medical_expiry": _LONG_AGO,
        "ir_expiry": _LONG_AGO, "sim_expiry": _LONG_AGO,
        "route_check_expiry": _LONG_AGO, "sep_expiry": _LONG_AGO,
        "crm_expiry": _LONG_AGO, "dg_expiry": _LONG_AGO,
    })
    return row


def _flight_row(flight_id):
    """One sector of Air Eagle's real two-sector domestic rotation.
    flight_ids are allocated as (n*10, n*10+1), so the low digit says
    which leg this is — the legs must be chronological, continuous
    (destination -> next origin) and non-overlapping or build_duty()
    rejects the duty before any candidate is even considered."""
    leg = flight_id % 10
    if leg == 0:
        origin, destination = "KHI", "LHE"
        dep, arr = dt.time(19, 0), dt.time(20, 45)
    else:
        origin, destination = "LHE", "KHI"
        dep, arr = dt.time(22, 0), dt.time(23, 45)
    return {
        "flight_id": flight_id, "flight_no": f"EPE {700 + flight_id}",
        "origin": origin, "destination": destination,
        "dep_time_planned": dt.datetime.combine(_ROTATION_DATE, dep),
        "arr_time_planned": dt.datetime.combine(_ROTATION_DATE, arr),
        "dep_time_actual": None, "arr_time_actual": None,
        "domestic": True, "status": "PLANNED", "meal_provided": True,
        "snack_provided": True, "cargo_dg": False,
    }


class RoundTrips:
    """Counts calls to the leaf functions that each cost exactly one
    database round-trip."""

    def __init__(self):
        self.by_source = {}

    def hit(self, source):
        self.by_source[source] = self.by_source.get(source, 0) + 1

    @property
    def total(self):
        return sum(self.by_source.values())

    def __repr__(self):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.by_source.items()))
        return f"<{self.total} round-trips: {parts}>"


class _NoEngine:
    """Raises on ANY use. An unpatched query path then fails loudly here
    instead of quietly opening a connection — which, on a machine whose
    .env points at production, means quietly WRITING to it."""

    def __getattr__(self, name):
        raise AssertionError(
            f"reached the database (engine.{name}) — a query path is "
            f"unpatched, so this test is not measuring what it claims to, "
            f"and against a live .env it would be writing to that database"
        )


def isolate_from_database(monkeypatch, counts=None):
    """Cut EVERY route to a database, on every module that holds one.

    THE LESSON OF 2026-08-26. These modules do `from db.db import
    get_engine` and `from services.audit_service import log_audit`,
    which binds a COPY into each module's namespace. Patching
    `audit_service.log_audit` does nothing to
    `assignment_service.log_audit`; patching `db.db.get_engine` does
    nothing to any of them. An earlier version patched two modules by
    name and missed assignment_service's own copy of log_audit — through
    which every REJECTED candidate pair wrote a real
    PAIR_ASSIGNMENT_REJECTED row. On a machine with no .env that showed
    as "DATABASE_URL not set"; on one whose .env pointed at production
    it silently wrote 2,954 rows into the live audit trail.

    Enumerating modules is what makes this complete rather than
    whack-a-mole: anything holding the attribute gets it replaced, so a
    new service joins the net automatically.
    """
    from services import (assignment_service, audit_service, crew_service,
                          flight_service, roster_generator_service,
                          rotation_template_service)

    for module in (assignment_service, audit_service, crew_service, flight_service,
                   roster_generator_service, rotation_template_service):
        if hasattr(module, "get_engine"):
            monkeypatch.setattr(module, "get_engine", lambda: _NoEngine())
        if hasattr(module, "log_audit"):
            # An audit row is an INSERT, i.e. a round-trip — and one is
            # written per REJECTED candidate pair, so it is counted
            # rather than merely silenced.
            monkeypatch.setattr(
                module, "log_audit",
                (lambda *a, **k: counts.hit("audit_write")) if counts is not None
                else (lambda *a, **k: None))


def run_generation(monkeypatch, commanders, second_pilots, rotations=1):
    """Runs the real generate_for_window() over fake leaves, returning
    (summary, RoundTrips)."""
    from services import assignment_service, crew_service, flight_service
    from services import roster_generator_service as rgs
    from services import rotation_template_service as rts

    counts = RoundTrips()

    crew = ([_crew_row(f"CPT-{i:02d}", "CPT") for i in range(1, commanders + 1)]
            + [_crew_row(f"FO-{i:02d}", "FO") for i in range(1, second_pilots + 1)])
    crew_df = pd.DataFrame(crew, columns=CREW_COLUMNS)
    by_id = {r["crew_id"]: pd.Series(r) for r in crew}

    instances = pd.DataFrame([
        {"id": i, "rotation_code": f"EPE-{i}", "rotation_date": _ROTATION_DATE,
         "status": "APPROVED", "template_id": 1, "version": 1}
        for i in range(1, rotations + 1)
    ])

    def counted(source, value):
        def fn(*args, **kwargs):
            counts.hit(source)
            return value() if callable(value) else value
        return fn

    # --- leaves: one query each ---
    monkeypatch.setattr(crew_service, "get_all_crew",
                        counted("get_all_crew", lambda: crew_df.copy()))
    monkeypatch.setattr(crew_service, "get_crew",
                        lambda cid: (counts.hit("get_crew"), by_id.get(cid))[1])
    monkeypatch.setattr(flight_service, "get_flight",
                        lambda fid: (counts.hit("get_flight"), pd.Series(_flight_row(fid)))[1])
    # Patch the SEAM, not _fetch_duty_rows itself: the real caching
    # in that function must run, or the test measures a cache it
    # reimplemented rather than the one that ships.
    monkeypatch.setattr(assignment_service, "_read_duty_rows",
                        lambda *a, **k: (counts.hit("duty_history_query"), pd.DataFrame())[1])
    monkeypatch.setattr(assignment_service, "_find_paired_pilot",
                        lambda *a, **k: (counts.hit("find_paired_pilot"), None)[1])
    monkeypatch.setattr(assignment_service, "search_roster",
                        lambda **k: (counts.hit("search_roster"),
                                     pd.DataFrame(columns=["crew_id", "duty_id", "operating_position",
                                                            "role_assigned", "duty_date"]))[1])
    # Column names matter: _seat_occupant() indexes them even when
    # the frame is empty.
    empty_roster = pd.DataFrame(columns=["roster_id", "crew_id", "flight_id",
                                          "operating_position", "role_assigned", "status"])
    monkeypatch.setattr(assignment_service, "get_roster_for_flight",
                        lambda *a, **k: (counts.hit("get_roster_for_flight"), empty_roster.copy())[1])
    monkeypatch.setattr(rts, "get_instances",
                        counted("get_instances", lambda: instances.copy()))
    monkeypatch.setattr(rts, "get_promoted_flight_ids",
                        lambda iid: (counts.hit("get_promoted_flight_ids"), [iid * 10, iid * 10 + 1])[1])
    isolate_from_database(monkeypatch, counts)

    monkeypatch.setattr(rgs, "_record_uncovered", lambda *a, **k: None)

    summary = rgs.generate_for_window(_ROTATION_DATE, _ROTATION_DATE, app_user="occ1")
    return summary, counts


def test_generation_makes_no_query_per_candidate_for_ages(monkeypatch):
    """_age_of() issued one round-trip per candidate, per seat, purely
    to read a birthday — data already loaded in the crew snapshot. In
    the pair search the second-pilot list was rebuilt inside the
    commander loop, so this cost C + C x (1 + S) per rotation before any
    legality check ran."""
    _, counts = run_generation(monkeypatch, commanders=6, second_pilots=4)

    # get_all_crew is the ONE legitimate crew read for the whole run.
    assert counts.by_source.get("get_all_crew", 0) == 1, counts


def test_round_trips_do_not_grow_quadratically_with_pool_size(monkeypatch):
    """THE test that catches the defect class.

    A single-point budget catches "it got worse". It does NOT catch a
    reintroduced C x S loop, because a small fixture keeps the absolute
    number low. Growth is the property that actually broke: adding one
    pilot must add a bounded amount of work, not a multiple of the
    other pool.

    Compares a pool one larger on BOTH sides, so a quadratic term shows
    up as a super-linear jump."""
    _, small = run_generation(monkeypatch, commanders=3, second_pilots=3)
    _, large = run_generation(monkeypatch, commanders=6, second_pilots=6)

    # READ round-trips only. Audit WRITES are excluded here and covered
    # by their own test below, because they are still O(C x S) and that
    # is an open product question rather than a regression to gate on.
    #
    # Measured 2026-08-22: reads went 15 -> 21 (1.4x) with the fix and
    # 129 -> 537 (4.16x) without. A 2x ceiling sits cleanly between them.
    small_reads = small.total - small.by_source.get("audit_write", 0)
    large_reads = large.total - large.by_source.get("audit_write", 0)
    assert large_reads <= small_reads * 2, (
        f"READ round-trips grew super-linearly with pool size — "
        f"small={small!r} large={large!r}"
    )


def test_round_trip_budget_for_a_pinned_pool(monkeypatch):
    """Absolute ceiling against a PINNED pool, so the number means
    something. Air Eagle's real pool is 6 commanders and 10
    second-pilot-eligible pilots; this uses a smaller fixed pool so the
    ceiling does not move when the airline hires someone.

    If this fails, print the counter — it attributes the round-trips by
    source, which is how the 4,822 was diagnosed in the first place.

    Measured at 15 for this fixture (2026-08-22), of which C + S = 6 are
    the one duty-history query per crew member per rotation. 25 leaves
    room for an honest extra read without hiding a regression."""
    _, counts = run_generation(monkeypatch, commanders=3, second_pilots=3)

    reads = counts.total - counts.by_source.get("audit_write", 0)
    assert reads <= 25, counts


# ------------------------------------------------------------------
# The ad-hoc pair path (Control Room) — DB-free smoke
# ------------------------------------------------------------------

def test_adhoc_pair_path_runs_without_a_stray_name(monkeypatch):
    """assign_pair_to_new_flights() shipped a NameError on 2026-08-22:
    it referenced `prefetch`, which it never received, after a global
    edit matched two functions with identical crew-fetch shapes. The ad-
    hoc pair path was completely broken — Control Room could not assign
    crew and showed a raw exception.

    It surfaced only on real Postgres, because every test of that path
    is DB-gated and skips wherever Postgres is absent. This one is not:
    the crew rows have expired documents, so validation REJECTS before
    any flight is written, which exercises the whole read path without
    needing a database.

    A NameError on a rarely-taken branch is exactly what a smoke test is
    for — it needs no assertion beyond "it ran"."""
    from services import assignment_service, crew_service

    crew = {"CPT-01": pd.Series(_crew_row("CPT-01", "CPT")),
            "FO-01": pd.Series(_crew_row("FO-01", "FO"))}

    isolate_from_database(monkeypatch)
    monkeypatch.setattr(crew_service, "get_crew", lambda cid: crew.get(cid))
    monkeypatch.setattr(assignment_service, "_read_duty_rows",
                        lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(assignment_service, "_find_paired_pilot", lambda *a, **k: None)

    flights_data = [{
        "origin": "KHI", "destination": "LHE",
        "dep_time_planned": dt.datetime.combine(_ROTATION_DATE, dt.time(19, 0)),
        "arr_time_planned": dt.datetime.combine(_ROTATION_DATE, dt.time(20, 45)),
        "domestic": True,
    }]

    result, flight_ids = assignment_service.assign_pair_to_new_flights(
        "CPT-01", "FO-01", flights_data, app_user="occ1")

    # Expired documents, so nothing may be written.
    assert result.status in ("REJECTED", "NEEDS_REVIEW"), result.status
    assert not flight_ids


@pytest.mark.xfail(
    reason="OPEN: one PAIR_ASSIGNMENT_REJECTED audit row is written per "
           "rejected candidate pair, so audit writes are still O(C x S). "
           "Whether a speculative candidate trial deserves a permanent "
           "audit row is an operator decision, not a refactor — see "
           "HANDOVER.md 2026-08-26.",
    strict=True,
)
def test_audit_writes_do_not_grow_quadratically(monkeypatch):
    """The remaining quadratic term, deliberately left FAILING so it
    stays visible.

    Generation trials every candidate pair, and each rejection writes an
    audit row — an INSERT, i.e. a round-trip. For Air Eagle's real pool
    that is 54 writes per uncrewed rotation, and it is what made the
    production audit_log grow by 2,954 rows in a morning.

    Marked xfail(strict=True) rather than deleted or loosened: strict
    means it FAILS if someone fixes the underlying issue without
    removing the marker, so this cannot rot into a permanently ignored
    test. It is a tracked defect, not an accepted one.
    """
    _, small = run_generation(monkeypatch, commanders=3, second_pilots=3)
    _, large = run_generation(monkeypatch, commanders=6, second_pilots=6)

    assert large.by_source["audit_write"] <= small.by_source["audit_write"] * 2, (
        f"audit writes grew super-linearly — small={small!r} large={large!r}"
    )
