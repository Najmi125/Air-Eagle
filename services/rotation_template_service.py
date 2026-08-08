"""
services/rotation_template_service.py

Canonical write path for rotation_templates, rotation_template_legs,
rotation_instances, and rotation_instance_legs (migrations/011, 012).
Also the write path for the approval workflow that promotes an
APPROVED rotation_instance's legs into real flights rows — via the
existing flight_service.add_flight(), never a direct INSERT here;
`flights` stays owned exclusively by flight_service.py per the
Ownership Table, this file only calls into it.

Two immutability/no-overlap guarantees for rotation_templates/
rotation_template_legs live at the DATABASE level (migrations/011's
EXCLUDE constraint and triggers), not just here — this service's
ordering (insert the new version, then close the old one; never
expose an update/delete for template legs) is the convenient path that
satisfies those constraints, not the mechanism that enforces them. A
third guarantee, against double-promotion, lives at the database level
too (migrations/012's partial unique index on flights). See both
migration files' comments for the actual guarantees.
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional, Sequence

import pandas as pd
from sqlalchemy import text

from db.db import get_engine
from services.audit_service import log_audit
from services import flight_service
from core.rotation_expansion import TemplateLeg, expand_template

REQUIRED_TEMPLATE_FIELDS = {"rotation_code", "days_of_week", "effective_from"}


def _validate_legs(legs: Sequence[dict]) -> None:
    """
    Shared validation for create_template()/create_new_version(),
    called before anything is written. flight_no is required
    (migrations/012 enforces this at the database level too, but a
    clean ValueError here beats a raw NOT NULL violation reaching the
    caller — same principle flight_service.add_flight()'s own
    REQUIRED_FIELDS check already follows) — a template leg describes
    a known, named, recurring rotation, unlike an ad-hoc Control Room
    charter, where a missing flight_no is legitimate.
    """
    for leg in legs:
        if not leg.get("flight_no"):
            raise ValueError(
                f"leg_order={leg.get('leg_order')}: flight_no is required for a "
                f"template leg (a recurring rotation is always named) — ad-hoc "
                f"flights with no flight_no go through Control Room, not templates"
            )


def _insert_legs(conn, template_id: int, legs: Sequence[dict]) -> None:
    for leg in legs:
        conn.execute(text("""
            INSERT INTO rotation_template_legs
                (template_id, leg_order, flight_no, origin, destination,
                 dep_time, arr_time, day_offset, domestic)
            VALUES (:template_id, :leg_order, :flight_no, :origin, :destination,
                    :dep_time, :arr_time, :day_offset, :domestic)
        """), {
            "template_id": template_id,
            "leg_order": leg["leg_order"],
            "flight_no": leg.get("flight_no"),
            "origin": leg["origin"],
            "destination": leg["destination"],
            "dep_time": leg["dep_time"],
            "arr_time": leg["arr_time"],
            "day_offset": leg.get("day_offset", 0),
            "domestic": bool(leg["domestic"]),
        })


def create_template(
    rotation_code: str,
    days_of_week: Sequence[int],
    legs: Sequence[dict],
    effective_from: dt.date,
    meal_provided: bool,
    description: Optional[str] = None,
    effective_until: Optional[dt.date] = None,
    app_user: Optional[str] = None,
) -> int:
    """
    Creates version 1 of a rotation template. legs: list of dicts with
    leg_order/origin/destination/dep_time/arr_time/domestic (and
    optionally flight_no/day_offset) — the same shape
    core.rotation_expansion.TemplateLeg expects, kept as plain dicts
    here so callers don't need to import a core dataclass just to call
    this function.

    meal_provided: whether a meal is provided across this WHOLE
    rotation (2026-08-08) — required, no default, mirroring the
    rotation_templates.meal_provided column's own NOT NULL intent at
    this call-site level: a caller must make a real choice, the same
    way rotation_code/effective_from already have none. Broadcast onto
    every rotation_instance_leg this template's versions ever expand
    into (expand_and_persist()) and, from there, onto every promoted
    flights row (approve_instance()) — see migrations/014's own header
    for why this lives on the template, not on rotation_template_legs
    (a whole-duty operational fact, not a per-leg one) and why it's
    NOT NULL DEFAULT TRUE at the database level even though this
    parameter itself has no default. core/legality/pcaa_ano012_core.py's
    D25 rule fires NEEDS_MANUAL_REVIEW for any duty over 6h whose
    meal_provided is unknown — this is the fix for that gap having no
    real data source anywhere in the pipeline.

    Raises ValueError if rotation_code/days_of_week/effective_from is
    missing, or if legs is empty (matches expand_template()'s own
    validation — failing here, before anything is written, is
    preferable to a template that can never actually expand).
    """
    if not rotation_code:
        raise ValueError("rotation_code is required")
    if not days_of_week:
        raise ValueError("days_of_week is required")
    if not legs:
        raise ValueError("legs must not be empty")
    if effective_from is None:
        raise ValueError("effective_from is required")
    _validate_legs(legs)

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            INSERT INTO rotation_templates
                (rotation_code, description, days_of_week, effective_from,
                 effective_until, version, meal_provided)
            VALUES (:rotation_code, :description, :days_of_week, :effective_from,
                    :effective_until, 1, :meal_provided)
            RETURNING id
        """), {
            "rotation_code": rotation_code,
            "description": description,
            "days_of_week": list(days_of_week),
            "effective_from": effective_from,
            "effective_until": effective_until,
            "meal_provided": bool(meal_provided),
        })
        template_id = result.scalar()
        _insert_legs(conn, template_id, legs)

        log_audit(
            action_type="ROTATION_TEMPLATE_CREATED",
            reason=f"{rotation_code} v1",
            changed_state=str({
                "rotation_code": rotation_code, "days_of_week": list(days_of_week),
                "effective_from": str(effective_from), "leg_count": len(legs),
            }),
            app_user=app_user,
            conn=conn,
        )

    return template_id


