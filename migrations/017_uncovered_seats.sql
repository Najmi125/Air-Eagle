-- ============================================================
-- 017_uncovered_seats.sql
--
-- The one durability gap the operating_position column (migration
-- 016) can't close on its own: an uncovered seat has no roster row at
-- all, so there's nothing to query once a page refresh loses the
-- generator's in-memory GenerationSummary. Two bad fixes rejected
-- deliberately: a sentinel/NULL crew_id row (breaks roster.crew_id's
-- NOT NULL REFERENCES crew(crew_id), needs filtering everywhere,
-- risks the exact "silently bad data" bug class this project has
-- already been burned by once); or deriving UNCOVERED live by diffing
-- APPROVED rotation_instances against roster coverage (loses the real
-- rejection reason from the specific attempted-candidate ordering at
-- generation time, and could disagree with that original attempt as
-- fairness bookkeeping shifts on a later recompute).
--
-- This table only ever records the NEGATIVE case -- created when
-- services/roster_generator_service.py's pair search fails to fill a
-- seat, with the real reason attached. Nothing for the covered case
-- (roster already fully owns that, no duplication, no sync burden).
--
-- Keyed on rotation_instance_id because that's the only durable
-- anchor an uncovered seat has -- but writers are NOT limited to the
-- generator. services/assignment_service.py's
-- remove_assignment_from_duty() also writes here, when a
-- manually-unassigned pilot held a rotation-linked seat: a controller
-- removing a Commander leaves that seat just as genuinely uncovered
-- as a generator search that never found one, and this table is the
-- single durable source of truth for "which seats are currently
-- empty" -- not a generator-only failure log. A manually-vacated seat
-- with no durable record here would undercut the point of the table.
-- (Control Room's ad-hoc path remains the one exception: it's
-- synchronous and always resolves immediately -- REJECTED or written
-- -- so it never leaves a durable gap to record.)
-- ============================================================

CREATE TABLE IF NOT EXISTS uncovered_seats (
    id                    SERIAL PRIMARY KEY,
    rotation_instance_id  INTEGER NOT NULL REFERENCES rotation_instances(id),
    operating_position    VARCHAR(20) NOT NULL CHECK (operating_position IN ('COMMANDER', 'SECOND_PILOT')),
    reason                TEXT NOT NULL,
    generated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    resolved_at           TIMESTAMP,

    -- At most one OPEN row per seat per rotation. Re-running Generate,
    -- or a fresh manual unassign, updates this same row (reason +
    -- generated_at refreshed, resolved_at cleared if it had been set)
    -- rather than accumulating duplicates -- see the service-layer
    -- write path, not enforced by this constraint alone since it
    -- doesn't scope by resolved_at IS NULL (a partial unique index
    -- doing that is deliberately NOT used here: it would let a
    -- resolved row's slot silently get reused rather than requiring
    -- the write path to make an explicit resolve-or-reopen choice).
    CONSTRAINT uq_uncovered_seats_open UNIQUE (rotation_instance_id, operating_position)
);

CREATE INDEX IF NOT EXISTS idx_uncovered_seats_open
    ON uncovered_seats (rotation_instance_id)
    WHERE resolved_at IS NULL;
