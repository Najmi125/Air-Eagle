-- ============================================================
-- 020_backfill_operated_status.sql
--
-- Sets status = 'OPERATED' on flights that demonstrably flew but were
-- never marked, because until 2026-08-21 nothing in the application
-- could mark them.
--
-- WHY THIS EXISTS. flights.status could only ever become CANCELLED:
-- cancel_flight() was its sole writer, and recording actual times wrote
-- the two timestamps and nothing else. So a flight that flew stayed
-- PLANNED forever, DISRUPTED was unreachable entirely, and the Flt
-- Schedule filter offered four states of which two could never occur.
-- services/flight_service.py's _apply_operated_rule() closes that going
-- forward; this closes it backwards.
--
-- ORDERING — APPLY THIS BEFORE DEPLOYING THE CODE THAT SHIPS THE RULE.
-- This is a SEPARATE requirement from the reboot rule, not part of it.
-- If the code lands first, newly-recorded actuals start setting
-- OPERATED while older rows with identical data still read PLANNED, and
-- the record becomes inconsistent in a way that looks like the bug
-- rather than the fix. Applying this first means every row with both
-- actuals reads the same, whenever it was entered.
--
-- SCOPED TO 'PLANNED' DELIBERATELY:
--
--   * CANCELLED is terminal. A cancelled flight with actual times
--     recorded against it stays cancelled — cancellation is a
--     deliberate act and must not be undone by a backfill.
--   * DISRUPTED is a controller's manual judgement and outranks the
--     automatic label. "It flew" is recoverable from the actual times;
--     "it was disrupted" is recoverable from nothing else.
--   * OPERATED rows are already correct and are not rewritten, which
--     also makes this migration idempotent.
--
-- Backfilling rather than leaving history alone was a decision, not an
-- omission: PLANNED on these rows was never a judgement anyone made,
-- only the absence of any way to record one. The trial database holds
-- very little data, so the cost either way is trivial — what is not
-- trivial is the first reconciliation being wrong about flights that
-- demonstrably flew.
--
-- READ BEFORE WRITING A REPORT ON THIS COLUMN: status does NOT mean
-- "flew", and cannot, because one column cannot hold both OPERATED and
-- DISRUPTED — so some flown flights will always carry another label.
-- The honest test for "which flights actually flew" is
-- `dep_time_actual IS NOT NULL AND arr_time_actual IS NOT NULL`. See
-- HANDOVER.md (2026-08-21) for why these are two different jobs.
-- ============================================================

UPDATE flights
SET status = 'OPERATED',
    updated_at = NOW()
WHERE status = 'PLANNED'
  AND dep_time_actual IS NOT NULL
  AND arr_time_actual IS NOT NULL;
