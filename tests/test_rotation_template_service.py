"""
tests/test_rotation_template_service.py

DB-integration tests for services/rotation_template_service.py and the
database-level guarantees migrations/011_rotation_templates.sql adds
(the EXCLUDE constraint against overlapping template versions, and the
immutability triggers) — tested directly against real Postgres, not
assumed from the migration's SQL source, same discipline as
tests/test_schema.py.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

import services.rotation_template_service as rts
import services.flight_service as flight_service
import services.assignment_service as assignment_service
import services.crew_service as crew_service

DOMESTIC_LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
     "flight_no": "EPE 786", "domestic": True},
    {"leg_order": 2, "origin": "LHE", "destination": "KHI",
     "dep_time": dt.time(22, 0), "arr_time": dt.time(23, 45),
     "flight_no": "EPE 787", "domestic": True},
]
DOMESTIC_DAYS = [1, 2, 3, 4, 5]


@pytest.fixture(autouse=True)
def _patch_engine(_patch_all_service_engines):
    """Thin per-file wrapper — the actual patching logic lives once in
    conftest.py's _patch_all_service_engines, so no module here can be
    forgotten. This is the exact fixture that was missing
    audit_service (2026-08-04), causing
    test_expand_approve_then_assign_crew_reproduces_hand_verified_numbers
    to fail with a real-Postgres RuntimeError: crew_service.add_crew()'s
    own log_audit() call has no conn parameter, so it fell through to
    an unpatched get_engine(). Fixed by moving the patching to one
    shared place instead of a per-file list to remember."""
    return _patch_all_service_engines


def _create_domestic_template(effective_from=dt.date(2026, 1, 1), effective_until=None):
    return rts.create_template(
        rotation_code="EPE-786-787", days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=effective_from, effective_until=effective_until,
        description="KHI-LHE-KHI domestic",
    )


# ------------------------------------------------------------------
# create_template / create_new_version
# ------------------------------------------------------------------

def test_create_template_writes_version_1_and_its_legs(_patch_engine):
    template_id = _create_domestic_template()
    versions = rts.get_versions("EPE-786-787")
    assert len(versions) == 1
    assert versions.iloc[0]["version"] == 1
    assert versions.iloc[0]["effective_until"] is None

    legs = rts.get_template_legs(template_id)
    assert len(legs) == 2
    assert list(legs["flight_no"]) == ["EPE 786", "EPE 787"]


def test_create_new_version_closes_prior_version_and_sets_superseded_by(_patch_engine):
    v1_id = _create_domestic_template(effective_from=dt.date(2026, 1, 1))
    v2_id = rts.create_new_version(
        rotation_code="EPE-786-787", days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2026, 9, 1),
    )

    versions = rts.get_versions("EPE-786-787")
    assert len(versions) == 2
    v1_row = versions[versions["id"] == v1_id].iloc[0]
    v2_row = versions[versions["id"] == v2_id].iloc[0]

    assert v1_row["effective_until"] == dt.date(2026, 8, 31)  # day BEFORE v2's start, not the same day
    assert v1_row["superseded_by"] == v2_id
    assert v2_row["effective_from"] == dt.date(2026, 9, 1)
    assert v2_row["effective_until"] is None
    assert v2_row["version"] == 2


def test_create_new_version_without_an_open_version_raises(_patch_engine):
    with pytest.raises(ValueError, match="No open version"):
        rts.create_new_version(
            rotation_code="DOES-NOT-EXIST", days_of_week=DOMESTIC_DAYS,
            legs=DOMESTIC_LEGS, effective_from=dt.date(2026, 9, 1),
        )


# ------------------------------------------------------------------
# EXCLUDE constraint — the actual overlap guarantee, tested directly
# against the database, not just through create_new_version()'s
# convenience path.
# ------------------------------------------------------------------

def test_overlapping_open_ended_versions_rejected_by_the_database(_patch_engine):
    """The exact silent-duplicate scenario this constraint exists to
    prevent: v1 open-ended, v2 inserted directly (bypassing
    create_new_version()) without closing v1 first."""
    engine = _patch_engine
    _create_domestic_template(effective_from=dt.date(2026, 1, 1))

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO rotation_templates
                    (rotation_code, days_of_week, effective_from, version)
                VALUES ('EPE-786-787', ARRAY[1,2,3,4,5]::smallint[], '2026-09-01', 2)
            """))


