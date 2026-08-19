-- ============================================================
-- 019_allow_delete_unused_templates.sql
--
-- Makes the rotation-template immutability guards PRECISE rather than
-- absolute: a template that has produced nothing can be deleted; a
-- template with any history remains as protected as it is today.
--
-- Why this exists (2026-08-19). A widget-key bug on
-- pages/7_Schedule_Templates.py let a second template be created
-- carrying the first one's leg values. Recovering from that required
-- manually disabling these triggers on the live database, which is not
-- an acceptable workflow. The guards themselves were not at fault —
-- they fired exactly as designed, as did the version-overlap
-- constraint. The fault was a guard applied where there was no history
-- to protect: a template created seconds ago, referenced by nothing.
--
-- DELIBERATELY NOT A BYPASS. The condition is encoded INSIDE the
-- trigger, so there is no escape hatch to scope and nothing a caller
-- can switch off. Every alternative considered was a general escape
-- hatch by construction:
--
--   * session_replication_role = replica   disables ALL triggers,
--                                          session-wide
--   * ALTER TABLE ... DISABLE TRIGGER      global, not session-scoped —
--                                          leaves concurrent sessions
--                                          unprotected (this is what
--                                          the manual recovery used)
--   * a session GUC flag the trigger reads a reusable "turn the guard
--                                          off" switch, i.e. exactly
--                                          the hatch to avoid
--
-- Encoding the rule in the trigger means it applies identically to
-- services/rotation_template_service.py and to a hand-written DELETE
-- in psql. The guard gets narrower in scope and stronger in kind.
--
-- SOLE VERSION ONLY, and this is a real limitation rather than an
-- oversight. create_new_version() closes the previous row's
-- effective_until and sets superseded_by, and the UPDATE branch below
-- permits that exactly once, never again. Deleting a v2 would require
-- REOPENING v1's effective_until, which the guard forbids — correctly.
-- So deletion is restricted to a template that is the only version of
-- its rotation_code, which is precisely the "just created it by
-- mistake" recovery case. A bad v2 is superseded by a v3; that is what
-- versioning is for.
--
-- Reference graph audited before writing this (the "unused" test is
-- only as good as the set of things that can reference a template):
--
--     rotation_templates(id) <- rotation_templates.superseded_by (self)
--                            <- rotation_template_legs.template_id
--                            <- rotation_instances.template_id
--     rotation_instances(id) <- rotation_instance_legs.instance_id
--                            <- flights.rotation_instance_id
--     rotation_template_legs <- (nothing)
--
-- So "no rotation_instances" implies no instance legs and no flights.
-- The flights check below is therefore redundant today and included
-- anyway: it is cheap, it states the invariant instead of leaving it
-- to be inferred, and it keeps this correct if a future migration ever
-- points flights at a template directly.
-- tests/test_rotation_template_delete.py asserts that set of foreign
-- keys is still exactly what it was when this was written, so adding a
-- new referencing table fails loudly here rather than silently
-- orphaning a reference.
--
-- Only the function bodies change. The triggers themselves already
-- point at these names (migrations/011), so CREATE OR REPLACE is
-- enough — no DROP/CREATE TRIGGER, nothing detached even briefly.
-- ============================================================

-- Shared predicate: is this template safe to delete? Kept as one
-- function so the templates guard and the legs guard cannot drift
-- apart into disagreeing about what "unused" means.
CREATE OR REPLACE FUNCTION rotation_template_is_deletable(p_template_id INTEGER)
RETURNS BOOLEAN AS $$
DECLARE
    v_rotation_code VARCHAR(50);
BEGIN
    SELECT rotation_code INTO v_rotation_code
    FROM rotation_templates WHERE id = p_template_id;

    IF v_rotation_code IS NULL THEN
        RETURN FALSE;   -- no such template
    END IF;

    -- Has produced instances (DRAFT ones included: a draft is still
    -- something a controller has seen and may be acting on).
    IF EXISTS (SELECT 1 FROM rotation_instances WHERE template_id = p_template_id) THEN
        RETURN FALSE;
    END IF;

    -- Redundant while instances are the only path to a flight; see the
    -- header note on why it is here regardless.
    IF EXISTS (
        SELECT 1 FROM flights f
        JOIN rotation_instances ri ON ri.id = f.rotation_instance_id
        WHERE ri.template_id = p_template_id
    ) THEN
        RETURN FALSE;
    END IF;

    -- Sole version of its rotation_code. Also covers both directions of
    -- the supersession chain, since versions share a rotation_code.
    IF EXISTS (
        SELECT 1 FROM rotation_templates
        WHERE rotation_code = v_rotation_code AND id <> p_template_id
    ) THEN
        RETURN FALSE;
    END IF;

    -- Belt and braces: nothing points at it via superseded_by even if
    -- some future path produced a cross-code reference.
    IF EXISTS (SELECT 1 FROM rotation_templates WHERE superseded_by = p_template_id) THEN
        RETURN FALSE;
    END IF;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;


-- rotation_template_legs: UPDATE stays a hard block, unconditionally.
-- DELETE is permitted only as part of deleting a template that is
-- itself deletable. The UPDATE branch is unchanged from migrations/011
-- and is the thing most at risk of an accidental relaxation in a
-- rewrite like this — tests/test_rotation_template_delete.py asserts
-- it still refuses.
CREATE OR REPLACE FUNCTION reject_rotation_template_legs_mutation() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rotation_template_is_deletable(OLD.template_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'rotation_template_legs rows are immutable — create a new template version instead (template_id=%)',
            OLD.template_id;
    END IF;

    RAISE EXCEPTION 'rotation_template_legs rows are immutable — create a new template version instead (template_id=%)',
        COALESCE(OLD.template_id, NEW.template_id);
END;
$$ LANGUAGE plpgsql;


-- rotation_templates: the UPDATE branch below is byte-for-byte the
-- rule from migrations/011 — closing an open effective_until (and
-- recording superseded_by) exactly once, everything else fixed at
-- insert. Only the DELETE branch changes.
CREATE OR REPLACE FUNCTION guard_rotation_templates_mutation() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rotation_template_is_deletable(OLD.id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'rotation_templates row id=% cannot be deleted: it has produced rotation instances, or is not the only version of rotation_code %. Create a new version instead.',
            OLD.id, OLD.rotation_code;
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
