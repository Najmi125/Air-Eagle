-- ============================================================
-- 016_operating_position.sql
--
-- The flight-deck crew package (2026-08-12) — four real defects,
-- reproduced against real Postgres on main before this was written:
-- no seat model at all (five Captains on one flight, all ALLOWED —
-- the existing uq_roster_crew_flight_role_active index (migration
-- 005) only stops the SAME person double-booking, never stops two
-- DIFFERENT people both holding role_assigned='CPT' on one flight);
-- partial unassign corrupting a duty; a pilot committed as PLANNED
-- before the pair is known legal; find_legal_candidates_for_duty()
-- offering candidates the real gate would refuse.
--
-- New operator instruction reshaping the model: both operating pilots
-- may be Captains. Grade (CPT/FO — what the person is qualified as)
-- and operating position (Commander/PIC or Second Pilot/SIC — what
-- they do on THIS flight) are two different things that were
-- conflated. Grade stays on crew.role, unchanged. Operating position
-- is new, lives on the assignment.
--
-- role_assigned keeps its existing meaning and values (CPT/FO/LM/ENGR
-- grade-as-filled) untouched — every FTL_EXEMPT_ROLES check and
-- crew-role-match validation keys off it and needs zero changes.
-- operating_position is additive: NULL for LM/ENGR (not part of any
-- pair), 'COMMANDER'/'SECOND_PILOT' for pilot seats only.
--
-- Architectural fork, assessed not assumed (see HANDOVER.md for the
-- full comparison): a new crew_packages-style table duplicating "who
-- is assigned" would need permanent sync with roster, the exact bug
-- class 003_roster_table.sql's own header warns is this platform's
-- single most repeated bug. Chosen instead: this column, plus the
-- partial-unique-index idiom this project already uses twice
-- (migrations 005, 012) for "real invariant, DB-backed."
--
-- Seat-uniqueness is scoped to flight_id, NOT duty_id. duty_id is
-- confirmed per-crew-member (assign_crew_to_duty() generates a fresh
-- DUTY-{uuid} on every call, one per person, not shared across a
-- pair) -- forcing it to become shared would break its meaning
-- everywhere else in the FDP engine, which treats duty_id as "one
-- person's own continuous duty." flight_id is the genuinely common
-- key between a Commander's and a Second Pilot's separate rows for
-- one sector, and does NOT collide with one pilot's own multi-sector
-- rows (each sector is its own flight_id) -- verified directly
-- against real Postgres before this migration was written, not
-- assumed: two different "Commanders" on one flight is rejected; the
-- same Commander across both sectors of a 2-sector duty is accepted;
-- the same person as both Commander and Second Pilot on one flight is
-- already rejected by the EXISTING uq_roster_crew_flight_role_active
-- index (role_assigned is fixed to that person's one real grade, so a
-- second row for them on the same flight always collides there
-- regardless of operating_position) -- no new constraint needed for
-- that case.
-- ============================================================

ALTER TABLE roster ADD COLUMN IF NOT EXISTS operating_position VARCHAR(20);

ALTER TABLE roster ADD CONSTRAINT chk_roster_operating_position
    CHECK (operating_position IN ('COMMANDER', 'SECOND_PILOT') OR operating_position IS NULL);

-- Commander must be CPT-graded. Second Pilot may be CPT or FO graded
-- (no separate right-seat qualification -- operator confirmed any
-- current Captain may fly Second Pilot), so no corresponding check is
-- needed on that side. Single-row check, no cross-row query, so a
-- plain CHECK is sufficient -- no trigger needed.
ALTER TABLE roster ADD CONSTRAINT chk_roster_commander_is_cpt
    CHECK (operating_position != 'COMMANDER' OR role_assigned = 'CPT');

CREATE UNIQUE INDEX IF NOT EXISTS uq_roster_flight_operating_position_active
    ON roster (flight_id, operating_position)
    WHERE status != 'CANCELLED' AND operating_position IS NOT NULL;
