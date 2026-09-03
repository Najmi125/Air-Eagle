"""
services/crew_service.py

Canonical write path for the crew table. Per the Ownership Table
and Section 17 ("No raw SQL in Streamlit pages"), pages/2_Crew_Data.py
calls these functions — it does not write SQL directly, and it does
not create or alter the crew table (that's migrations/001_crew_table.sql's
job, exclusively).

crew_id is always auto-generated here, never taken from user input.
This matches what the crew data collection template already told the
operator: "FTLguard will assign its own internal Crew ID at
data-entry time — you don't need to invent one." Whatever ID the
operator's own systems use goes in operator_staff_id instead, kept
as a reference, not used as the primary key.
"""
from typing import Optional
import pandas as pd
from sqlalchemy import text

from db.db import get_engine
from services.audit_service import log_audit

REQUIRED_FIELDS = {"name", "role"}

# Columns updatable via update_crew(). Deliberately an allowlist —
# crew_id, created_at are never updated this way, and this list is
# also what protects against building a dynamic UPDATE statement
# from arbitrary caller-supplied keys.
UPDATABLE_FIELDS = {
    "operator_staff_id", "name", "role", "date_of_birth", "nationality",
    "base", "phone", "email", "license_no", "license_expiry",
    "medical_expiry", "ir_expiry", "sim_expiry",
    "route_check_expiry", "sep_expiry", "crm_expiry", "dg_expiry",
    "remarks",
}

ROLE_PREFIXES = {"CPT": "CPT", "FO": "FO", "LM": "LM", "ENGR": "ENGR"}

# Known real-world synonyms for the same canonical role, confirmed
# 2026-07-21: "Engr is AME" — the user's own operational term for
# the role the template's dropdown calls "ENGR". Normalized here, at
# the single write boundary, so every downstream consumer (the FTL
# exemption check in assignment_service.py, crew_id generation
# below, role-match validation) can trust crew.role is already
# canonical and never needs to repeat this mapping or do its own
# case-insensitive comparison. This is what actually closes the bug
# a review found: a role stored as "AME" or "Engr" (any case) would
# silently fail to match FTL_EXEMPT_ROLES = {"LM", "ENGR"}, wrongly
# subjecting an exempt person to FDP/rest math that doesn't apply to
# them — or, depending on where the mismatch happened, the reverse.
ROLE_SYNONYMS = {"AME": "ENGR", "CAPT": "CPT"}


def _normalize_role(role: str) -> str:
    """Canonical form: uppercase, then map known synonyms. Applied
    once here, at every write path (add_crew, update_crew) — nowhere
    else in the codebase should need its own role normalization
    logic if this is applied consistently at the boundary."""
    canonical = role.strip().upper()
    return ROLE_SYNONYMS.get(canonical, canonical)


def _generate_crew_id(role: str) -> str:
    """CPT-01, FO-01, LM-01, ENGR-01, ... or CREW-01 for anything
    else (the template explicitly allows role='Other'). Assumes role
    is already normalized (see _normalize_role) — callers in this
    file always normalize before calling this."""
    prefix = ROLE_PREFIXES.get(role, "CREW")
    engine = get_engine()
    with engine.connect() as conn:
        existing = conn.execute(text(
            "SELECT crew_id FROM crew WHERE crew_id LIKE :pattern"
        ), {"pattern": f"{prefix}-%"}).fetchall()

    max_seq = 0
    for row in existing:
        try:
            seq = int(row[0].split("-")[-1])
            max_seq = max(max_seq, seq)
        except (ValueError, IndexError):
            continue

    return f"{prefix}-{max_seq + 1:02d}"


def add_crew(crew_data: dict, app_user: Optional[str] = None) -> str:
    """
    Insert a new crew member. Returns the auto-generated crew_id.

    Raises ValueError if required fields (name, role) are missing —
    this fails loudly rather than inserting a partial/invalid row,
    per the platform's "never fill missing policy with silent
    defaults" principle.
    """
    missing = [f for f in REQUIRED_FIELDS if not crew_data.get(f) and crew_data.get(f) is not False]
    if missing:
        raise ValueError(f"Missing required crew field(s): {', '.join(missing)}")

    fields = {k: v for k, v in crew_data.items() if k in UPDATABLE_FIELDS}
    fields["role"] = _normalize_role(fields["role"])

    crew_id = _generate_crew_id(fields["role"])
    fields["crew_id"] = crew_id

    columns = ", ".join(fields.keys())
    placeholders = ", ".join(f":{k}" for k in fields.keys())

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO crew ({columns}) VALUES ({placeholders})"
        ), fields)

    log_audit(
        action_type="CREW_ADDED",
        affected_crew=crew_id,
        changed_state=str(fields),
        app_user=app_user,
    )
    return crew_id


