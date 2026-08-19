"""
tests/test_rotation_template_delete.py

migrations/019 rewrites two guard functions that have been protecting
live data since migrations/011. A rewrite of a guard is precisely where
an unintended relaxation hides, and it would not show up in any test of
the new behaviour — so roughly half of this file asserts the OLD rules
still hold, unchanged.

Tested against real Postgres rather than read off the migration's SQL,
same discipline as tests/test_schema.py and
tests/test_rotation_template_service.py.

The foreign-key audit at the top is the load-bearing one for safety:
"this template is unused" is only a true statement if the set of things
that can reference a template is the set migrations/019 checks. If a
later migration adds a table pointing at rotation_templates, the delete
path could orphan a reference, and that test fails rather than letting
it happen quietly.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

import services.rotation_template_service as rts

LEGS = [
    {"leg_order": 1, "origin": "KHI", "destination": "LHE",
     "dep_time": dt.time(19, 0), "arr_time": dt.time(20, 45),
     "flight_no": "EPE 786", "domestic": True},
    {"leg_order": 2, "origin": "LHE", "destination": "KHI",
     "dep_time": dt.time(22, 0), "arr_time": dt.time(23, 45),
     "flight_no": "EPE 787", "domestic": True},
]
DAYS = [1, 2, 3, 4, 5]


@pytest.fixture(autouse=True)
def _patch_engine(_patch_all_service_engines):
    return _patch_all_service_engines


def _create(code="EPE-786-787", effective_from=dt.date(2026, 9, 1)):
    # meal_provided/snack_provided are REQUIRED parameters of
    # create_template(), not optional ones. Omitting them made every
    # test in this file fail at setup, so the whole delete and
    # trigger-regression suite silently never ran on its first
    # real-Postgres round (2026-08-19).
    return rts.create_template(
        rotation_code=code, days_of_week=DAYS, legs=LEGS,
        effective_from=effective_from, meal_provided=True, snack_provided=True,
        app_user="occ1",
    )


def _template_id(engine, code):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT id FROM rotation_templates WHERE rotation_code = :c ORDER BY version"
        ), {"c": code}).scalars().first()


# ------------------------------------------------------------------
# The reference graph delete safety depends on
# ------------------------------------------------------------------

def test_no_unaudited_foreign_keys_reference_the_template_tables(_patch_engine):
    """migrations/019 decides "unused" by checking rotation_instances
    (and, redundantly, flights). That is only sufficient while these
    are the ONLY foreign keys pointing at the template tables. Adding a
    new referencing table without revisiting the delete rule could
    orphan a reference, so pin the set here.

    If this fails, do not just update the expected set — check whether
    rotation_template_is_deletable() needs to consider the new table
    first."""
    with _patch_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT src.relname AS referencing_table,
                   att.attname AS referencing_column,
                   tgt.relname AS referenced_table
            FROM pg_constraint c
            JOIN pg_class src ON src.oid = c.conrelid
            JOIN pg_class tgt ON tgt.oid = c.confrelid
            JOIN unnest(c.conkey) AS k(attnum) ON TRUE
            JOIN pg_attribute att ON att.attrelid = src.oid AND att.attnum = k.attnum
            WHERE c.contype = 'f'
              AND tgt.relname IN ('rotation_templates', 'rotation_template_legs')
        """)).mappings().all()

    found = {(r["referencing_table"], r["referencing_column"], r["referenced_table"])
             for r in rows}
    expected = {
        ("rotation_templates", "superseded_by", "rotation_templates"),
        ("rotation_template_legs", "template_id", "rotation_templates"),
        ("rotation_instances", "template_id", "rotation_templates"),
    }

    assert found == expected, (
        "The set of foreign keys referencing the rotation-template tables has "
        "changed. migrations/019's rotation_template_is_deletable() may no "
        "longer be sufficient to prove a template is unused.\n"
        "  unexpected: %s\n  missing: %s" % (sorted(found - expected), sorted(expected - found))
    )


# ------------------------------------------------------------------
# Pre-existing guard behaviour — must be UNCHANGED by migrations/019
# ------------------------------------------------------------------

def test_updating_a_template_leg_is_still_refused(_patch_engine):
    """The legs trigger gained a DELETE branch; its UPDATE branch must
    still be an unconditional hard block."""
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")

    with pytest.raises(DatabaseError) as excinfo:
        with _patch_engine.begin() as conn:
            conn.execute(text(
                "UPDATE rotation_template_legs SET origin = 'ISB' WHERE template_id = :id"
            ), {"id": template_id})

    assert "immutable" in str(excinfo.value)


def test_updating_a_template_leg_is_refused_even_when_the_template_is_deletable(_patch_engine):
    """The sharp edge of the rewrite: the legs trigger now permits
    DELETE when the parent is unused. An unused parent must NOT also
    make its legs editable — deletable and mutable are different
    things, and conflating them would silently make every fresh
    template's legs editable in place."""
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")
    assert rts.get_template_deletability(template_id)["deletable"] is True

    with pytest.raises(DatabaseError) as excinfo:
        with _patch_engine.begin() as conn:
            conn.execute(text(
                "UPDATE rotation_template_legs SET origin = 'ISB' WHERE template_id = :id"
            ), {"id": template_id})

    assert "immutable" in str(excinfo.value)


def test_immutable_template_columns_are_still_refused(_patch_engine):
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")

    with pytest.raises(DatabaseError) as excinfo:
        with _patch_engine.begin() as conn:
            conn.execute(text(
                "UPDATE rotation_templates SET rotation_code = 'OTHER' WHERE id = :id"
            ), {"id": template_id})

    assert "immutable" in str(excinfo.value)


