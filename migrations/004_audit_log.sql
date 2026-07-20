-- ============================================================
-- 004_audit_log.sql
--
-- Single, unified audit table for every service write — crew
-- changes, flight changes, assignments, overrides, disruptions.
-- Per Project Instructions Section 16's required field list and
-- the Ownership Table ("Audit record -> audit_log table ->
-- audit_service.py -> Called inside every service write").
--
-- Deliberately NOT split into per-feature audit tables (e.g. a
-- separate crew_audit vs assignment_audit). One table, one
-- authority, append-only. Fields irrelevant to a given action type
-- (e.g. legality_result on a plain crew edit) are simply NULL for
-- that row — that's expected, not a schema gap.
--
-- This table is intentionally built in Phase 4, not earlier: Phase
-- 3 had no service-layer writes yet to audit, and building this
-- ahead of the first real write would have been exactly the kind
-- of speculative schema Section 3's FUTURE classification warns
-- against. Phase 4 is the first phase with real writes
-- (crew_service.py), so this is now needed, not speculative.
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id                    SERIAL PRIMARY KEY,
    transaction_id               VARCHAR(50),
    app_user                     VARCHAR(100),
    "timestamp"                   TIMESTAMP NOT NULL DEFAULT NOW(),
    airline_code                 VARCHAR(10),
    action_type                  VARCHAR(50) NOT NULL,
    original_state                TEXT,
    changed_state                 TEXT,
    reason                       TEXT,
    rule_applied                 VARCHAR(100),
    legality_result               VARCHAR(20),
    warning_or_failure_reason     TEXT,
    override_reason               TEXT,
    approver                     VARCHAR(100),
    affected_crew                 VARCHAR(20),
    affected_flight               INTEGER,
    affected_duty                 VARCHAR(50),
    affected_aircraft             VARCHAR(20),
    linked_disruption_event       VARCHAR(50),
    data_source                   VARCHAR(50)
);

CREATE INDEX IF NOT EXISTS idx_audit_affected_crew ON audit_log(affected_crew);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log("timestamp");
CREATE INDEX IF NOT EXISTS idx_audit_action_type ON audit_log(action_type);
