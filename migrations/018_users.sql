-- ============================================================
-- 018_users.sql
--
-- Authentication for attribution, not access control. Three fixed
-- OCC accounts, full access each, no tiers, no self-registration —
-- see AuthPlan-Ccode.txt for the full design. Before this, every
-- audit_log.app_user was NULL: the audit trail knew WHAT happened
-- and WHEN, never WHO. This table exists to close that, nothing more.
--
-- No role/permission column deliberately — the settled spec is
-- attribution only, not tiered access. Accounts are created by
-- scripts/seed_users.py, never through the app UI (no
-- self-registration surface to build or secure). Only password
-- hashes are ever stored (PBKDF2-HMAC-SHA256, stdlib hashlib, see
-- services/auth_service.py) — the actual password is never written
-- anywhere.
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   BYTEA NOT NULL,
    salt            BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
