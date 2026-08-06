-- ============================================================
-- 011_rotation_templates.sql
--
-- Phase 7 groundwork (2026-08-04): recurring schedule templates.
-- Template layer ONLY — the roster generator itself, the approval/
-- publish workflow, and any UI are separate, later pieces.
--
-- WHY THIS EXISTS: roster.duty_id (003_roster_table.sql) is a fresh
-- UUID generated at ASSIGNMENT time (services/assignment_service.py's
-- _validate_new_duty()), not at flight-creation time — nothing in
-- `flights` declares which rows belong to one rotation today. A
-- generator inferring rotation membership from Flight Log alone
-- (aircraft, timing, continuity) would be guessing, and a wrong guess
-- here is a silently wrong FDP — this project's most-repeated bug
-- class (see this same file's own header note). These tables make
-- rotation membership DECLARED data instead of an inference.
--
-- Four new tables:
--   rotation_templates      -- the recurring pattern's identity + validity window
--   rotation_template_legs  -- its ordered legs, as time-of-day + day_offset (no calendar date)
--   rotation_instances      -- one row per calendar occurrence, DRAFT until approved
--   rotation_instance_legs  -- that occurrence's legs, with real computed datetimes
--
-- Nothing here is ever visible to Flight Log, reports, or
-- roster_coverage() — those all read `flights`, and a draft is never
-- inserted there. Promotion (DRAFT -> real `flights` rows via the
-- existing flight_service.add_flight()) is a later piece; this
-- migration only adds flights.rotation_instance_id (nullable) so that
-- future promotion can trace an approved flight back to the exact
-- template version and date that produced it. NULL for every Control
-- Room ad-hoc flight, exactly as today.
-- ============================================================

-- Needed for the EXCLUDE constraint below (equality + range overlap
-- in one constraint). Confirm this extension is actually available on
-- Supabase's plan before relying on it there — works locally against
-- Postgres 16, not yet confirmed on Supabase specifically as of this
-- migration.
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS rotation_templates (
    id                SERIAL PRIMARY KEY,
    rotation_code     VARCHAR(50) NOT NULL,   -- e.g. 'EPE-786-787' — stable identity across versions
    description       TEXT,
    days_of_week      SMALLINT[] NOT NULL,    -- ISO weekday numbers, 1=Mon..7=Sun
    effective_from    DATE NOT NULL,
    effective_until   DATE,                   -- NULL = effective until further notice (the normal case)
    version           INTEGER NOT NULL DEFAULT 1,
    superseded_by     INTEGER REFERENCES rotation_templates(id),
    created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_rotation_templates_days_of_week
        CHECK (days_of_week <@ ARRAY[1,2,3,4,5,6,7]::smallint[]),
    CONSTRAINT chk_rotation_templates_dates
        CHECK (effective_until IS NULL OR effective_until >= effective_from),

    -- THE actual guarantee against a silently duplicated rotation: no
    -- two versions of the same rotation_code may have overlapping
    -- effective date ranges. A NULL effective_until is treated as
    -- infinity, so creating v2 while v1 is still open-ended is
    -- rejected unless v1's effective_until is closed within the same
    -- transaction. '[]' is an INCLUSIVE bound on both ends — v1 ending
    -- 2026-08-31 and v2 starting 2026-08-31 also collide (a single day
    -- cannot belong to two versions), which is why
    -- rotation_template_service.create_new_version() closes the prior
    -- version's effective_until to the day BEFORE the new version's
    -- effective_from, not the same day.
    --
    -- DEFERRABLE INITIALLY DEFERRED, deliberately: create_new_version()
    -- has a genuine circular dependency — the new version's row must
    -- exist before superseded_by can reference its id, but the OLD
    -- version's effective_until must already be closed before the NEW
    -- version's INSERT for a plain (non-deferred) constraint to accept
    -- it, since two open-ended ranges for the same rotation_code always
    -- overlap. Deferring the check to COMMIT lets both statements
    -- happen in either order inside one transaction — the constraint
    -- only has to be satisfied by the time the transaction actually
    -- commits, not after every individual statement. The guarantee
    -- itself is unaffected: a transaction that commits with a genuine
    -- overlap still fails, only the TIMING of the check moves from
    -- per-statement to per-transaction. This is the standard Postgres
    -- pattern for exactly this "two rows must be updated together to
    -- stay valid" shape — confirmed against real Postgres 16.
    CONSTRAINT excl_rotation_templates_no_overlap
        EXCLUDE USING gist (
            rotation_code WITH =,
            daterange(effective_from, COALESCE(effective_until, 'infinity'::date), '[]') WITH &&
        ) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS idx_rotation_templates_rotation_code ON rotation_templates(rotation_code);

CREATE TABLE IF NOT EXISTS rotation_template_legs (
    id              SERIAL PRIMARY KEY,
    template_id     INTEGER NOT NULL REFERENCES rotation_templates(id),
    leg_order       SMALLINT NOT NULL,
    flight_no       VARCHAR(20),
    origin          VARCHAR(10) NOT NULL,
    destination     VARCHAR(10) NOT NULL,
    dep_time        TIME NOT NULL,   -- time-of-day only — a template has no calendar date of its own
    arr_time        TIME NOT NULL,
    day_offset      SMALLINT NOT NULL DEFAULT 0,  -- 0 = same calendar day as the rotation's nominal start date
    domestic        BOOLEAN NOT NULL,             -- per-leg, matches flights.domestic's own granularity

    CONSTRAINT uq_rotation_template_legs_order UNIQUE (template_id, leg_order)
);

-- Immutability, enforced at the database level, not by convention —
-- this project has been burned by "by convention" before
-- (ensure_basic_crew_table()). rotation_template_legs is a hard block
-- on every UPDATE/DELETE: legs belong permanently to their template_id
-- version, full stop, no legitimate reason to ever touch one after
-- insert.
CREATE OR REPLACE FUNCTION reject_rotation_template_legs_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'rotation_template_legs rows are immutable — create a new template version instead (template_id=%)',
        COALESCE(OLD.template_id, NEW.template_id);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rotation_template_legs_immutable
    BEFORE UPDATE OR DELETE ON rotation_template_legs
    FOR EACH ROW EXECUTE FUNCTION reject_rotation_template_legs_mutation();

-- rotation_templates itself needs ONE narrow, legitimate mutation:
-- closing an open-ended effective_until (and recording superseded_by)
-- when a new version supersedes it — see
-- rotation_template_service.create_new_version(). Everything else
-- about an existing row (rotation_code, description, days_of_week,
-- effective_from, version) is permanently fixed at insert, and
-- effective_until itself can only be closed ONCE, never re-edited
-- after that (OLD.effective_until IS NOT NULL blocks any further
-- change). DELETE is never allowed at all.
CREATE OR REPLACE FUNCTION guard_rotation_templates_mutation() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'rotation_templates rows are never deleted — create a new version instead (id=%)', OLD.id;
    END IF;

    IF NEW.rotation_code   IS DISTINCT FROM OLD.rotation_code
       OR NEW.description   IS DISTINCT FROM OLD.description
       OR NEW.days_of_week   IS DISTINCT FROM OLD.days_of_week
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.version        IS DISTINCT FROM OLD.version
       OR OLD.effective_until IS NOT NULL THEN
        RAISE EXCEPTION
            'rotation_templates rows are immutable except for closing an open effective_until exactly once (id=%)',
            OLD.id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rotation_templates_guard
    BEFORE UPDATE OR DELETE ON rotation_templates
    FOR EACH ROW EXECUTE FUNCTION guard_rotation_templates_mutation();

CREATE TABLE IF NOT EXISTS rotation_instances (
    id              SERIAL PRIMARY KEY,
    template_id     INTEGER NOT NULL REFERENCES rotation_templates(id),
    rotation_code   VARCHAR(50) NOT NULL,   -- denormalized from the template at creation time, so the
                                             -- idempotency check below doesn't need a join, and so "one
                                             -- instance per rotation per date, regardless of which
                                             -- version produced it" is enforced directly at the DB level too
    version         INTEGER NOT NULL,
    rotation_date   DATE NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_rotation_instances_status CHECK (status IN ('DRAFT', 'APPROVED', 'REJECTED')),
    CONSTRAINT uq_rotation_instances_code_date UNIQUE (rotation_code, rotation_date)
);

CREATE INDEX IF NOT EXISTS idx_rotation_instances_status ON rotation_instances(status);

CREATE TABLE IF NOT EXISTS rotation_instance_legs (
    id                  SERIAL PRIMARY KEY,
    instance_id         INTEGER NOT NULL REFERENCES rotation_instances(id),
    leg_order           SMALLINT NOT NULL,
    flight_no           VARCHAR(20),
    origin              VARCHAR(10) NOT NULL,
    destination         VARCHAR(10) NOT NULL,
    dep_time_planned    TIMESTAMP NOT NULL,
    arr_time_planned    TIMESTAMP NOT NULL,
    domestic            BOOLEAN NOT NULL,

    CONSTRAINT uq_rotation_instance_legs_order UNIQUE (instance_id, leg_order),
    CONSTRAINT chk_rotation_instance_legs_times CHECK (arr_time_planned > dep_time_planned)
);

-- Forward-traceability for the later promotion piece — NOT written to
-- by anything in this migration or in services/rotation_template_
-- service.py. NULL for every ad-hoc Control Room flight, exactly as
-- today; set only when a future approval step calls the existing
-- flight_service.add_flight() to promote an APPROVED rotation_instance
-- into real flights rows.
ALTER TABLE flights ADD COLUMN IF NOT EXISTS rotation_instance_id INTEGER REFERENCES rotation_instances(id);