def create_new_version(
    rotation_code: str,
    days_of_week: Sequence[int],
    legs: Sequence[dict],
    effective_from: dt.date,
    meal_provided: bool,
    description: Optional[str] = None,
    effective_until: Optional[dt.date] = None,
    app_user: Optional[str] = None,
) -> int:
    """
    Creates the next version of an existing rotation, in one
    transaction: inserts the new version first (its id is needed for
    the old version's superseded_by), then closes the current open
    (effective_until IS NULL) version's effective_until to the day
    BEFORE the new version's effective_from — not the same day, since
    the EXCLUDE constraint's '[]' inclusive bounds mean a single day
    cannot belong to two versions.

    meal_provided: required, no default — see create_template()'s own
    docstring for the full reasoning. A new version can genuinely set
    a different value than the version it supersedes (that's the whole
    point of storing this per-version rather than assuming it never
    changes).

    This order is a genuine circular dependency, not an arbitrary
    choice: superseded_by can't be set until the new row exists, but a
    NON-deferred overlap constraint would reject the new row's INSERT
    outright, since the old version is still open-ended at that exact
    moment. migrations/011's EXCLUDE constraint is declared DEFERRABLE
    INITIALLY DEFERRED specifically so this works: the overlap check
    only runs at COMMIT, by which point both statements below have
    already brought the two rows to a jointly non-overlapping state.
    Confirmed against real Postgres 16: this insert-then-close order
    succeeds, and a genuine overlap (e.g. a v3 inserted while v2 is
    left open) still correctly fails at COMMIT — the guarantee itself
    doesn't move, only the timing of when it's checked.

    Raises ValueError if no open version exists for rotation_code, or
    if effective_from does not leave at least one day for the closing
    effective_until (i.e. effective_from <= the current version's own
    effective_from).
    """
    if not legs:
        raise ValueError("legs must not be empty")
    _validate_legs(legs)

    engine = get_engine()
    with engine.begin() as conn:
        current = conn.execute(text("""
            SELECT id, effective_from FROM rotation_templates
            WHERE rotation_code = :rotation_code AND effective_until IS NULL
            ORDER BY version DESC LIMIT 1
        """), {"rotation_code": rotation_code}).mappings().first()

        if current is None:
            raise ValueError(
                f"No open version (effective_until IS NULL) found for "
                f"rotation_code={rotation_code!r} — use create_template() for a "
                f"genuinely new rotation, not create_new_version()"
            )

        day_before = effective_from - dt.timedelta(days=1)
        if day_before < current["effective_from"]:
            raise ValueError(
                f"effective_from={effective_from} leaves no room to close the "
                f"current version (effective_from={current['effective_from']}) — "
                f"the new version must start at least one day after the current "
                f"version's own effective_from"
            )

        next_version = conn.execute(text("""
            SELECT COALESCE(MAX(version), 0) + 1 FROM rotation_templates
            WHERE rotation_code = :rotation_code
        """), {"rotation_code": rotation_code}).scalar()

        new_result = conn.execute(text("""
            INSERT INTO rotation_templates
                (rotation_code, description, days_of_week, effective_from,
                 effective_until, version, meal_provided)
            VALUES (:rotation_code, :description, :days_of_week, :effective_from,
                    :effective_until, :version, :meal_provided)
            RETURNING id
        """), {
            "rotation_code": rotation_code,
            "description": description,
            "days_of_week": list(days_of_week),
            "effective_from": effective_from,
            "effective_until": effective_until,
            "version": next_version,
            "meal_provided": bool(meal_provided),
        })
        new_template_id = new_result.scalar()

        # Closing effective_until (NULL -> day_before) and setting
        # superseded_by together is the one legitimate mutation
        # migrations/011's guard_rotation_templates_mutation() trigger
        # allows — every other column must stay exactly as it was, or
        # the trigger rejects this UPDATE.
        conn.execute(text("""
            UPDATE rotation_templates
            SET effective_until = :day_before, superseded_by = :new_id
            WHERE id = :old_id
        """), {"day_before": day_before, "new_id": new_template_id, "old_id": current["id"]})

        _insert_legs(conn, new_template_id, legs)

        log_audit(
            action_type="ROTATION_TEMPLATE_VERSION_CREATED",
            reason=f"{rotation_code} v{next_version}, supersedes template_id={current['id']}",
            changed_state=str({
                "rotation_code": rotation_code, "version": next_version,
                "effective_from": str(effective_from), "leg_count": len(legs),
            }),
            app_user=app_user,
            conn=conn,
        )

    return new_template_id