def test_effective_until_can_still_be_closed_exactly_once(_patch_engine):
    """The one legitimate mutation the guard has always allowed, and
    the once-only rule that backs it. Both halves matter: create_new_
    version() depends on the first close succeeding, and the delete
    scope (sole-version only) depends on a closed version staying
    closed."""
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")

    with _patch_engine.begin() as conn:
        conn.execute(text(
            "UPDATE rotation_templates SET effective_until = :d WHERE id = :id"
        ), {"d": dt.date(2026, 12, 31), "id": template_id})

    with pytest.raises(DatabaseError) as excinfo:
        with _patch_engine.begin() as conn:
            conn.execute(text(
                "UPDATE rotation_templates SET effective_until = :d WHERE id = :id"
            ), {"d": dt.date(2027, 1, 31), "id": template_id})

    assert "exactly once" in str(excinfo.value)


# ------------------------------------------------------------------
# The new behaviour
# ------------------------------------------------------------------

def test_unused_template_can_be_deleted_with_its_legs(_patch_engine):
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")

    deleted_code = rts.delete_template(template_id, app_user="occ1")

    assert deleted_code == "EPE-786-787"
    with _patch_engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM rotation_templates WHERE id = :id"
        ), {"id": template_id}).scalar() == 0
        assert conn.execute(text(
            "SELECT COUNT(*) FROM rotation_template_legs WHERE template_id = :id"
        ), {"id": template_id}).scalar() == 0
    assert rts.get_all_rotation_codes() == []


def test_deleting_an_unused_template_writes_an_audit_record(_patch_engine):
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")

    rts.delete_template(template_id, app_user="occ1")

    with _patch_engine.connect() as conn:
        row = conn.execute(text("""
            SELECT app_user, original_state FROM audit_log
            WHERE action_type = 'ROTATION_TEMPLATE_DELETED'
        """)).mappings().first()

    assert row is not None, "a deletion must leave an audit record"
    assert row["app_user"] == "occ1"
    assert "EPE-786-787" in row["original_state"]


def test_template_with_instances_cannot_be_deleted(_patch_engine):
    """The case the guard exists for: expansion has produced drafts, so
    there is history to protect."""
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")
    rts.expand_and_persist("EPE-786-787", dt.date(2026, 9, 1), dt.date(2026, 9, 7),
                            app_user="occ1")

    assert rts.get_template_deletability(template_id)["deletable"] is False

    with pytest.raises(DatabaseError) as excinfo:
        rts.delete_template(template_id, app_user="occ1")
    assert "cannot be deleted" in str(excinfo.value)

    with _patch_engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM rotation_templates WHERE id = :id"
        ), {"id": template_id}).scalar() == 1


def test_a_refused_delete_leaves_the_legs_intact(_patch_engine):
    """delete_template() removes legs before the template, in one
    transaction. If the template delete is refused, the legs must come
    back — a partial delete would leave a template with no route, which
    is worse than either outcome."""
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")
    rts.expand_and_persist("EPE-786-787", dt.date(2026, 9, 1), dt.date(2026, 9, 7),
                            app_user="occ1")

    with pytest.raises(DatabaseError):
        rts.delete_template(template_id, app_user="occ1")

    with _patch_engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM rotation_template_legs WHERE template_id = :id"
        ), {"id": template_id}).scalar() == len(LEGS)


def test_a_superseded_version_cannot_be_deleted(_patch_engine):
    """Sole-version only. Deleting v1 of a two-version chain would
    orphan v2's history; deleting v2 would need v1's effective_until
    reopened, which the guard forbids."""
    _create()
    rts.create_new_version(
        rotation_code="EPE-786-787", days_of_week=DAYS, legs=LEGS,
        effective_from=dt.date(2026, 10, 1), meal_provided=True, snack_provided=True,
        app_user="occ1",
    )

    with _patch_engine.connect() as conn:
        ids = conn.execute(text(
            "SELECT id FROM rotation_templates WHERE rotation_code = :c ORDER BY version"
        ), {"c": "EPE-786-787"}).scalars().all()

    for template_id in ids:
        assert rts.get_template_deletability(template_id)["deletable"] is False
        with pytest.raises(DatabaseError):
            rts.delete_template(template_id, app_user="occ1")

    with _patch_engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM rotation_templates WHERE rotation_code = :c"
        ), {"c": "EPE-786-787"}).scalar() == 2


def test_deletability_reason_names_the_blocking_cause(_patch_engine):
    """The UI shows this string in place of the disabled control, so it
    has to say which of the two rules applied."""
    _create()
    template_id = _template_id(_patch_engine, "EPE-786-787")
    rts.expand_and_persist("EPE-786-787", dt.date(2026, 9, 1), dt.date(2026, 9, 7),
                            app_user="occ1")

    result = rts.get_template_deletability(template_id)

    assert result["deletable"] is False
    assert result["instance_count"] > 0
    assert "rotation instance" in result["reason"]


def test_deleting_a_missing_template_raises_valueerror(_patch_engine):
    with pytest.raises(ValueError):
        rts.delete_template(999999, app_user="occ1")


def test_a_deleted_rotation_code_can_be_created_again(_patch_engine):
    """The point of the whole feature: recovering from a mistaken
    creation means being able to create it properly afterwards, under
    the same code."""
    _create()
    rts.delete_template(_template_id(_patch_engine, "EPE-786-787"), app_user="occ1")

    _create()

    assert rts.get_all_rotation_codes() == ["EPE-786-787"]