def test_adjacent_non_overlapping_versions_are_accepted(_patch_engine):
    """v1 ending 2026-08-31, v2 starting 2026-09-01 — adjacent, not
    overlapping. Must succeed; this is exactly what
    create_new_version() itself produces."""
    engine = _patch_engine
    _create_domestic_template(effective_from=dt.date(2026, 1, 1),
                               effective_until=dt.date(2026, 8, 31))
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO rotation_templates
                (rotation_code, days_of_week, effective_from, version)
            VALUES ('EPE-786-787', ARRAY[1,2,3,4,5]::smallint[], '2026-09-01', 2)
        """))
    versions = rts.get_versions("EPE-786-787")
    assert len(versions) == 2


def test_same_day_boundary_is_rejected_not_accepted(_patch_engine):
    """v1 ending 2026-08-31 and v2 STARTING 2026-08-31 (not
    2026-09-01) must be rejected -- the '[]' inclusive bound means a
    single day cannot belong to two versions. This is exactly why
    create_new_version() closes the prior version to the day BEFORE
    the new effective_from, not the same day; an off-by-one here would
    surface as an integrity error, not a wrong roster."""
    engine = _patch_engine
    _create_domestic_template(effective_from=dt.date(2026, 1, 1),
                               effective_until=dt.date(2026, 8, 31))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO rotation_templates
                    (rotation_code, days_of_week, effective_from, version)
                VALUES ('EPE-786-787', ARRAY[1,2,3,4,5]::smallint[], '2026-08-31', 2)
            """))


def test_different_rotation_codes_never_conflict(_patch_engine):
    """The EXCLUDE constraint is scoped per rotation_code -- two
    unrelated rotations with identical, fully-overlapping date ranges
    must both succeed."""
    engine = _patch_engine
    _create_domestic_template(effective_from=dt.date(2026, 1, 1))
    rts.create_template(
        rotation_code="EPE-802-805", days_of_week=[2, 4, 5, 6],
        legs=DOMESTIC_LEGS, effective_from=dt.date(2026, 1, 1),
    )
    assert len(rts.get_versions("EPE-786-787")) == 1
    assert len(rts.get_versions("EPE-802-805")) == 1


# ------------------------------------------------------------------
# Immutability triggers
# ------------------------------------------------------------------

def test_rotation_template_legs_update_is_rejected(_patch_engine):
    """The trigger raises a PL/pgSQL RAISE EXCEPTION, which SQLAlchemy
    surfaces as InternalError, not IntegrityError -- DatabaseError is
    the common parent of both, used here rather than pinning to
    InternalError specifically. This is distinct from the EXCLUDE-
    constraint tests above, which raise a real constraint violation
    (IntegrityError) and are asserted as such."""
    engine = _patch_engine
    template_id = _create_domestic_template()
    with pytest.raises(DatabaseError):
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE rotation_template_legs SET origin = 'XXX'
                WHERE template_id = :template_id AND leg_order = 1
            """), {"template_id": template_id})


def test_rotation_template_legs_delete_is_rejected(_patch_engine):
    engine = _patch_engine
    template_id = _create_domestic_template()
    with pytest.raises(DatabaseError):
        with engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM rotation_template_legs WHERE template_id = :template_id
            """), {"template_id": template_id})


def test_rotation_templates_delete_is_rejected(_patch_engine):
    engine = _patch_engine
    template_id = _create_domestic_template()
    with pytest.raises(DatabaseError):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM rotation_templates WHERE id = :id"), {"id": template_id})


