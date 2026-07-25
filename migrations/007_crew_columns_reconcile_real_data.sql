-- ============================================================
-- 007_crew_columns_reconcile_real_data.sql
--
-- The operator's returned crew data (AirEagle_Crew_Data_Simple.xlsx,
-- 2026-07-21) used different column names than the original template
-- for two fields, and added one we didn't anticipate:
--   - "LPC/OPC Exp" (our name) -> "SIM Exp" (their name) — same
--     concept (simulator proficiency check), their terminology.
--   - "Line Check Exp" (our name) -> "Route Check Exp" (their name)
--     — same concept, their terminology.
--   - "IR" (Instrument Rating expiry) — genuinely new, not in the
--     original 19-column template at all.
--
-- Renaming rather than adding-and-deprecating: no real production
-- data exists yet (only test data), so there's no migration cost to
-- getting the names right now versus carrying both forever. If real
-- data existed already, the right move would be additive instead —
-- this repo just isn't past that point yet.
-- ============================================================

ALTER TABLE crew RENAME COLUMN lpc_opc_expiry TO sim_expiry;
ALTER TABLE crew RENAME COLUMN line_check_expiry TO route_check_expiry;
ALTER TABLE crew ADD COLUMN IF NOT EXISTS ir_expiry DATE;