def get_template_legs(template_id: int) -> pd.DataFrame:
    engine = get_engine()
    return pd.read_sql(text("""
        SELECT * FROM rotation_template_legs
        WHERE template_id = :template_id
        ORDER BY leg_order
    """), engine, params={"template_id": template_id})


def get_versions(rotation_code: str) -> pd.DataFrame:
    """All versions of a rotation_code, oldest first."""
    engine = get_engine()
    return pd.read_sql(text("""
        SELECT * FROM rotation_templates
        WHERE rotation_code = :rotation_code
        ORDER BY version
    """), engine, params={"rotation_code": rotation_code})


def _versions_covering_window(conn, rotation_code: str, date_from: dt.date, date_to: dt.date):
    """Every template version for rotation_code whose effective range
    overlaps [date_from, date_to] — the EXCLUDE constraint guarantees
    these ranges never overlap each other, so at most one covers any
    single date in the window."""
    return conn.execute(text("""
        SELECT * FROM rotation_templates
        WHERE rotation_code = :rotation_code
          AND effective_from <= :date_to
          AND (effective_until IS NULL OR effective_until >= :date_from)
        ORDER BY effective_from
    """), {"rotation_code": rotation_code, "date_from": date_from, "date_to": date_to}).mappings().all()


def expand_and_persist(
    rotation_code: str, date_from: dt.date, date_to: dt.date,
    app_user: Optional[str] = None,
) -> List[int]:
    """
    Expands rotation_code over [date_from, date_to] and persists any
    NEW draft instances — idempotent: a date that already has a
    rotation_instance (DRAFT or APPROVED, from this run or any earlier
    one) is skipped entirely, never re-inserted or altered. This is
    what makes "no silent reshuffle on regeneration" a structural
    guarantee: re-running this after a new template version exists
    only ever fills forward gaps.

    A window spanning a version boundary correctly uses each version's
    own days_of_week/legs for the dates it actually covers — see
    _versions_covering_window(); the EXCLUDE constraint on
    rotation_templates guarantees at most one version applies to any
    given date, so there's no ambiguity to resolve here.

    Returns the list of newly created rotation_instance ids (empty if
    every date in the window already had an instance, or if no
    version of rotation_code covers any date in the window).
    """
    engine = get_engine()
    created_ids: List[int] = []

    with engine.begin() as conn:
        versions = _versions_covering_window(conn, rotation_code, date_from, date_to)

        for version_row in versions:
            legs_rows = conn.execute(text("""
                SELECT * FROM rotation_template_legs
                WHERE template_id = :template_id ORDER BY leg_order
            """), {"template_id": version_row["id"]}).mappings().all()

            template_legs = [
                TemplateLeg(
                    leg_order=row["leg_order"], origin=row["origin"],
                    destination=row["destination"], dep_time=row["dep_time"],
                    arr_time=row["arr_time"], flight_no=row["flight_no"],
                    day_offset=row["day_offset"], domestic=row["domestic"],
                )
                for row in legs_rows
            ]

            # Clip the window to this version's own effective range —
            # expand_template() has no concept of versions, it just
            # expands every matching weekday in whatever range it's given.
            version_from = max(date_from, version_row["effective_from"])
            version_to = date_to if version_row["effective_until"] is None \
                else min(date_to, version_row["effective_until"])
            if version_from > version_to:
                continue

            drafts = expand_template(
                version_row["days_of_week"], template_legs, version_from, version_to)

            for draft in drafts:
                exists = conn.execute(text("""
                    SELECT 1 FROM rotation_instances
                    WHERE rotation_code = :rotation_code AND rotation_date = :rotation_date
                """), {"rotation_code": rotation_code, "rotation_date": draft.rotation_date}).first()
                if exists is not None:
                    continue

                instance_result = conn.execute(text("""
                    INSERT INTO rotation_instances
                        (template_id, rotation_code, version, rotation_date, status)
                    VALUES (:template_id, :rotation_code, :version, :rotation_date, 'DRAFT')
                    RETURNING id
                """), {
                    "template_id": version_row["id"], "rotation_code": rotation_code,
                    "version": version_row["version"], "rotation_date": draft.rotation_date,
                })
                instance_id = instance_result.scalar()

                for leg in draft.legs:
                    # meal_provided is rotation-level (version_row, not
                    # leg) — broadcast the template version's one value
                    # onto every leg this run creates, same denormalize-
                    # at-expansion-time treatment domestic/flight_no
                    # already get (see migrations/014's header).
                    conn.execute(text("""
                        INSERT INTO rotation_instance_legs
                            (instance_id, leg_order, flight_no, origin, destination,
                             dep_time_planned, arr_time_planned, domestic, meal_provided)
                        VALUES (:instance_id, :leg_order, :flight_no, :origin, :destination,
                                :dep_time_planned, :arr_time_planned, :domestic, :meal_provided)
                    """), {
                        "instance_id": instance_id, "leg_order": leg.leg_order,
                        "flight_no": leg.flight_no, "origin": leg.origin,
                        "destination": leg.destination,
                        "dep_time_planned": leg.dep_time_planned,
                        "arr_time_planned": leg.arr_time_planned,
                        "domestic": leg.domestic,
                        "meal_provided": version_row["meal_provided"],
                    })

                created_ids.append(instance_id)

        if created_ids:
            log_audit(
                action_type="ROTATION_INSTANCES_EXPANDED",
                reason=f"{rotation_code} {date_from} to {date_to}",
                changed_state=str({"created_instance_ids": created_ids}),
                app_user=app_user,
                conn=conn,
            )

    return created_ids


