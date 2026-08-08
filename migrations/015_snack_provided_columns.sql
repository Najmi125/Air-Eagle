-- ============================================================
-- 015_snack_provided_columns.sql
--
-- Same gap, same fix, one migration later: core/legality/
-- pcaa_ano012_core.py's D2.18 rule fires a WARNING (not
-- NEEDS_MANUAL_REVIEW) for any duty over 4h whose snack_provided is
-- False, and stays silent when it's None (unknown) — unlike
-- meal_provided (migrations/014), this was previously an
-- accidentally-harmless gap rather than a blocking one, since nothing
-- in this codebase ever populated snack_provided either. Flagged
-- deliberately in HANDOVER.md as an open item rather than inferred
-- from the meal_provided answer (a snack and a meal are legally
-- distinct D2.18/D25 categories) until the operator was actually
-- asked.
--
-- Operator-confirmed (2026-08-08): a snack is provided on every
-- rotation, today. Recorded as DATA, not a code default, for the same
-- reason as meal_provided — hardcoding True anywhere in the rule
-- engine itself would silently defeat D2.18 for a future rotation
-- where a snack genuinely isn't provided. ASSUMPTION — requires
-- airline validation (see HANDOVER.md).
--
-- All three columns BOOLEAN NOT NULL DEFAULT TRUE, threaded through
-- exactly the same layers as meal_provided, for exactly the same
-- reasons — see migrations/014's header for the full reasoning
-- (rotation-level source of truth on rotation_templates, denormalized
-- onto rotation_instance_legs at expansion time, and onto flights at
-- approval time; ad-hoc Control Room flights fall back to the
-- column's own DEFAULT TRUE).
-- ============================================================

ALTER TABLE rotation_templates ADD COLUMN IF NOT EXISTS snack_provided BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE rotation_instance_legs ADD COLUMN IF NOT EXISTS snack_provided BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE flights ADD COLUMN IF NOT EXISTS snack_provided BOOLEAN NOT NULL DEFAULT TRUE;
