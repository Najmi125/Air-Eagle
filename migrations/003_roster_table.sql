-- ============================================================
-- 003_roster_table.sql
--
-- Crew assignment state. Per the Ownership Table: this table is
-- written only through assignment_service.py (Phase 6+), never by
-- pages inserting rows directly.
--
-- CRITICAL — read before touching this table or any query against
-- it: this table stores ONE ROW PER CREW PER FLIGHT SECTOR. A
-- multi-sector duty produces multiple rows sharing the same
-- duty_id, report_time, debrief_time, and fdp_hours (the duty-level
-- values are repeated across each sector row belonging to that
-- duty). This is the exact design Project Instructions Section 9
-- documents, and the exact shape core/duty_summary.py's
-- group_roster_rows_into_duties() and calculate_crew_duty_summary()
-- expect and are tested against (see tests/test_duty_summary.py).
--
-- duty_id is NOT NULL and NOT optional. Every historical version of
-- this table that made duty_id nullable or omitted it entirely broke
-- duty-level deduplication silently — see the old repo's
-- utils/schema.py vs migrations/001_k2_operational_support_tables.sql
-- conflict, where one definition had duty_id and the other didn't.
-- There is only one definition here; there won't be a second one to
-- disagree with it.
--
-- Never write:
--     total_fdp = roster_df['fdp_hours'].sum()
-- over rows from this table without first grouping by duty_id
-- (or calling core/duty_summary.py's helpers, which do this
-- correctly and are tested). That mistake is the single most
-- repeated bug in this platform's history.
-- ============================================================

CREATE TABLE IF NOT EXISTS roster (
    roster_id       SERIAL PRIMARY KEY,
    crew_id         VARCHAR(20) NOT NULL REFERENCES crew(crew_id),
    flight_id       INTEGER NOT NULL REFERENCES flights(flight_id),
    duty_id         VARCHAR(50) NOT NULL,
    duty_date       DATE NOT NULL,
    report_time     TIMESTAMP NOT NULL,
    debrief_time    TIMESTAMP NOT NULL,
    fdp_hours       NUMERIC(5, 2),
    role_assigned   VARCHAR(20) NOT NULL,   -- CPT / FO / LM / ENGR — position filled on THIS flight
    status          VARCHAR(20) NOT NULL DEFAULT 'PLANNED',
    override_flag   BOOLEAN NOT NULL DEFAULT FALSE,
    remarks         TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_roster_status CHECK (status IN ('PLANNED', 'OPERATED', 'CANCELLED', 'DISRUPTED')),
    CONSTRAINT chk_roster_times CHECK (debrief_time > report_time),

    -- Prevents accidentally assigning the same crew member to the
    -- same role on the same flight twice (a data-entry error, not a
    -- legitimate scenario). Does NOT prevent two different crew
    -- members holding the same role on one flight where that's
    -- legitimate (e.g. two loadmasters) — the constraint is scoped
    -- to (crew_id, flight_id, role_assigned), not (flight_id, role_assigned).
    CONSTRAINT uq_roster_crew_flight_role UNIQUE (crew_id, flight_id, role_assigned)
);

-- Matches the grouping keys core/duty_summary.py actually uses
-- (crew_id, duty_id, report_time, debrief_time) — see
-- DEFAULT_DUTY_KEYS in that file.
CREATE INDEX IF NOT EXISTS idx_roster_duty_group ON roster(crew_id, duty_id, report_time, debrief_time);
CREATE INDEX IF NOT EXISTS idx_roster_crew_date ON roster(crew_id, duty_date);
CREATE INDEX IF NOT EXISTS idx_roster_flight ON roster(flight_id);