def get_instances(rotation_code: Optional[str] = None, status: Optional[str] = None) -> pd.DataFrame:
    engine = get_engine()
    query = "SELECT * FROM rotation_instances"
    conditions = []
    params: dict = {}
    if rotation_code:
        conditions.append("rotation_code = :rotation_code")
        params["rotation_code"] = rotation_code
    if status:
        conditions.append("status = :status")
        params["status"] = status
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY rotation_date"
    return pd.read_sql(text(query), engine, params=params)


def get_instance_legs(instance_id: int) -> pd.DataFrame:
    engine = get_engine()
    return pd.read_sql(text("""
        SELECT * FROM rotation_instance_legs
        WHERE instance_id = :instance_id
        ORDER BY leg_order
    """), engine, params={"instance_id": instance_id})


def _promoted_flight_ids(conn, instance_id: int) -> List[int]:
    """Shared query, given an already-open connection: the real
    flight_ids an APPROVED instance's legs were promoted into, in leg
    (chronological) order. Used by approve_instance()'s own idempotency
    check and by get_promoted_flight_ids() below — factored out
    (2026-08-04) so services/roster_generator_service.py, which needs
    this exact "which real flights make up this approved rotation"
    lookup for every candidate rotation it considers, doesn't have to
    duplicate it."""
    return list(conn.execute(text("""
        SELECT flight_id FROM flights
        WHERE rotation_instance_id = :id
        ORDER BY dep_time_planned
    """), {"id": instance_id}).scalars().all())


def get_promoted_flight_ids(instance_id: int) -> List[int]:
    """Public wrapper for _promoted_flight_ids() — the flight_ids an
    APPROVED rotation_instance's legs were promoted into, in leg
    (chronological — the order assign_crew_to_duty() needs) order.
    Empty list if the instance isn't approved yet or doesn't exist."""
    engine = get_engine()
    with engine.connect() as conn:
        return _promoted_flight_ids(conn, instance_id)


