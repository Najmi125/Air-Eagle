-- ============================================================
-- 006_flights_domestic_column.sql
--
-- core/duty_builder.py requires an explicit domestic=True/False to
-- pick the correct D7.1.2 report/debrief buffer (45/15 min domestic
-- vs 60/30 min international) — it deliberately does not guess this
-- from origin/destination codes, since Air Eagle's actual route mix
-- was never confirmed (see HANDOVER.md).
--
-- This value belongs on the flight, not asked again at every crew
-- assignment: it's a property of the route (KHI-LHE is domestic
-- regardless of which crew member flies it), not a property of who's
-- flying it. Asking per-assignment risked two different controllers
-- answering differently for the same flight.
--
-- NOT NULL, no default: every flight must have this decided at
-- creation time, same as origin/destination already are. The old
-- repo's pattern of silently defaulting ambiguous fields is exactly
-- what this repo exists to not repeat.
-- ============================================================

ALTER TABLE flights ADD COLUMN IF NOT EXISTS domestic BOOLEAN;

-- Enforced as a separate step so this migration stays safe to run
-- against a table that might already have rows (NOT NULL can't be
-- added in the same statement as the column without a default, and
-- we deliberately don't want a default here).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flights' AND column_name = 'domestic'
        AND is_nullable = 'NO'
    ) THEN
        -- Only safe to enforce NOT NULL if there are no existing
        -- NULL rows. Pre-production (no real flights yet), this is
        -- always true. If this migration is ever run against a DB
        -- with existing flight rows lacking a domestic value, this
        -- will fail loudly rather than silently leave it nullable —
        -- which is correct: that data gap needs a human decision,
        -- not a migration guessing on their behalf.
        ALTER TABLE flights ALTER COLUMN domestic SET NOT NULL;
    END IF;
END $$;
