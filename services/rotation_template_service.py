"""
services/rotation_template_service.py

Canonical write path for rotation_templates, rotation_template_legs,
rotation_instances, and rotation_instance_legs (migrations/011). Phase
7 groundwork (2026-08-04) — the template layer only. Does NOT write to
flights or roster: promoting an APPROVED rotation_instance into real
flights rows (via the existing flight_service.add_flight()) is a
later, separate approval-workflow piece, not built here.

Two immutability/no-overlap guarantees live at the DATABASE level
(migrations/011's EXCLUDE constraint and triggers), not just here —
this service's ordering (close the old version, then insert the new
one; never expose an update/delete for template legs) is the
convenient path that satisfies those constraints, not the mechanism
that enforces them. See migrations/011_rotation_templates.sql's
comments for the actual guarantees.
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional, Sequence

import pandas as pd
from sqlalchemy import text

from db.db import get_engine
from services.audit_service import log_audit
from core.rotation_expansion import TemplateLeg, expand_template

REQUIRED_TEMPLATE_FIELDS = {"rotation_code", "days_of_week", "effective_from"}


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

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            INSERT INTO rotation_templates
                (rotation_code, description, days_of_week, effective_from,
                 effective_until, version)
            VALUES (:rotation_code, :description, :days_of_week, :effective_from,
                    :effective_until, 1)
            RETURNING id
        """), {
            "rotation_code": rotation_code,
            "description": description,
            "days_of_week": list(days_of_week),
            "effective_from": effective_from,
            "effective_until": effective_until,
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
    description: Optional[str] = None,
    effective_until: Optional[dt.date] = None,
    app_user: Optional[str] = None,
) -> int:
    """
    Creates the next version of an existing rotation, in one
    transaction: closes the current open (effective_until IS NULL)
    version's effective_until to the day BEFORE the new version's
    effective_from (not the same day — the EXCLUDE constraint's '[]'
    inclusive bounds mean a single day cannot belong to two versions),
    records superseded_by, then inserts the new version.

    The database EXCLUDE constraint (migrations/011) is the actual
    guarantee against overlap; this ordering is what makes the normal
    path succeed without ever hitting it. Raises ValueError if no open
    version exists for rotation_code, or if effective_from does not
    leave at least one day for the closing effective_until (i.e.
    effective_from <= the current version's own effective_from).
    """
    if not legs:
        raise ValueError("legs must not be empty")

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
                 effective_until, version)
            VALUES (:rotation_code, :description, :days_of_week, :effective_from,
                    :effective_until, :version)
            RETURNING id
        """), {
            "rotation_code": rotation_code,
            "description": description,
            "days_of_week": list(days_of_week),
            "effective_from": effective_from,
            "effective_until": effective_until,
            "version": next_version,
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
                    conn.execute(text("""
                        INSERT INTO rotation_instance_legs
                            (instance_id, leg_order, flight_no, origin, destination,
                             dep_time_planned, arr_time_planned, domestic)
                        VALUES (:instance_id, :leg_order, :flight_no, :origin, :destination,
                                :dep_time_planned, :arr_time_planned, :domestic)
                    """), {
                        "instance_id": instance_id, "leg_order": leg.leg_order,
                        "flight_no": leg.flight_no, "origin": leg.origin,
                        "destination": leg.destination,
                        "dep_time_planned": leg.dep_time_planned,
                        "arr_time_planned": leg.arr_time_planned,
                        "domestic": leg.domestic,
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
