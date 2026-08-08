"""
services/flight_service.py

Canonical write path for the flights table. Per the Ownership Table:
add_flight()/update_flight()/cancel_flight().

Flight Log is a permanent record — cancel_flight() never deletes a
row, it sets status='CANCELLED'. This matches what was explicitly
requested: "Flight Log ... a permanent log of all flights." The
flights.status CHECK constraint (migrations/002_flights_table.sql)
already only allows PLANNED/OPERATED/CANCELLED/DISRUPTED, so this
mirrors what the schema was already built to support.
"""
import datetime as dt
from typing import Optional
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from db.db import get_engine
from services.audit_service import log_audit

REQUIRED_FIELDS = {"origin", "destination", "dep_time_planned", "arr_time_planned", "domestic"}

UPDATABLE_FIELDS = {
    "flight_no", "origin", "destination", "aircraft",
    "dep_time_planned", "arr_time_planned",
    "dep_time_actual", "arr_time_actual",
    "status", "cargo_dg", "remarks", "domestic",
    # Free-text occupant fields (migrations/010) — LM and AME/ENGR are
    # not crew records for Air Eagle (2026-08-02 operator decision, see
    # HANDOVER.md), so anyone aboard beyond the two cockpit seats is
    # recorded here as plain text, not a structured roster row.
    "other_occupants_operating", "other_occupants_non_operating",
    # Traceback to the rotation_instance that produced this flight via
    # approval (services/rotation_template_service.py's
    # approve_instance(), migrations/011/012) — NULL for every Control
    # Room ad-hoc flight, set only at promotion time.
    "rotation_instance_id",
    # NOT in REQUIRED_FIELDS, deliberately (migrations/014, 2026-08-08):
    # an ad-hoc Control Room caller that omits it falls back to this
    # column's own DEFAULT TRUE (operator-confirmed universal fact
    # today), rather than forcing every ad-hoc caller to know about it.
    # rotation-sourced flights always pass it explicitly (approve_
    # instance()) from the template's own stored value.
    "meal_provided",
    # Same shape and reasoning as meal_provided, one migration later
    # (migrations/015, 2026-08-08).
    "snack_provided",
}


def add_flight(flight_data: dict, app_user: Optional[str] = None,
               conn: Optional[Connection] = None) -> int:
    """
    Insert a new flight. Returns the generated flight_id.

    Validates arr_time_planned > dep_time_planned at the service
    layer too (not just relying on the DB CHECK constraint) so the
    caller gets a clean ValueError instead of a raw SQL error.

    conn: an already-open SQLAlchemy Connection inside an active
    transaction (e.g. from `with engine.begin() as conn:`). Pass this
    when the insert must be atomic with other writes in the same
    transaction — see services/rotation_template_service.py's
    approve_instance(), which promotes every leg of a rotation into a
    real flight together, all-or-nothing. Same contract as
    audit_service.log_audit()'s own conn parameter (Step 6,
    2026-08-02): when passed, both the INSERT and the FLIGHT_ADDED
    audit record below join the caller's transaction and share its
    fate — if the caller later rolls back, neither survives. When
    conn is None (the default, and every pre-existing call site —
    Control Room's ad-hoc path still bypasses this function entirely
    with its own raw INSERT, unrelated to this parameter), behavior
    is unchanged: this function opens and commits its own independent
    transaction, same as always.
    """
    missing = [f for f in REQUIRED_FIELDS if not flight_data.get(f) and flight_data.get(f) is not False]
    if missing:
        raise ValueError(f"Missing required flight field(s): {', '.join(missing)}")

    if flight_data["arr_time_planned"] <= flight_data["dep_time_planned"]:
        raise ValueError("arr_time_planned must be after dep_time_planned")

    fields = {k: v for k, v in flight_data.items() if k in UPDATABLE_FIELDS}

    columns = ", ".join(fields.keys())
    placeholders = ", ".join(f":{k}" for k in fields.keys())
    insert_stmt = text(
        f"INSERT INTO flights ({columns}) VALUES ({placeholders}) RETURNING flight_id"
    )

    if conn is not None:
        flight_id = conn.execute(insert_stmt, fields).scalar()
        log_audit(
            action_type="FLIGHT_ADDED",
            affected_flight=flight_id,
            changed_state=str(fields),
            app_user=app_user,
            conn=conn,
        )
        return flight_id

    engine = get_engine()
    with engine.begin() as own_conn:
        flight_id = own_conn.execute(insert_stmt, fields).scalar()

    log_audit(
        action_type="FLIGHT_ADDED",
        affected_flight=flight_id,
        changed_state=str(fields),
        app_user=app_user,
    )
    return flight_id


