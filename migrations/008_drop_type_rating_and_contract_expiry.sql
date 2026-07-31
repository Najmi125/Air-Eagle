-- ============================================================
-- 008_drop_type_rating_and_contract_expiry.sql
--
-- Removes type_rating_expiry and contract_expiry from the crew
-- table entirely — not just excluded from the qualification gate's
-- checks (services/assignment_service.py AE-CREW-QUAL-001), but
-- removed from the data model altogether.
--
-- Why: both columns have been empty for every real crew row
-- received from the operator so far (see the 2026-07-21 data-quality
-- findings earlier in HANDOVER.md — "Type Rating Exp" and
-- "Contract Exp" are empty for every single row, a systematic gap,
-- not a per-person one). Checking them in the qualification gate
-- meant every real crew member would hold at NEEDS_MANUAL_REVIEW
-- until the operator filled them in — genuinely correct gate
-- behavior, but a practical dead end when the operator has shown no
-- sign of tracking either field. Decision (2026-08-01): trust OCC's
-- own offline process to have already removed anyone who isn't
-- actually qualified, rather than have this system re-derive that
-- from two columns that will likely never be populated. Revisit if
-- that trust turns out to be misplaced.
--
-- No data loss of consequence: both columns are confirmed empty for
-- every crew row imported to date.
-- ============================================================

ALTER TABLE crew DROP COLUMN IF EXISTS type_rating_expiry;
ALTER TABLE crew DROP COLUMN IF EXISTS contract_expiry;
