"""Correcting a crew field re-checks the duties already written.

Reported from live use (2026-09-05): an OCC member set CPT-03's SIM
expiry to a past date and saved it. CPT-03 stayed on already-written
PLANNED rosters and nothing was flagged. Future assignments were
correctly refused the whole time — the gate works — but a pilot whose
document lapses today stayed on next week's published roster silently,
and the only remedy was somebody noticing.

The scope is the GENERAL case, not the reported slice: an OCC member
enters a crew field wrong and corrects it days later, and it is not
only expiry dates. A wrong `base` corrected a week later is the same
operator scenario as a wrong expiry.

DB-FREE. The orchestration, the tier router and the never-clear rule
are all decidable without Postgres, which matters because every
DB-gated test of this path skips wherever Postgres is absent — the
condition under which the original defect survived review.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

import services.assignment_service as asg
import services.crew_service as crew_service

D = dt.datetime
_FAR = dt.date(2099, 1, 1)
_LAPSED = dt.date(2026, 8, 1)
_NOW = D(2026, 9, 5, 6, 0)


def _crew(crew_id="CPT-03", role="CPT", is_active=True, **expiries):
    row = {
        "crew_id": crew_id, "name": "SYED FAHIM MAHMOOD", "role": role,
        "base": "KHI", "is_active": is_active,
        "date_of_birth": dt.date(1980, 1, 1), "operator_staff_id": "AE-143",
        "license_expiry": _FAR, "medical_expiry": _FAR, "sim_expiry": _FAR,
        "route_check_expiry": _FAR, "ir_expiry": _FAR, "sep_expiry": _FAR,
        "crm_expiry": _FAR, "dg_expiry": _FAR,
    }
    row.update(expiries)
    return pd.Series(row)


def _duty(duty_id="DUTY-1", day=12, position="COMMANDER"):
    return {
        "duty_id": duty_id,
        "report_time": D(2026, 9, day, 18, 15),
        "debrief_time": D(2026, 9, day, 23, 59),
        "role_assigned": "CPT",
        "operating_position": position,
        "flight_ids": [day * 10, day * 10 + 1],
    }


class _Result:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class _Conn:
    def __init__(self, log, rowcount=1):
        self._log = log
        self._rowcount = rowcount

    def execute(self, statement, params=None):
        self._log.append((str(statement), params or {}))
        return _Result(self._rowcount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Engine:
    """Records every statement, so a test can assert what was written
    rather than what a helper claims it wrote."""

    def __init__(self, rowcount=1):
        self.statements = []
        self._rowcount = rowcount

    def begin(self):
        return _Conn(self.statements, self._rowcount)

    def connect(self):
        return _Conn(self.statements, self._rowcount)


@pytest.fixture
def revalidation(monkeypatch):
    """Runs the REAL revalidate_crew_duties() over fake leaves."""
    def run(crew_row, duties, changed_fields, rowcount=1):
        engine = _Engine(rowcount=rowcount)
        audits = []
        monkeypatch.setattr(asg, "get_engine", lambda: engine)
        monkeypatch.setattr(asg, "log_audit", lambda **k: audits.append(k))
        monkeypatch.setattr(crew_service, "get_crew", lambda cid: crew_row)
        monkeypatch.setattr(asg, "_read_reval_rows", lambda *a, **k: list(duties))
        result = asg.revalidate_crew_duties(
            crew_row["crew_id"], changed_fields, app_user="occ1")
        return result, engine, audits
    return run


# ------------------------------------------------------------------
# The tier router
# ------------------------------------------------------------------

def test_a_field_legality_never_reads_costs_nothing():
    """Correcting a phone number must not walk the roster. This is the
    only thing standing between "every crew edit revalidates" and a
    page that takes seconds to save a typo fix."""
    for field in ("phone", "email", "name", "nationality", "remarks",
                  "operator_staff_id", "license_no"):
        assert asg._tier_for({field}) == 0, field


def test_each_tier_is_recognised():
    assert asg._tier_for({"sim_expiry"}) == 1
    assert asg._tier_for({"is_active"}) == 1
    assert asg._tier_for({"date_of_birth"}) == 2
    assert asg._tier_for({"base"}) == 3
    assert asg._tier_for({"role"}) == 3


def test_a_mixed_edit_takes_the_highest_tier():
    """The higher check subsumes the lower — tier 3 runs the whole
    gate, which already includes tier 1's qualification check. Taking
    the lowest would silently skip the expensive half of the edit."""
    assert asg._tier_for({"phone", "sim_expiry", "base"}) == 3
    assert asg._tier_for({"phone", "date_of_birth"}) == 2
    assert asg._tier_for({"phone", "medical_expiry"}) == 1


def test_is_active_is_a_legality_field_despite_not_being_updatable():
    """is_active is absent from crew_service.UPDATABLE_FIELDS — it is
    written only by deactivate_crew(). If the tier sets were derived
    from UPDATABLE_FIELDS, deactivating a pilot would revalidate
    nothing, which is the most consequential change of all."""
    assert "is_active" in asg.LEGALITY_FIELDS_TIER1
    assert "is_active" not in crew_service.UPDATABLE_FIELDS


# ------------------------------------------------------------------
# Scope
# ------------------------------------------------------------------

def test_nothing_is_read_at_all_when_no_legality_field_changed(revalidation):
    result, engine, audits = revalidation(_crew(), [_duty()], {"phone"})

    assert result["tier"] == 0
    assert result["checked"] == 0
    assert not engine.statements, "a phone-number edit queried the roster"
    assert not audits


def test_the_scoping_query_states_all_four_exclusions():
    """The exclusions are operator decisions and they live in SQL, so a
    DB-free test can only check that they are still STATED — it cannot
    execute them. Narrow, and honest about being narrow: it catches the
    filter being deleted, not the filter being wrong. The DB-gated
    tests cover the behaviour."""
    engine = _Engine()
    captured = {}

    def capture(_engine, query, params):
        captured["query"] = query
        captured["params"] = params
        return []

    original = asg._read_reval_rows
    asg._read_reval_rows = capture
    try:
        asg._future_planned_duties(engine, "CPT-03", now=_NOW)
    finally:
        asg._read_reval_rows = original

    query = " ".join(captured["query"].split())
    assert "status = 'PLANNED'" in query, (
        "OPERATED, DISRUPTED, NEEDS_REVIEW and PROPOSED are all excluded "
        "by this one clause"
    )
    assert "report_time > :now" in query, (
        "future is measured on report_time, not duty_date — a duty that "
        "reported this morning has already started"
    )
    assert captured["params"]["now"] == _NOW


# ------------------------------------------------------------------
# Flagging
# ------------------------------------------------------------------

def test_a_lapsed_expiry_flags_the_future_duty(revalidation):
    """THE reported scenario, end to end through the real function."""
    result, engine, audits = revalidation(
        _crew(sim_expiry=_LAPSED), [_duty()], {"sim_expiry"})

    assert result["tier"] == 1
    assert result["checked"] == 1
    assert len(result["flagged"]) == 1
    assert result["flagged"][0]["duty_id"] == "DUTY-1"
    assert any("SIM" in r for r in result["flagged"][0]["reasons"])

    flagged_sql = [s for s, _ in engine.statements if "NEEDS_REVIEW" in s]
    assert len(flagged_sql) == 1
    assert "status = 'PLANNED'" in flagged_sql[0], (
        "the crew path must only touch future PLANNED rows"
    )
    assert [a["action_type"] for a in audits] == [
        "DUTY_FLAGGED_FOR_REVIEW_AFTER_CREW_CHANGE"]


def test_a_crew_member_still_current_flags_nothing(revalidation):
    """Not vacuous: the same path over valid documents writes nothing."""
    result, engine, audits = revalidation(_crew(), [_duty()], {"sim_expiry"})

    assert result["checked"] == 1
    assert result["flagged"] == []
    assert not [s for s, _ in engine.statements if "NEEDS_REVIEW" in s]
    assert not audits


def test_every_affected_duty_is_flagged_with_no_cap(revalidation):
    """No throttling, by operator decision: if a correction affects
    thirty duties then thirty duties are genuinely affected, and
    quietly showing five would be exactly the reassuring-but-false
    record this project keeps stamping out."""
    duties = [_duty(f"DUTY-{i}", day=i) for i in range(6, 30)]
    result, engine, audits = revalidation(
        _crew(medical_expiry=_LAPSED), duties, {"medical_expiry"})

    assert result["checked"] == len(duties)
    assert len(result["flagged"]) == len(duties)
    assert len(audits) == len(duties), "every flag must leave its own audit row"


def test_deactivation_flags_duties_the_same_way(revalidation):
    """The door that is easy to miss — is_active is not in
    UPDATABLE_FIELDS, so a fix wired only into update_crew() would
    leave this bypassing revalidation entirely."""
    result, _engine, _audits = revalidation(
        _crew(is_active=False), [_duty()], {"is_active"})

    assert len(result["flagged"]) == 1
    assert any("active" in r.lower() for r in result["flagged"][0]["reasons"])


def test_a_duty_that_stopped_being_planned_mid_sweep_is_not_reported(revalidation):
    """The UPDATE matched no rows, so the duty is no longer PLANNED —
    something else changed it between the scoping query and the write.
    Reporting it as flagged would be a lie."""
    result, _engine, audits = revalidation(
        _crew(sim_expiry=_LAPSED), [_duty()], {"sim_expiry"}, rowcount=0)

    assert result["flagged"] == []
    assert not audits


# ------------------------------------------------------------------
# Never auto-clear
# ------------------------------------------------------------------

def test_a_correction_in_the_safe_direction_clears_nothing(revalidation):
    """A renewal does not un-flag. The flag records that nobody has
    LOOKED since the data changed, not that the data is bad — so only
    a human retires it."""
    result, engine, _audits = revalidation(
        _crew(sim_expiry=_FAR), [_duty()], {"sim_expiry"})

    assert result["flagged"] == []
    assert not [s for s, _ in engine.statements
                if "PLANNED" in s and "UPDATE" in s.upper()
                and "NEEDS_REVIEW" not in s.split("WHERE")[0]], (
        "revalidation must never move a row OUT of NEEDS_REVIEW"
    )


def test_clearing_a_flag_requires_a_reason(monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(asg, "get_engine", lambda: engine)
    monkeypatch.setattr(asg, "log_audit", lambda **k: None)

    for empty in ("", "   ", None):
        with pytest.raises(ValueError, match="reason is required"):
            asg.clear_duty_review_flag("DUTY-1", empty)
    assert not engine.statements


def test_clearing_a_flag_only_moves_needs_review_back_to_planned(monkeypatch):
    engine = _Engine()
    audits = []
    monkeypatch.setattr(asg, "get_engine", lambda: engine)
    monkeypatch.setattr(asg, "log_audit", lambda **k: audits.append(k))

    changed = asg.clear_duty_review_flag("DUTY-1", "Checked with the CP", app_user="occ1")

    assert changed == 1
    sql = " ".join(engine.statements[0][0].split())
    assert "SET status = 'PLANNED'" in sql
    assert "status = 'NEEDS_REVIEW'" in sql, (
        "clearing must not touch a row that was never flagged"
    )
    assert audits[0]["action_type"] == "DUTY_REVIEW_FLAG_CLEARED"
    assert "Checked with the CP" in audits[0]["warning_or_failure_reason"]


def test_clearing_nothing_writes_no_audit(monkeypatch):
    """A clear that matched no rows did not happen, and must not claim
    to have."""
    engine = _Engine(rowcount=0)
    audits = []
    monkeypatch.setattr(asg, "get_engine", lambda: engine)
    monkeypatch.setattr(asg, "log_audit", lambda **k: audits.append(k))

    assert asg.clear_duty_review_flag("DUTY-1", "already fine") == 0
    assert not audits


# ------------------------------------------------------------------
# The two doors
# ------------------------------------------------------------------

def test_update_crew_revalidates_only_what_actually_changed(monkeypatch):
    """pages/2_Crew_Data.py submits every field on every save, changed
    or not. If "changed" meant "what the caller passed", opening the
    form and saving without touching anything would revalidate the
    whole roster."""
    existing = _crew()
    seen = {}

    monkeypatch.setattr(crew_service, "get_crew", lambda cid: existing)
    monkeypatch.setattr(crew_service, "get_engine", lambda: _Engine())
    monkeypatch.setattr(crew_service, "log_audit", lambda **k: None)
    monkeypatch.setattr(
        crew_service, "_revalidate_after_crew_change",
        lambda crew_id, changed, app_user=None: seen.update(changed=changed) or {})

    # Same values back again — a no-op save.
    crew_service.update_crew("CPT-03", {
        "phone": existing["phone"] if "phone" in existing else None,
        "base": "KHI",
        "sim_expiry": _FAR,
    })
    assert seen["changed"] == set(), f"a no-op save revalidated: {seen['changed']}"

    # Now genuinely change one field.
    seen.clear()
    crew_service.update_crew("CPT-03", {"base": "KHI", "sim_expiry": _LAPSED})
    assert seen["changed"] == {"sim_expiry"}


def test_both_crew_writers_go_through_the_one_door():
    """Structural, so it cannot rot: if a third writer of a
    legality-relevant crew field appears, this is the check that says
    it must call the revalidation too."""
    source = Path("services/crew_service.py").read_text(encoding="utf-8")
    writers = [line for line in source.splitlines()
               if "UPDATE crew SET" in line]
    assert len(writers) == 2, (
        f"crew_service has {len(writers)} writers of the crew table; every "
        f"one that can touch a legality-relevant field must call "
        f"_revalidate_after_crew_change(). Found: {writers}"
    )
    assert source.count("_revalidate_after_crew_change(") == 3, (
        "expected one definition plus one call from each of update_crew "
        "and deactivate_crew"
    )
