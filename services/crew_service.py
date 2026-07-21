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
    "medical_expiry", "type_rating_expiry", "lpc_opc_expiry",
    "line_check_expiry", "sep_expiry", "crm_expiry", "dg_expiry",
    "contract_expiry", "remarks",
}

ROLE_PREFIXES = {"CPT": "CPT", "FO": "FO", "LM": "LM", "ENGR": "ENGR"}


def _generate_crew_id(role: str) -> str:
    """CPT-01, FO-01, LM-01, ENGR-01, ... or CREW-01 for anything
    else (the template explicitly allows role='Other')."""
    prefix = ROLE_PREFIXES.get(role.strip().upper(), "CREW")
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

    crew_id = _generate_crew_id(crew_data["role"])

    fields = {k: v for k, v in crew_data.items() if k in UPDATABLE_FIELDS}
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


def update_crew(crew_id: str, updates: dict, app_user: Optional[str] = None) -> None:
    """Update one or more fields on an existing crew member.
    Raises ValueError if crew_id doesn't exist or updates contains
    no recognized fields."""
    existing = get_crew(crew_id)
    if existing is None:
        raise ValueError(f"No crew member with crew_id={crew_id}")

    fields = {k: v for k, v in updates.items() if k in UPDATABLE_FIELDS}
    if not fields:
        raise ValueError("No updatable fields provided")

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


def deactivate_crew(crew_id: str, reason: Optional[str] = None, app_user: Optional[str] = None) -> None:
    """Soft-delete only — is_active=FALSE, row is never removed.
    Matches the platform's permanent-record principle (same reasoning
    as Flight Log never hard-deleting a flight)."""
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