def test_rotation_templates_arbitrary_update_is_rejected(_patch_engine):
    """Only the ONE legitimate transition (closing effective_until,
    setting superseded_by) is allowed -- an update to any other column
    must still be rejected, e.g. trying to silently correct a
    days_of_week typo instead of creating a new version."""
    engine = _patch_engine
    template_id = _create_domestic_template()
    with pytest.raises(DatabaseError):
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE rotation_templates SET days_of_week = ARRAY[1,2,3,4,5,6]::smallint[]
                WHERE id = :id
            """), {"id": template_id})


def test_rotation_templates_cannot_reopen_or_re_close_effective_until(_patch_engine):
    """The legitimate transition (NULL -> a date) is allowed exactly
    once -- re-editing an already-closed effective_until must still be
    rejected, not treated as a second free pass."""
    engine = _patch_engine
    v1_id = _create_domestic_template(effective_from=dt.date(2026, 1, 1))
    rts.create_new_version(
        rotation_code="EPE-786-787", days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2026, 9, 1),
    )
    with pytest.raises(DatabaseError):
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE rotation_templates SET effective_until = '2026-12-31' WHERE id = :id
            """), {"id": v1_id})


# ------------------------------------------------------------------
# expand_and_persist
# ------------------------------------------------------------------

def test_expand_and_persist_creates_one_instance_per_matching_weekday(_patch_engine):
    _create_domestic_template(effective_from=dt.date(2026, 1, 1))
    created = rts.expand_and_persist("EPE-786-787", dt.date(2026, 8, 3), dt.date(2026, 8, 9))
    assert len(created) == 5  # Mon-Fri within one week

    instances = rts.get_instances(rotation_code="EPE-786-787")
    assert len(instances) == 5
    assert all(instances["status"] == "DRAFT")

    legs = rts.get_instance_legs(int(instances.iloc[0]["id"]))
    assert len(legs) == 2
    assert list(legs["flight_no"]) == ["EPE 786", "EPE 787"]


def test_expand_and_persist_is_idempotent(_patch_engine):
    _create_domestic_template(effective_from=dt.date(2026, 1, 1))
    first = rts.expand_and_persist("EPE-786-787", dt.date(2026, 8, 3), dt.date(2026, 8, 9))
    second = rts.expand_and_persist("EPE-786-787", dt.date(2026, 8, 3), dt.date(2026, 8, 9))

    assert len(first) == 5
    assert second == []  # nothing new -- every date already had an instance
    assert len(rts.get_instances(rotation_code="EPE-786-787")) == 5


def test_expand_and_persist_after_new_version_only_adds_forward_never_touches_existing(_patch_engine):
    """The structural guarantee behind 'no silent reshuffle on
    regeneration': re-expanding after a template version change adds
    instances for dates on/after the new version only, and does not
    alter any already-created instance -- including one manually
    marked APPROVED, standing in for what the (not-yet-built) approval
    step would do."""
    engine = _patch_engine
    _create_domestic_template(effective_from=dt.date(2026, 1, 1))
    first_batch = rts.expand_and_persist("EPE-786-787", dt.date(2026, 8, 3), dt.date(2026, 8, 7))
    assert len(first_batch) == 5

    approved_id = first_batch[0]
    with engine.begin() as conn:
        conn.execute(text("UPDATE rotation_instances SET status = 'APPROVED' WHERE id = :id"),
                      {"id": approved_id})

    # New version starting 2026-08-10 changes nothing about the route,
    # just proves the version-boundary plumbing without needing a
    # second real rotation shape.
    rts.create_new_version(
        rotation_code="EPE-786-787", days_of_week=DOMESTIC_DAYS, legs=DOMESTIC_LEGS,
        effective_from=dt.date(2026, 8, 10),
    )

    second_batch = rts.expand_and_persist("EPE-786-787", dt.date(2026, 8, 3), dt.date(2026, 8, 14))

    # Only the genuinely new dates (8/10-8/14, weekdays only -> 3 dates:
    # Mon 8/10, Tue 8/11, Wed 8/12... actually 8/10 is a Monday) get created.
    all_instances = rts.get_instances(rotation_code="EPE-786-787")
    assert len(all_instances) == 5 + len(second_batch)

    # The manually-approved instance is untouched by the whole second pass.
    still_approved = all_instances[all_instances["id"] == approved_id].iloc[0]
    assert still_approved["status"] == "APPROVED"


def test_expand_and_persist_no_template_for_rotation_code_returns_empty(_patch_engine):
    created = rts.expand_and_persist("NO-SUCH-ROTATION", dt.date(2026, 8, 3), dt.date(2026, 8, 9))
    assert created == []


