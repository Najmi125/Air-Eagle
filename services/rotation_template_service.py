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
from core.duty_builder import FlightLeg, build_duty

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

    Route continuity (leg N's destination == leg N+1's origin) is
    checked here too, as of 2026-08-19. core/duty_builder.py's
    build_duty() has always enforced it, but only at EXPANSION time — so
    a template with disconnected legs saved cleanly and only failed
    days later, far from where the mistake was made. Same principle as
    the arr>dep check pages/7_Schedule_Templates.py already does at
    creation: catch it where it was typed. It lives here rather than in
    the page so a non-UI caller can't sidestep it, and the message
    deliberately echoes build_duty()'s so the two never disagree about
    what continuity means.
    """
    for leg in legs:
        if not leg.get("flight_no"):
            raise ValueError(
                f"leg_order={leg.get('leg_order')}: flight_no is required for a "
                f"template leg (a recurring rotation is always named) — ad-hoc "
                f"flights with no flight_no go through Control Room, not templates"
            )

    ordered = sorted(legs, key=lambda leg: leg.get("leg_order", 0))
    for current, following in zip(ordered, ordered[1:]):
        if current.get("destination") != following.get("origin"):
            raise ValueError(
                f"leg_order={current.get('leg_order')} arrives at "
                f"{current.get('destination')} but leg_order="
                f"{following.get('leg_order')} departs from {following.get('origin')} "
                f"— a crew member can't be in two places at once. These legs don't "
                f"form one physically continuous rotation."
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
    snack_provided: bool,
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

    snack_provided: same shape, same reasoning, same required-no-default
    footing (migrations/015, 2026-08-08) — D2.18 fires a WARNING (not
    NEEDS_MANUAL_REVIEW) for any duty over 4h whose snack_provided is
    False, and stays silent when it's unknown, so this was previously
    an accidentally-harmless gap rather than a blocking one the way
    meal_provided was — still fixed the same way, as real data, not a
    code default.

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
                 effective_until, version, meal_provided, snack_provided)
            VALUES (:rotation_code, :description, :days_of_week, :effective_from,
                    :effective_until, 1, :meal_provided, :snack_provided)
            RETURNING id
        """), {
            "rotation_code": rotation_code,
            "description": description,
            "days_of_week": list(days_of_week),
            "effective_from": effective_from,
            "effective_until": effective_until,
            "meal_provided": bool(meal_provided),
            "snack_provided": bool(snack_provided),
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
    snack_provided: bool,
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

    meal_provided/snack_provided: required, no default — see
    create_template()'s own docstring for the full reasoning. A new
    version can genuinely set different values than the version it
    supersedes (that's the whole point of storing these per-version
    rather than assuming they never change).

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
                 effective_until, version, meal_provided, snack_provided)
            VALUES (:rotation_code, :description, :days_of_week, :effective_from,
                    :effective_until, :version, :meal_provided, :snack_provided)
            RETURNING id
        """), {
            "rotation_code": rotation_code,
            "description": description,
            "days_of_week": list(days_of_week),
            "effective_from": effective_from,
            "effective_until": effective_until,
            "version": next_version,
            "meal_provided": bool(meal_provided),
            "snack_provided": bool(snack_provided),
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


def get_all_rotation_codes() -> List[str]:
    """Distinct rotation_code values across every version — the one
    discovery primitive get_versions()/get_template_legs() don't cover,
    since both require already knowing rotation_code. Added for
    pages/7_Schedule_Templates.py, which otherwise has no way to list
    existing templates (2026-08-10)."""
    engine = get_engine()
    return pd.read_sql(text("""
        SELECT DISTINCT rotation_code FROM rotation_templates ORDER BY rotation_code
    """), engine)["rotation_code"].tolist()


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
                    # meal_provided/snack_provided are rotation-level
                    # (version_row, not leg) — broadcast the template
                    # version's values onto every leg this run creates,
                    # same denormalize-at-expansion-time treatment
                    # domestic/flight_no already get (see migrations/014's
                    # and migrations/015's headers).
                    conn.execute(text("""
                        INSERT INTO rotation_instance_legs
                            (instance_id, leg_order, flight_no, origin, destination,
                             dep_time_planned, arr_time_planned, domestic,
                             meal_provided, snack_provided)
                        VALUES (:instance_id, :leg_order, :flight_no, :origin, :destination,
                                :dep_time_planned, :arr_time_planned, :domestic,
                                :meal_provided, :snack_provided)
                    """), {
                        "instance_id": instance_id, "leg_order": leg.leg_order,
                        "flight_no": leg.flight_no, "origin": leg.origin,
                        "destination": leg.destination,
                        "dep_time_planned": leg.dep_time_planned,
                        "arr_time_planned": leg.arr_time_planned,
                        "domestic": leg.domestic,
                        "meal_provided": version_row["meal_provided"],
                        "snack_provided": version_row["snack_provided"],
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
                "snack_provided": leg["snack_provided"],
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


def _deletability(conn, template_id: int) -> dict:
    """Shared by get_template_deletability() and delete_template(), on a
    caller-supplied connection so the latter can ask inside its own
    transaction.

    The BOOLEAN comes from migrations/019's own
    rotation_template_is_deletable(), never from a Python
    reimplementation of the rule. That matters: the trigger and this
    answer must not be able to drift into disagreeing about what
    "unused" means, and the only way to guarantee that is to ask the
    same function the trigger asks. Python's job here is limited to
    turning "false" into a sentence a controller can act on, which is
    the one thing a BOOLEAN can't carry.
    """
    row = conn.execute(text("""
        SELECT rotation_code, version FROM rotation_templates WHERE id = :id
    """), {"id": template_id}).mappings().first()

    if row is None:
        return {"deletable": False, "reason": f"No template with id={template_id}",
                "instance_count": 0, "version_count": 0}

    deletable = bool(conn.execute(text(
        "SELECT rotation_template_is_deletable(:id)"
    ), {"id": template_id}).scalar())

    instance_count = int(conn.execute(text("""
        SELECT COUNT(*) FROM rotation_instances WHERE template_id = :id
    """), {"id": template_id}).scalar())

    version_count = int(conn.execute(text("""
        SELECT COUNT(*) FROM rotation_templates WHERE rotation_code = :code
    """), {"code": row["rotation_code"]}).scalar())

    if deletable:
        return {"deletable": True, "reason": None,
                "instance_count": instance_count, "version_count": version_count}

    if instance_count:
        reason = (
            f"{instance_count} rotation instance(s) have been generated from this "
            f"template. Close it with an effective_until date, or supersede it with "
            f"a new version — deleting it would orphan work already done."
        )
    elif version_count > 1:
        reason = (
            f"{row['rotation_code']} has {version_count} versions. Only a sole-version "
            f"template can be deleted: removing one version of a chain would leave its "
            f"predecessor permanently closed, since effective_until can only be closed "
            f"once. Supersede it with a new version instead."
        )
    else:
        # The function said no for a reason this code doesn't enumerate
        # (e.g. a superseded_by reference from another code). Say so
        # plainly rather than inventing an explanation.
        reason = (
            f"Template {row['rotation_code']} v{int(row['version'])} is referenced by "
            f"other records and cannot be deleted."
        )

    return {"deletable": False, "reason": reason,
            "instance_count": instance_count, "version_count": version_count}


def get_template_deletability(template_id: int) -> dict:
    """
    Can this template be deleted, and if not, why not? Returns
    {"deletable": bool, "reason": str|None, "instance_count": int,
     "version_count": int}.

    Exists so the UI can DISABLE the delete control with a specific
    reason rather than offering it and failing — "1 rotation instance
    already generated" tells a controller what to do next, where a raw
    trigger exception does not.

    This is a display query, not enforcement. It can go stale between
    being read and a DELETE being issued; the trigger is what actually
    refuses. See delete_template() for how the two layers relate.
    """
    engine = get_engine()
    with engine.connect() as conn:
        return _deletability(conn, template_id)


def delete_template(template_id: int, app_user: Optional[str] = None) -> str:
    """
    Deletes a template that has produced nothing. Returns the deleted
    rotation_code.

    Only for undoing a mistaken creation — a template referenced by
    nothing, where there is no history to protect and therefore nothing
    to lose. Anything with instances, or any version that is part of a
    supersession chain, is refused; see migrations/019 for the full
    reasoning and for why the rule lives in the trigger rather than in a
    bypass.

    Checks deletability BEFORE touching anything, and raises ValueError
    with the human reason if it fails. An earlier version of this
    function deliberately didn't, on the grounds that a Python check
    would be a TOCTOU gap and would imply Python enforces the rule.
    That reasoning was half right, and the half it got wrong showed up
    in real use (2026-08-19):

    Because legs are deleted first, the refusal for a template that IS
    in use came from the LEGS trigger, so a controller clicking Delete
    on a used template was told "rotation_template_legs rows are
    immutable — create a new template version instead", which is true
    and explains nothing about why THIS template can't go. The invariant
    held perfectly; the message was about the wrong guard.

    The TOCTOU concern is real but harmless here, because of the
    direction the race can fail in: a pre-check can only ever produce a
    better error message, never a permission. If an instance appears
    between the check and the DELETE, the trigger still refuses, and the
    caller sees the trigger's message — precisely the old behaviour. The
    check can never turn a "no" into a "yes".

    So the layering is: this check exists for the message, the trigger
    is the truth. tests/test_rotation_template_service.py asserts a raw
    SQL DELETE that bypasses this function entirely is still refused,
    which is what keeps that claim honest.

    Legs go first: rotation_template_legs.template_id has no
    ON DELETE CASCADE, so the parent row cannot go while they reference
    it. Both statements share one transaction, so a refusal on either
    leaves the template completely intact.

    The audit record is written BEFORE the deletes, in the same
    transaction. Writing it afterwards would be reading from a row that
    no longer exists; writing it in a separate transaction would risk a
    committed audit entry for a deletion that was then refused. Ordering
    it first inside one transaction means it survives if and only if the
    deletion does.
    """
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT rotation_code, version FROM rotation_templates WHERE id = :id
        """), {"id": template_id}).mappings().first()

        if row is None:
            raise ValueError(f"No template with id={template_id}")

        # Asked inside this transaction, and answered by the same SQL
        # function the trigger consults — so the message a controller
        # sees can't describe a different rule from the one enforced.
        status = _deletability(conn, template_id)
        if not status["deletable"]:
            raise ValueError(status["reason"])

        log_audit(
            action_type="ROTATION_TEMPLATE_DELETED",
            original_state=f"{row['rotation_code']} v{int(row['version'])} (template_id={template_id})",
            reason="Unused template deleted — no rotation instances generated",
            app_user=app_user,
            conn=conn,
        )

        conn.execute(text("DELETE FROM rotation_template_legs WHERE template_id = :id"),
                     {"id": template_id})
        conn.execute(text("DELETE FROM rotation_templates WHERE id = :id"),
                     {"id": template_id})

    return row["rotation_code"]