def update_flight(flight_id: int, updates: dict, app_user: Optional[str] = None) -> None:
    """Update one or more fields on an existing flight — including
    recording actual departure/arrival times, which is how a delay
    gets captured. This does NOT recompute FDP; that's
    core/duty_builder.py's recompute_fdp_after_delay(), called by
    whatever service consumes the updated actual times (Phase 6)."""
    existing = get_flight(flight_id)
    if existing is None:
        raise ValueError(f"No flight with flight_id={flight_id}")

    fields = {k: v for k, v in updates.items() if k in UPDATABLE_FIELDS}
    if not fields:
        raise ValueError("No updatable fields provided")

    set_clause = ", ".join(f"{k} = :{k}" for k in fields.keys())
    fields["flight_id"] = flight_id

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            f"UPDATE flights SET {set_clause}, updated_at = NOW() WHERE flight_id = :flight_id"
        ), fields)

    log_audit(
        action_type="FLIGHT_UPDATED",
        affected_flight=flight_id,
        original_state=str(existing.to_dict()),
        changed_state=str({k: v for k, v in fields.items() if k != "flight_id"}),
        app_user=app_user,
    )


def cancel_flight(flight_id: int, reason: Optional[str] = None, app_user: Optional[str] = None) -> None:
    """Marks status='CANCELLED'. Never deletes the row — Flight Log
    is a permanent record of every flight ever built, operated or not."""
    existing = get_flight(flight_id)
    if existing is None:
        raise ValueError(f"No flight with flight_id={flight_id}")

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE flights SET status = 'CANCELLED', updated_at = NOW() WHERE flight_id = :fid"
        ), {"fid": flight_id})

    log_audit(
        action_type="FLIGHT_CANCELLED",
        affected_flight=flight_id,
        reason=reason,
        app_user=app_user,
    )


def get_flight(flight_id: int) -> Optional[pd.Series]:
    engine = get_engine()
    df = pd.read_sql(text("SELECT * FROM flights WHERE flight_id = :fid"), engine, params={"fid": flight_id})
    if df.empty:
        return None
    return df.iloc[0]


def get_all_flights(
    status_filter: Optional[str] = None,
    date_from=None,
    date_to=None,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
    flight_no: Optional[str] = None,
) -> pd.DataFrame:
    """Defaults to showing EVERY flight, cancelled included — this is
    a permanent log, not a live/active-only view. Pass status_filter
    to narrow it down.

    date_from/date_to/flight_no added for services/assistant/reports.py's
    flight_records template (2026-08-01) — extending this single
    canonical read path rather than adding a parallel query function,
    per the one-read-path-per-table convention already followed
    elsewhere in this file. Filters on dep_time_planned, matching the
    existing ORDER BY; a flight is "in range" by when it was
    scheduled to depart, not by any actual/delayed time.

    flight_no is matched with spaces stripped and case-folded on both
    sides (REPLACE/UPPER in SQL): query_parser.parse_flight_no()
    returns "EPE 786" (with a space), but the real stored format
    wasn't confirmed against actual data at the time this was written
    — comparing loosely avoids a silent zero-row result if the DB
    turns out to store "EPE786" instead."""
    engine = get_engine()
    query = "SELECT * FROM flights"
    conditions = []
    params = {}
    if status_filter:
        conditions.append("status = :status")
        params["status"] = status_filter
    if flight_no:
        conditions.append("REPLACE(UPPER(flight_no), ' ', '') = REPLACE(UPPER(:flight_no), ' ', '')")
        params["flight_no"] = flight_no
    if date_from is not None:
        conditions.append("dep_time_planned >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        # date_to is an inclusive CALENDAR DATE (e.g. "through July
        # 31"), but dep_time_planned is a timestamp — a bare
        # `<= date_to` would silently mean "on or before midnight of
        # date_to", excluding every flight later that same day. Widen
        # to end-of-day only when date_to has no time component of
        # its own; a caller that already passed a real datetime is
        # trusted to mean exactly that instant.
        if isinstance(date_to, dt.date) and not isinstance(date_to, dt.datetime):
            date_to = dt.datetime.combine(date_to, dt.time.max)
        conditions.append("dep_time_planned <= :date_to")
        params["date_to"] = date_to
    if origin:
        conditions.append("origin = :origin")
        params["origin"] = origin
    if destination:
        conditions.append("destination = :destination")
        params["destination"] = destination
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY dep_time_planned DESC"
    return pd.read_sql(text(query), engine, params=params)
