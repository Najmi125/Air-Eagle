-- ============================================================
-- 001_crew_table.sql
--
-- Matches the crew data collection template already sent to the
-- operator (AirEagle_Crew_Data_Simple.xlsx). Column names here are
-- the SQL-safe equivalents of that template's headers.
--
-- crew_id is FTLguard's own internally-assigned identifier (e.g.
-- 'CPT-01'), separate from operator_staff_id, which is whatever
-- internal employee number Air Eagle already uses (if any) — kept
-- as a reference field, not the primary key, per what was already
-- promised to the operator on the template's instructions tab.
--
-- Deliberately NOT included yet: Loadmaster-specific or AME/Engr-
-- specific qualification columns. The template explicitly deferred
-- these ("operator will add column if needed") pending Monday's
-- data and the open "Engr" role clarification (flight-deck FE vs
-- line-maintenance AME). Add them as a new numbered migration once
-- that's confirmed — don't guess the shape now.
--
-- `base` has no default value. Do not hardcode 'KHI' here even
-- though that's Air Eagle's actual current base — that exact
-- pattern was found and removed twice already in the old repo
-- (utils/schema.py, utils/schema_v2.py). Airline-specific values
-- belong in configs/airlines/AEAGLE/, not baked into schema
-- defaults, even in an airline-specific repo — this repo may end
-- up cloned as the starting point for the next client.
--
-- All timestamps in this schema are stored as naive TIMESTAMP,
-- UTC by convention — matching core/legality/pcaa_ano012_core.py's
-- Duty dataclass, which uses naive datetimes with a separate
-- tz_offset field rather than tz-aware datetimes. Keep this
-- consistent across the whole schema; do not mix in TIMESTAMPTZ.
-- ============================================================

CREATE TABLE IF NOT EXISTS crew (
    crew_id             VARCHAR(20) PRIMARY KEY,
    operator_staff_id   VARCHAR(50),
    name                VARCHAR(100) NOT NULL,
    role                VARCHAR(20) NOT NULL,   -- CPT / FO / LM / ENGR / Other — intentionally not CHECK-constrained, see note below
    date_of_birth       DATE,
    nationality         VARCHAR(50),
    base                VARCHAR(10),
    phone               VARCHAR(20),
    email               VARCHAR(100),
    license_no          VARCHAR(50),
    license_expiry      DATE,
    medical_expiry      DATE,
    type_rating_expiry  DATE,
    lpc_opc_expiry      DATE,
    line_check_expiry   DATE,
    sep_expiry          DATE,
    crm_expiry          DATE,
    dg_expiry           DATE,
    contract_expiry     DATE,
    remarks             TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

-- `role` is not CHECK-constrained to a fixed list. This is a
-- deliberate choice, not an oversight: the operator's own crew data
-- may introduce a role we haven't anticipated (the template itself
-- includes "Other" as a valid value), and a CHECK constraint would
-- reject the row instead of accepting it for review. Validate role
-- values at the service layer (Phase 6), where a rejection can
-- carry a clear message — not at the schema layer, where it can't.

CREATE INDEX IF NOT EXISTS idx_crew_role ON crew(role);
CREATE INDEX IF NOT EXISTS idx_crew_is_active ON crew(is_active);
