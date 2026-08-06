-- ============================================================
-- 012_rotation_legs_flight_no_required.sql
--
-- Phase 7 approval workflow (2026-08-04): closes a real gap found on
-- review of the promotion design (rotation_instances DRAFT ->
-- APPROVED -> real flights, services/rotation_template_service.py's
-- approve_instance()). migrations/011 is already merged and immutable
-- — this is a new, separate migration, not an edit to 011.
--
-- flight_no was nullable on rotation_template_legs/rotation_instance_
-- legs, matching flights.flight_no's own nullability (deliberately
-- nullable there for genuinely ad-hoc Control Room charters). But a
-- TEMPLATE leg describes a known, named, recurring rotation — "a
-- template leg with no flight number" isn't a legitimate case the way
-- an ad-hoc charter's missing number is. Left nullable, it would also
-- silently defeat the uniqueness guarantee below: Postgres treats
-- NULL as distinct from every other NULL in a UNIQUE constraint, so a
-- leg with flight_no = NULL would never collide with another leg with
-- flight_no = NULL, even on the same rotation_instance_id.
--
-- Fails loudly at apply time if any existing row already has a NULL
-- flight_no here — the correct behavior (never a silent backfill),
-- not expected to actually happen since no real template data exists
-- yet as of this migration.
-- ============================================================

ALTER TABLE rotation_template_legs ALTER COLUMN flight_no SET NOT NULL;
ALTER TABLE rotation_instance_legs ALTER COLUMN flight_no SET NOT NULL;

-- The actual guarantee against double-promotion: one rotation should
-- never produce two flights carrying the same flight number. This
-- makes it structurally impossible, not merely dependent on
-- approve_instance()'s own idempotency check (which looks up existing
-- flights by rotation_instance_id and returns early) staying correct
-- forever — same "database constraint over convention" standard
-- migrations/011 already set for its own two invariants.
--
-- PARTIAL, not a plain UNIQUE constraint — mirrors this repo's own
-- existing precedent (migrations/005_roster_partial_unique_index.sql's
-- `WHERE status != 'CANCELLED'`) rather than relying on plain
-- UNIQUE's implicit NULL-handling to keep ad-hoc flights out of scope.
-- Explicitly scoped to rows where rotation_instance_id IS NOT NULL,
-- so Control Room's ad-hoc charters (rotation_instance_id always
-- NULL, flight_no still nullable there, untouched by this migration)
-- are never subject to this constraint at all — two ad-hoc flights
-- sharing a flight_no (or both having none) is unrelated to this
-- guarantee and stays perfectly legal.
CREATE UNIQUE INDEX IF NOT EXISTS uq_flights_rotation_instance_flight_no
    ON flights (rotation_instance_id, flight_no)
    WHERE rotation_instance_id IS NOT NULL;
