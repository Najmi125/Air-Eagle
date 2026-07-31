-- ============================================================
-- 009_roster_needs_review_status.sql
--
-- Originally authored as migrations/008 on branch
-- step5-stale-data-fixes, in parallel with a second, independent
-- migrations/008 on branch remove-type-rating-contract-fields (drop
-- type_rating_expiry/contract_expiry) — both branched off main at
-- 007. remove-type-rating-contract-fields merged first and kept 008;
-- this one was renumbered to 009 after rebasing onto that merge, per
-- the plan recorded in HANDOVER.md's 2026-08-01 Step 5 entry.
--
-- Adds 'NEEDS_REVIEW' to roster.status's allowed values. Needed by
-- services/assignment_service.py's update_flight_actual_times_and_
-- revalidate(): when a delay recompute (core/duty_builder.py's
-- recompute_fdp_after_delay()) reveals that a duty is no longer
-- LEGAL/WARNING, the affected roster row(s) are flagged NEEDS_REVIEW
-- rather than silently left at their previous status (PLANNED/
-- OPERATED/etc) with only an audit-log alert as the only trace.
-- Matches the "never silently pass, never silently auto-reject on
-- an unresolved question" principle already used for
-- AlertStatus.NEEDS_MANUAL_REVIEW at assignment time — this is the
-- same concept applied to a duty that becomes questionable AFTER it
-- was already saved, not just at the moment of assignment.
--
-- Note: OPERATED and DISRUPTED have been allowed values on this
-- column since 003_roster_table.sql but are not yet written by any
-- code path — this migration doesn't change that, it only adds one
-- more value to the same not-yet-fully-wired vocabulary.
-- ============================================================

ALTER TABLE roster DROP CONSTRAINT chk_roster_status;
ALTER TABLE roster ADD CONSTRAINT chk_roster_status
    CHECK (status IN ('PLANNED', 'OPERATED', 'CANCELLED', 'DISRUPTED', 'NEEDS_REVIEW'));
