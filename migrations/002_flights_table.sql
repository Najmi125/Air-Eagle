-- ============================================================
-- 002_flights_table.sql
--
-- Flight operational state. Per the Ownership Table in Project
-- Instructions: this table is the single authority for flight
-- state, written only through flight_service.py's add_flight() /
-- update_flight() / cancel_flight() (Phase 6+) — never by pages
-- writing SQL directly.
--
-- flight_no is nullable: Air Eagle's cargo ops are ad-hoc as well
-- as scheduled (confirmed 2026-07-19), and an ad-hoc charter flight
-- may not have a fixed flight number assigned in advance the way a
-- scheduled route does.
--
-- status uses a CHECK constraint (unlike crew.role) because this is
-- a small, genuinely closed set that the whole platform's logic
-- depends on being exactly these four values — duty_summary.py's
-- disrupted-duty counting already checks status == 'DISRUPTED'
-- literally. An unexpected fifth status value here would silently
-- break that logic rather than loudly rejecting bad data, so the
-- constraint is deliberately strict where crew.role deliberately
-- was not.
-- ============================================================

CREATE TABLE IF NOT EXISTS flights (
    flight_id           SERIAL PRIMARY KEY,
    flight_no           VARCHAR(20),
    origin               VARCHAR(10) NOT NULL,
    destination           VARCHAR(10) NOT NULL,
    aircraft             VARCHAR(20),        -- tail number or type; single/small B737 fleet
    dep_time_planned     TIMESTAMP NOT NULL,
    arr_time_planned     TIMESTAMP NOT NULL,
    dep_time_actual       TIMESTAMP,
    arr_time_actual       TIMESTAMP,
    status               VARCHAR(20) NOT NULL DEFAULT 'PLANNED',
    cargo_dg             BOOLEAN NOT NULL DEFAULT FALSE,
    remarks               TEXT,
    created_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_flights_status CHECK (status IN ('PLANNED', 'OPERATED', 'CANCELLED', 'DISRUPTED')),
    CONSTRAINT chk_flights_times CHECK (arr_time_planned > dep_time_planned)
);

CREATE INDEX IF NOT EXISTS idx_flights_dep_time ON flights(dep_time_planned);
CREATE INDEX IF NOT EXISTS idx_flights_status ON flights(status);
