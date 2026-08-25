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

# Air Eagle's single B737 (registration supplied by the operator
# 2026-08-21). Pre-fills the aircraft field on the pages that create or
# edit a flight, so a controller doesn't retype it on every charter.
#
# ONE default is correct here because the airline operates ONE aircraft.
# This is an AIRLINE-CONFIGURATION value, not a platform assumption —
# FTLguard itself has no notion of a fleet of one, and nothing in
# core/ or the legality engine reads it.
#
# THE TRIGGER FOR CHANGING THIS: a second aircraft. At that point a
# default is the wrong shape entirely — it becomes a selector over a
# fleet, and a silent default would start attributing flights to the
# wrong airframe, which is worse than an empty field. Do not extend
# this to a "primary aircraft" default; make it a choice.
#
# It lives here, in the module that owns the flights table and has
# `aircraft` in UPDATABLE_FIELDS, rather than in a new module of its
# own: both consuming pages already import flight_service, so this adds
# no new import edge — and a new service module would oblige a
# Manage-app reboot on deploy (the stale-sys.modules rule, three
# occurrences so far) for the sake of one constant.
AIRCRAFT_DEFAULT = "AP-BNW"

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


def _apply_operated_rule(existing, fields: dict) -> dict:
    """A flight with BOTH actual times recorded has flown. Returns
    `fields` with status set accordingly, or raises if the caller's
    explicit status contradicts recorded fact.

    Lives here, at the single generic UPDATE on `flights`, rather than
    in a page — and it has to, for a reason beyond layering: the updates
    dict alone cannot answer the question. Departure actual is commonly
    recorded on one shift and arrival on the next, so any given call
    sees only one column. This merges the incoming values over the
    stored row, which a page-level rule could only do by re-reading the
    flight itself.

    THE INVARIANT, NOT A DEFAULT. The three rules:

    1. CANCELLED is terminal. Recording actuals against a cancelled
       flight never revives it — cancellation is a deliberate act,
       written by cancel_flight() outside this path entirely, and a
       late actual-time entry must not undo it.

    2. An explicit status wins, EXCEPT PLANNED on a flown flight, which
       is refused. Without that exception the rule would be optional:
       any caller could assert PLANNED over two recorded actuals and
       have it stick, and "a flight with both actuals is operated"
       would stop being true. DISRUPTED is explicitly allowed to win —
       see rule 3.

    3. The automatic transition fires only from PLANNED. A flight a
       controller has marked DISRUPTED keeps that label when actuals
       arrive: "it flew" is recoverable from the actual times
       themselves, "it was disrupted" is recoverable from nothing else.

    Read the note in HANDOVER.md before writing any report keyed on
    status = 'OPERATED': status does NOT mean "flew". It cannot, because
    one column cannot hold both OPERATED and DISRUPTED, so some flown
    flights will always carry a different label. The honest test for
    "which flights actually flew" is
    `dep_time_actual IS NOT NULL AND arr_time_actual IS NOT NULL`.
    This rule exists to make the filter meaningful and the record
    readable — a different job.
    """
    def _present(value):
        return value is not None and not pd.isna(value)

    merged_dep = fields.get("dep_time_actual", existing["dep_time_actual"])
    merged_arr = fields.get("arr_time_actual", existing["arr_time_actual"])
    both_actuals = _present(merged_dep) and _present(merged_arr)

    current_status = existing["status"]
    explicit_status = fields.get("status")

    if current_status == "CANCELLED":
        if explicit_status and explicit_status != "CANCELLED":
            raise ValueError(
                f"flight {existing['flight_id']} is CANCELLED — cancellation is "
                f"terminal and cannot be changed to {explicit_status}"
            )
        fields.pop("status", None)
        return fields

    if explicit_status:
        if explicit_status == "PLANNED" and both_actuals:
            raise ValueError(
                "cannot set status PLANNED on a flight with both actual times "
                "recorded — the flight has flown. Clear the actual times first "
                "if they were entered in error."
            )
        return fields

    if both_actuals and current_status == "PLANNED":
        fields["status"] = "OPERATED"

    return fields


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

    fields = _apply_operated_rule(existing, fields)

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


def set_flight_disrupted(flight_id: int, reason: str, app_user: Optional[str] = None) -> None:
    """Mark a flight DISRUPTED. Manual only — the system never applies
    this label on its own.

    reason is REQUIRED, mirroring cancel_flight(). A disruption nobody
    can explain later is the case an auditor asks about, and this is the
    audit trail; `linked_disruption_event` on audit_log exists for
    exactly this and is populated rather than adding a column.

    Legal only from PLANNED. Not from OPERATED (a flight that flew is
    not relabelled — the disruption of a flown flight belongs in remarks
    and in this audit record) and not from CANCELLED (terminal).
    """
    if not (reason or "").strip():
        raise ValueError("A reason is required to mark a flight DISRUPTED")

    existing = get_flight(flight_id)
    if existing is None:
        raise ValueError(f"No flight with flight_id={flight_id}")
    if existing["status"] != "PLANNED":
        raise ValueError(
            f"flight {flight_id} is {existing['status']} — DISRUPTED can only be "
            f"applied to a PLANNED flight"
        )

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE flights SET status = 'DISRUPTED', updated_at = NOW() "
            "WHERE flight_id = :fid"
        ), {"fid": flight_id})

    log_audit(
        action_type="FLIGHT_DISRUPTED",
        affected_flight=flight_id,
        original_state=str(existing["status"]),
        changed_state="DISRUPTED",
        reason=reason.strip(),
        linked_disruption_event=reason.strip(),
        app_user=app_user,
    )


def clear_flight_disruption(flight_id: int, reason: str, app_user: Optional[str] = None) -> str:
    """Remove a DISRUPTED label. Returns the resulting status.

    Where it lands is decided by RECORDED FACT, not by the caller: a
    flight with both actual times has flown, so clearing the label
    yields OPERATED, not PLANNED. Offering PLANNED there would be a
    control that says one thing and does another — the automatic rule
    in _apply_operated_rule() would immediately move it anyway. The UI
    labels the action with this outcome rather than warning about it
    afterwards.

    reason is REQUIRED, like the forward transition. An unaudited undo
    leaves a record showing a flight that was never disrupted, when in
    fact it was labelled and then relabelled — which is precisely what
    an auditor asks about.
    """
    if not (reason or "").strip():
        raise ValueError("A reason is required to clear a DISRUPTED label")

    existing = get_flight(flight_id)
    if existing is None:
        raise ValueError(f"No flight with flight_id={flight_id}")
    if existing["status"] != "DISRUPTED":
        raise ValueError(
            f"flight {flight_id} is {existing['status']}, not DISRUPTED — "
            f"nothing to clear"
        )

    both_actuals = (
        existing["dep_time_actual"] is not None and not pd.isna(existing["dep_time_actual"])
        and existing["arr_time_actual"] is not None and not pd.isna(existing["arr_time_actual"])
    )
    new_status = "OPERATED" if both_actuals else "PLANNED"

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE flights SET status = :status, updated_at = NOW() "
            "WHERE flight_id = :fid"
        ), {"status": new_status, "fid": flight_id})

    log_audit(
        action_type="FLIGHT_DISRUPTION_CLEARED",
        affected_flight=flight_id,
        original_state="DISRUPTED",
        changed_state=new_status,
        reason=reason.strip(),
        app_user=app_user,
    )
    return new_status