# ------------------------------------------------------------------
# Approval workflow (2026-08-04) — DRAFT -> APPROVED promotes every
# leg into a real flights row via the existing flight_service.
# add_flight(); DRAFT -> REJECTED never touches flights at all. This
# is the step that makes a template actually produce operational
# flights.
# ------------------------------------------------------------------

def approve_instance(instance_id: int, app_user: Optional[str] = None) -> List[int]:
    """
    Approves a DRAFT rotation_instance and promotes every one of its
    legs into a real flights row, all in ONE transaction — either
    every leg becomes a real flight, or none does. A failure partway
    through (e.g. leg 2 of 3) rolls back leg 1's insert too; nothing
    is left half-promoted, and rotation_instances.status stays DRAFT.

    Idempotent on an already-APPROVED instance: returns the existing
    flight_ids (looked up via flights.rotation_instance_id) without
    creating anything new — same idempotency principle already used
    by expand_and_persist(). The database itself backs this up
    independently: migrations/012's partial unique index on
    (rotation_instance_id, flight_no) makes a genuine double-promotion
    structurally impossible even if this idempotency check were ever
    bypassed, not just discouraged by it.

    Raises ValueError if instance_id doesn't exist, or if the instance
    is REJECTED (not DRAFT and not already-APPROVED) — a rejected
    instance can't be approved by this function; see HANDOVER.md for
    why re-deciding a rejection is out of scope here.
    """
    engine = get_engine()
    with engine.begin() as conn:
        instance = conn.execute(text("""
            SELECT * FROM rotation_instances WHERE id = :id
        """), {"id": instance_id}).mappings().first()

        if instance is None:
            raise ValueError(f"No rotation_instance with id={instance_id}")

        if instance["status"] == "APPROVED":
            return _promoted_flight_ids(conn, instance_id)

        if instance["status"] != "DRAFT":
            raise ValueError(
                f"rotation_instance {instance_id} is {instance['status']}, not "
                f"DRAFT — cannot approve"
            )

        legs = conn.execute(text("""
            SELECT * FROM rotation_instance_legs
            WHERE instance_id = :id ORDER BY leg_order
        """), {"id": instance_id}).mappings().all()

        flight_ids = []
        for leg in legs:
            flight_id = flight_service.add_flight({
                "flight_no": leg["flight_no"],
                "origin": leg["origin"],
                "destination": leg["destination"],
                "dep_time_planned": leg["dep_time_planned"],
                "arr_time_planned": leg["arr_time_planned"],
                "domestic": leg["domestic"],
                "meal_provided": leg["meal_provided"],
                "rotation_instance_id": instance_id,
            }, app_user=app_user, conn=conn)
            flight_ids.append(flight_id)

        conn.execute(text("""
            UPDATE rotation_instances SET status = 'APPROVED' WHERE id = :id
        """), {"id": instance_id})

        log_audit(
            action_type="ROTATION_INSTANCE_APPROVED",
            reason=f"{instance['rotation_code']} {instance['rotation_date']}",
            changed_state=str({"instance_id": instance_id, "flight_ids": flight_ids}),
            app_user=app_user,
            conn=conn,
        )

    return flight_ids


def reject_instance(instance_id: int, reason: Optional[str] = None,
                     app_user: Optional[str] = None) -> None:
    """
    Rejects a DRAFT rotation_instance. Never touches flights — nothing
    was ever created for a DRAFT, so there's nothing to undo.

    Deliberately NOT idempotent the way approve_instance() is: raises
    ValueError for any non-DRAFT status, including an already-REJECTED
    instance. There's no prior side effect to safely return here the
    way approve's existing flight_ids are — rejecting an already-
    rejected instance a second time is a caller error to surface, not
    a safe no-op.
    """
    engine = get_engine()
    with engine.begin() as conn:
        instance = conn.execute(text("""
            SELECT status, rotation_code, rotation_date FROM rotation_instances
            WHERE id = :id
        """), {"id": instance_id}).mappings().first()

        if instance is None:
            raise ValueError(f"No rotation_instance with id={instance_id}")

        if instance["status"] != "DRAFT":
            raise ValueError(
                f"rotation_instance {instance_id} is {instance['status']}, not "
                f"DRAFT — cannot reject"
            )

        conn.execute(text("""
            UPDATE rotation_instances SET status = 'REJECTED' WHERE id = :id
        """), {"id": instance_id})

        log_audit(
            action_type="ROTATION_INSTANCE_REJECTED",
            reason=reason or f"{instance['rotation_code']} {instance['rotation_date']}",
            app_user=app_user,
            conn=conn,
        )