def compute_duty_window(legs: pd.DataFrame) -> Optional[tuple]:
    """The real duty window for a set of rotation_instance_legs:
    (report_time, debrief_time), or None if it can't be computed.

    Takes the legs DataFrame the caller already has rather than an
    instance_id, so the review table doesn't issue a second query per
    row.

    Exists because the draft review table was displaying first-departure
    and last-arrival under headings "Report" and "Debrief" (2026-08-19).
    Those are not the same thing: ANO-012 D7.1.2 adds a pre-flight and
    post-flight buffer either side. A real domestic rotation showed
    19:00->23:45 where the duty is actually 18:15->00:00, and an
    international showed 01:45->11:00 where the duty is 00:45->11:30 —
    so a controller judging whether a rotation is flyable read the FDP
    as an hour shorter than it is, and the draft contradicted what the
    Roster page showed for the same rotation once crewed.

    Routing through build_duty() rather than adding 45/15/60/30 here is
    the point: the draft review and the roster now agree BY
    CONSTRUCTION, because they are the same calculation. Duplicating the
    buffer arithmetic would let them drift apart again, which is the
    bug this fixes.

    This lives in the service, not the page, chiefly for the `domestic`
    aggregation. build_duty() takes ONE duty-level flag while legs carry
    one each, and assignment_service resolves that with
    all(bool(leg["domestic"])) at five separate sites: a duty counts as
    domestic only if EVERY leg is, so any international sector applies
    the longer 60/30 buffers to the whole duty. Re-deriving that in a
    page would be a sixth copy of a rule the page has no business
    knowing, and getting it wrong understates the duty window — the same
    direction of error this function exists to correct.

    Returns None rather than raising. build_duty() raises ValueError on
    empty or out-of-order legs, and a draft is exactly the place
    odd-looking data can reach: this is a DISPLAY value, and a display
    value must never be able to take the review table down. The caller
    shows a placeholder instead. (Learned the hard way the same day —
    see pages/7_Schedule_Templates.py's delete affordance.)
    """
    if legs is None or legs.empty:
        return None

    try:
        flight_legs = [
            FlightLeg(
                dep_time=row["dep_time_planned"],
                arr_time=row["arr_time_planned"],
                origin=row["origin"],
                destination=row["destination"],
            )
            for _, row in legs.iterrows()
        ]
        domestic = all(bool(row["domestic"]) for _, row in legs.iterrows())
        result = build_duty(flight_legs, domestic=domestic)
    except Exception:
        return None

    return result.report_time, result.debrief_time
