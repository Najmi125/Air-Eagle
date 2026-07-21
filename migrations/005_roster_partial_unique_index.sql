-- ============================================================
-- 005_roster_partial_unique_index.sql
--
-- Phase 3's roster table had UNIQUE(crew_id, flight_id, role_assigned)
-- unconditionally. That's correct for preventing an accidental
-- duplicate active assignment, but it has a real gap surfaced while
-- building Phase 6's unassign flow: unassigning a crew member should
-- mark status='CANCELLED' (same permanent-record pattern as
-- flights.cancel_flight() — never delete), but a cancelled row would
-- then permanently block ever re-assigning that same person to that
-- same flight/role again, since the constraint doesn't know to
-- ignore cancelled rows.
--
-- Fix: replace the unconditional UNIQUE constraint with a partial
-- unique index that only applies to non-cancelled rows. A crew
-- member can only hold one ACTIVE assignment to a given
-- (flight, role) at a time, but a cancelled one doesn't block a new
-- one from being created afterward.
-- ============================================================

ALTER TABLE roster DROP CONSTRAINT IF EXISTS uq_roster_crew_flight_role;

CREATE UNIQUE INDEX IF NOT EXISTS uq_roster_crew_flight_role_active
    ON roster (crew_id, flight_id, role_assigned)
    WHERE status != 'CANCELLED';
