-- ============================================================
-- 000_migration_tracking.sql
--
-- This migration creates the tracking table itself. It must be
-- the first thing ever run against a fresh database, and it is
-- run automatically by scripts/run_migrations.py before anything
-- else — you should never need to run this one by hand.
--
-- Why this exists:
-- The old repo had 5 competing Python schema scripts plus a
-- separately-numbered SQL migration, with no record anywhere of
-- which had actually been applied to a given database. Every fix
-- had to be defensively written as "ADD COLUMN IF NOT EXISTS
-- everything" because there was no way to know the DB's real
-- state. This table is that missing source of truth.
--
-- Rule going forward: every migration file in this directory is
-- numbered (001_, 002_, ...), applied in order, exactly once,
-- tracked here. Never edit a migration that has already been
-- applied anywhere — write a new numbered one instead.
-- ============================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id    VARCHAR(255) PRIMARY KEY,   -- filename, e.g. '001_initial_schema.sql'
    applied_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    checksum        VARCHAR(64) NOT NULL,       -- sha256 of file content at time of apply
    applied_by      VARCHAR(100)                -- optional: who/what ran it
);