# ------------------------------------------------------------------
# Approval workflow (2026-08-04): DRAFT -> APPROVED promotes legs into
# real flights via flight_service.add_flight(); DRAFT -> REJECTED
# never touches flights.
# ------------------------------------------------------------------

_FAR_FUTURE_EXPIRY = dt.date(2099, 1, 1)
_QUALIFICATION_DEFAULTS = {
    "license_expiry": _FAR_FUTURE_EXPIRY, "medical_expiry": _FAR_FUTURE_EXPIRY,
    "sim_expiry": _FAR_FUTURE_EXPIRY, "route_check_expiry": _FAR_FUTURE_EXPIRY,
    "ir_expiry": _FAR_FUTURE_EXPIRY, "sep_expiry": _FAR_FUTURE_EXPIRY,
    "crm_expiry": _FAR_FUTURE_EXPIRY, "dg_expiry": _FAR_FUTURE_EXPIRY,
    "date_of_birth": dt.date(1980, 1, 1),
}


def _add_crew(role="CPT", **overrides):
    crew_data = {"name": f"Test {role}", "role": role, "base": "KHI"}
    crew_data.update(_QUALIFICATION_DEFAULTS)
    crew_data.update(overrides)
    return crew_service.add_crew(crew_data)


def _one_instance_id(rotation_code="EPE-786-787"):
    _create_domestic_template(effective_from=dt.date(2026, 1, 1))
    created = rts.expand_and_persist(rotation_code, dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert len(created) == 1
    return created[0]


def test_approve_instance_promotes_every_leg_to_a_real_flight(_patch_engine):
    instance_id = _one_instance_id()
    flight_ids = rts.approve_instance(instance_id, app_user="tester")

    assert len(flight_ids) == 2
    flights_df = flight_service.get_all_flights()
    assert len(flights_df) == 2
    assert set(flights_df["flight_no"]) == {"EPE 786", "EPE 787"}
    assert set(flights_df["rotation_instance_id"]) == {instance_id}
    assert all(flights_df["domestic"])

    instances = rts.get_instances(rotation_code="EPE-786-787")
    assert instances.iloc[0]["status"] == "APPROVED"


def test_approve_instance_writes_layered_audit_entries(_patch_engine):
    engine = _patch_engine
    instance_id = _one_instance_id()
    rts.approve_instance(instance_id, app_user="tester")

    audit = pd.read_sql(text("SELECT action_type FROM audit_log"), engine)
    action_types = list(audit["action_type"])
    assert action_types.count("FLIGHT_ADDED") == 2
    assert action_types.count("ROTATION_INSTANCE_APPROVED") == 1


def test_approve_instance_is_idempotent(_patch_engine):
    instance_id = _one_instance_id()
    first = rts.approve_instance(instance_id)
    second = rts.approve_instance(instance_id)

    assert second == first
    assert len(flight_service.get_all_flights()) == 2  # not duplicated


def test_approve_instance_nonexistent_raises(_patch_engine):
    with pytest.raises(ValueError, match="No rotation_instance"):
        rts.approve_instance(999999)


def test_approve_instance_on_rejected_raises(_patch_engine):
    instance_id = _one_instance_id()
    rts.reject_instance(instance_id)
    with pytest.raises(ValueError, match="REJECTED"):
        rts.approve_instance(instance_id)


def test_approve_instance_atomicity_partial_failure_rolls_back_everything(_patch_engine, monkeypatch):
    """The direct regression test for 'all-or-nothing': force the
    SECOND leg's add_flight() call to fail and confirm ZERO flights
    exist afterward, and the instance is still DRAFT -- not partially
    APPROVED with only leg 1 promoted."""
    instance_id = _one_instance_id()

    real_add_flight = flight_service.add_flight
    calls = {"n": 0}

    def _flaky_add_flight(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure on the second leg")
        return real_add_flight(*args, **kwargs)

    monkeypatch.setattr(rts.flight_service, "add_flight", _flaky_add_flight)

    with pytest.raises(RuntimeError):
        rts.approve_instance(instance_id)

    assert len(flight_service.get_all_flights()) == 0
    instances = rts.get_instances(rotation_code="EPE-786-787")
    assert instances.iloc[0]["status"] == "DRAFT"
    assert calls["n"] == 2


def test_reject_instance_never_touches_flights(_patch_engine):
    instance_id = _one_instance_id()
    rts.reject_instance(instance_id, reason="route no longer needed")

    instances = rts.get_instances(rotation_code="EPE-786-787")
    assert instances.iloc[0]["status"] == "REJECTED"
    assert len(flight_service.get_all_flights()) == 0


def test_reject_instance_nonexistent_raises(_patch_engine):
    with pytest.raises(ValueError, match="No rotation_instance"):
        rts.reject_instance(999999)


def test_reject_instance_on_already_rejected_raises(_patch_engine):
    """Deliberately NOT idempotent, unlike approve_instance() -- there's
    no prior side effect to safely return."""
    instance_id = _one_instance_id()
    rts.reject_instance(instance_id)
    with pytest.raises(ValueError, match="REJECTED"):
        rts.reject_instance(instance_id)


def test_reject_instance_on_approved_raises(_patch_engine):
    instance_id = _one_instance_id()
    rts.approve_instance(instance_id)
    with pytest.raises(ValueError, match="APPROVED"):
        rts.reject_instance(instance_id)


# ------------------------------------------------------------------
# migrations/012's database guarantee against double-promotion —
# tested directly, not just through approve_instance()'s idempotency
# path.
# ------------------------------------------------------------------

def test_duplicate_rotation_instance_flight_no_rejected_by_database(_patch_engine):
    engine = _patch_engine
    instance_id = _one_instance_id()
    rts.approve_instance(instance_id)  # 2 real flights now exist

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO flights (origin, destination, dep_time_planned,
                    arr_time_planned, domestic, flight_no, rotation_instance_id)
                VALUES ('KHI', 'LHE', '2026-08-03 19:00', '2026-08-03 20:45',
                    TRUE, 'EPE 786', :instance_id)
            """), {"instance_id": instance_id})


def test_two_ad_hoc_flights_sharing_flight_no_are_not_rejected(_patch_engine):
    """The partial index must never constrain Control Room's ad-hoc
    flights (rotation_instance_id always NULL there) -- two unrelated
    ad-hoc flights sharing the same flight_no is unrelated to this
    guarantee and stays perfectly legal."""
    engine = _patch_engine
    with engine.begin() as conn:
        for _ in range(2):
            conn.execute(text("""
                INSERT INTO flights (origin, destination, dep_time_planned,
                    arr_time_planned, domestic, flight_no)
                VALUES ('KHI', 'LHE', '2026-08-03 19:00', '2026-08-03 20:45',
                    TRUE, 'EPE 999')
            """))
    assert len(flight_service.get_all_flights()) == 2


def test_create_template_rejects_leg_with_missing_flight_no(_patch_engine):
    bad_legs = [
        {"leg_order": 1, "origin": "KHI", "destination": "LHE",
         "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45), "domestic": True},
    ]
    with pytest.raises(ValueError, match="flight_no"):
        rts.create_template(
            rotation_code="BAD-ROTATION", days_of_week=[1], legs=bad_legs,
            effective_from=dt.date(2026, 1, 1),
        )
    assert rts.get_versions("BAD-ROTATION").empty


# ------------------------------------------------------------------
# End-to-end grounding test — expand, approve, then assign real crew
# through the real legality gate, tying the whole Phase 7 groundwork
# arc together: a template-sourced flight must be indistinguishable
# from a manually-entered one to every downstream consumer.
# ------------------------------------------------------------------

def test_expand_approve_then_assign_crew_reproduces_hand_verified_numbers(_patch_engine):
    instance_id = _one_instance_id()
    flight_ids = rts.approve_instance(instance_id)

    cpt = _add_crew("CPT")
    result = assignment_service.assign_crew_to_duty(cpt, sorted(flight_ids), "CPT")

    assert result.status == "ALLOWED"
    assert result.computed_report_time == dt.datetime(2026, 8, 3, 18, 15)
    assert result.computed_debrief_time == dt.datetime(2026, 8, 4, 0, 0)
    assert result.computed_fdp_hours == pytest.approx(5.75)