def update_crew(crew_id: str, updates: dict, app_user: Optional[str] = None) -> dict:
    """Update one or more fields on an existing crew member.
    Raises ValueError if crew_id doesn't exist or updates contains
    no recognized fields.

    RETURNS THE REVALIDATION RESULT (2026-09-05). Changing a
    legality-relevant field re-checks every future PLANNED duty this
    crew member holds and flags whichever no longer pass -- see
    assignment_service.revalidate_crew_duties(). Before this, an OCC
    member could set an expiry to a past date and the pilot would stay
    on next week's published roster with nothing flagged.

    The return value is what the caller reports; the flagging itself
    has already happened by the time this returns."""
    existing = get_crew(crew_id)
    if existing is None:
        raise ValueError(f"No crew member with crew_id={crew_id}")

    fields = {k: v for k, v in updates.items() if k in UPDATABLE_FIELDS}
    if not fields:
        raise ValueError("No updatable fields provided")
    if "role" in fields:
        fields["role"] = _normalize_role(fields["role"])

    set_clause = ", ".join(f"{k} = :{k}" for k in fields.keys())
    fields["crew_id"] = crew_id

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            f"UPDATE crew SET {set_clause}, updated_at = NOW() WHERE crew_id = :crew_id"
        ), fields)

    log_audit(
        action_type="CREW_UPDATED",
        affected_crew=crew_id,
        original_state=str(existing.to_dict()),
        changed_state=str({k: v for k, v in fields.items() if k != "crew_id"}),
        app_user=app_user,
    )

    return _revalidate_after_crew_change(
        crew_id, _changed_fields(existing, fields), app_user=app_user)


def _changed_fields(existing, fields: dict) -> set:
    """Which fields this write ACTUALLY changed, comparing old against
    new rather than trusting the caller's dict.

    Load-bearing, not a nicety. pages/2_Crew_Data.py submits every
    field on the form every time, changed or not, so "what the caller
    passed" would mean every save revalidated the whole roster --
    including a save where the operator opened the form and changed
    nothing. Comparing values is what makes a phone-number correction
    cost nothing and a medical-expiry correction cost a sweep.
    """
    changed = set()
    for name, new_value in fields.items():
        if name == "crew_id":
            continue
        old_value = existing[name] if name in existing else None
        if pd.isna(old_value) if _is_scalar_nan(old_value) else False:
            old_value = None
        if old_value != new_value:
            changed.add(name)
    return changed


def _is_scalar_nan(value) -> bool:
    """pd.isna() returns an ARRAY for array-likes, which cannot be used
    in a boolean context. Only ask it about scalars."""
    return not isinstance(value, (list, tuple, set, dict))


def _revalidate_after_crew_change(crew_id: str, changed_fields, app_user=None) -> dict:
    """Re-ask the roster whether it is still legal, after a crew field
    changed.

    IMPORTED INSIDE THE FUNCTION, deliberately, and this is the only
    place in services/ that does it. assignment_service imports
    crew_service at module level (`from services import crew_service`,
    and `from services.crew_service import ROLE_SYNONYMS`), so a
    module-level import in the other direction is a genuine cycle:
    crew_service would begin loading, hand control to
    assignment_service, which would then ask the half-built
    crew_service for ROLE_SYNONYMS and fail.

    Wiring this into the SERVICE rather than the page is the whole
    point -- it is what stops a second caller bypassing revalidation --
    so the cycle has to be broken here rather than avoided by moving
    the call somewhere weaker.

    Never raises into the caller's write. The crew edit has already
    committed by the time this runs; a revalidation that fails must not
    make the operator think their correction did not save. It returns
    an empty result and the audit trail still carries CREW_UPDATED.
    """
    from services import assignment_service  # noqa: PLC0415 -- see docstring

    try:
        return assignment_service.revalidate_crew_duties(
            crew_id, changed_fields, app_user=app_user)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        log_audit(
            action_type="CREW_REVALIDATION_FAILED",
            affected_crew=crew_id,
            warning_or_failure_reason=f"{type(exc).__name__}: {exc}",
            app_user=app_user,
        )
        return {"tier": 0, "checked": 0, "flagged": [], "schedule_level": [],
                "error": f"{type(exc).__name__}: {exc}"}


def deactivate_crew(crew_id: str, reason: Optional[str] = None, app_user: Optional[str] = None) -> dict:
    """Soft-delete only — is_active=FALSE, row is never removed.
    Matches the platform's permanent-record principle (same reasoning
    as Flight Log never hard-deleting a flight).

    REVALIDATES like update_crew() (2026-09-05), and this is the door
    that is easy to miss: is_active is not in UPDATABLE_FIELDS at all,
    so a revalidation wired only into update_crew() would have left
    the single most consequential crew change -- taking a pilot out of
    service while they still hold future duties -- bypassing it
    entirely."""
    existing = get_crew(crew_id)
    if existing is None:
        raise ValueError(f"No crew member with crew_id={crew_id}")

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE crew SET is_active = FALSE, updated_at = NOW() WHERE crew_id = :crew_id"
        ), {"crew_id": crew_id})

    log_audit(
        action_type="CREW_DEACTIVATED",
        affected_crew=crew_id,
        reason=reason,
        app_user=app_user,
    )

    # is_active is the change, and it is a tier-1 legality field even
    # though it never appears in UPDATABLE_FIELDS.
    return _revalidate_after_crew_change(crew_id, {"is_active"}, app_user=app_user)


def get_crew(crew_id: str) -> Optional[pd.Series]:
    engine = get_engine()
    df = pd.read_sql(text("SELECT * FROM crew WHERE crew_id = :cid"), engine, params={"cid": crew_id})
    if df.empty:
        return None
    return df.iloc[0]


def get_all_crew(active_only: bool = True) -> pd.DataFrame:
    engine = get_engine()
    query = "SELECT * FROM crew"
    if active_only:
        query += " WHERE is_active = TRUE"
    query += " ORDER BY role, crew_id"
    return pd.read_sql(text(query), engine)
