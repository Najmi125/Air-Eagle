-- ============================================================
-- 013_roster_proposed_status.sql
--
-- Phase 7 final piece (2026-08-04): the roster generator. Mirrors
-- 009_roster_needs_review_status.sql's exact pattern — add one more
-- value to roster.status's allowed vocabulary, same DROP/ADD
-- CONSTRAINT shape.
--
-- Adds 'PROPOSED'. Needed because assign_crew_to_duty()'s INSERT never
-- set status explicitly (relying on the schema default 'PLANNED'),
-- which means every assignment became immediately live the instant it
-- was written — fine for a human using the Roster page, wrong for the
-- generator, which must produce a draft OCC can review before it's
-- real, per the requirements doc's "draft -> OCC review -> publish,
-- crew sees only published" (already recorded 2026-07-21). Mirrors
-- rotation_instances' own DRAFT/APPROVED split (migrations/011) at
-- the roster level: services/roster_generator_service.py's
-- generate_for_window() writes PROPOSED rows via
-- assign_crew_to_duty(..., status="PROPOSED"); publish_window() is
-- the mechanical PROPOSED -> PLANNED flip, the analog of
-- approve_instance().
-- ============================================================

ALTER TABLE roster DROP CONSTRAINT chk_roster_status;
ALTER TABLE roster ADD CONSTRAINT chk_roster_status
    CHECK (status IN ('PLANNED', 'OPERATED', 'CANCELLED', 'DISRUPTED', 'NEEDS_REVIEW', 'PROPOSED'));
