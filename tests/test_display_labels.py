"""
tests/test_display_labels.py

Pure functions — no database, so these run everywhere. That matters
here specifically: the two cases most likely to be wrong in front of the
operator are the null fallbacks (one real crew member has no
operator_staff_id, every ad-hoc flight has a null flight_no), and both
are exactly the cases a DB-gated test would skip.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from services.display_labels import (CREW_DISPLAY_NAMES, crew_label, crew_labels,
                                      crew_seat_name, flight_label, flight_labels)


def _crew(crew_id="CPT-01", staff="AE92", name="Ali Raza"):
    return pd.Series({"crew_id": crew_id, "operator_staff_id": staff, "name": name})


def _flight(flight_id=4242, flight_no="EPE 786",
            dep=dt.datetime(2026, 8, 20, 19, 0), origin="KHI", destination="LHE"):
    return pd.Series({"flight_id": flight_id, "flight_no": flight_no,
                      "dep_time_planned": dep, "origin": origin,
                      "destination": destination})


# ---------------- crew ----------------

def test_crew_label_leads_with_operator_staff_id():
    assert crew_label(_crew()) == "AE92 (CPT-01) — Ali Raza"


def test_crew_id_stays_visible_because_audit_log_uses_it():
    """The staff ID leads, but crew_id is what appears in audit_log and
    what any support conversation is about — it must not be replaced."""
    assert "CPT-01" in crew_label(_crew())


def test_crew_label_falls_back_when_no_operator_staff_id():
    """One real crew member has none — a live case, not defensive."""
    assert crew_label(_crew(staff=None)) == "CPT-01 — Ali Raza"


def test_crew_label_never_renders_none_as_text():
    for empty in (None, float("nan"), "", "   "):
        label = crew_label(_crew(staff=empty))
        assert "None" not in label and "nan" not in label, label
        assert label == "CPT-01 — Ali Raza"


def test_crew_label_without_name():
    assert crew_label(_crew(), with_name=False) == "AE92 (CPT-01)"


def test_crew_labels_maps_every_row():
    df = pd.DataFrame([_crew(), _crew("FO-01", None, "Sara Khan")])
    assert crew_labels(df) == {
        "CPT-01": "AE92 (CPT-01) — Ali Raza",
        "FO-01": "FO-01 — Sara Khan",
    }


# ---------------- flights ----------------

def test_flight_label_leads_with_flight_number_date_and_time():
    assert flight_label(_flight()) == "EPE 786 · 20 Aug 1900z"


def test_flight_label_includes_the_date_because_numbers_repeat_daily():
    """EPE 786 alone is ambiguous across any selector spanning more than
    one day, which all of them do."""
    a = flight_label(_flight(dep=dt.datetime(2026, 8, 20, 19, 0)))
    b = flight_label(_flight(dep=dt.datetime(2026, 8, 21, 19, 0)))
    assert a != b


def test_flight_label_distinguishes_two_departures_on_the_same_day():
    """A rotation flying the same number twice in a day is ordinary for
    a cargo operator, and date alone cannot tell those two apart. That
    collision would render as one duplicated entry in a selector — which
    looks exactly like a missing flight."""
    morning = flight_label(_flight(dep=dt.datetime(2026, 8, 20, 6, 0)), include_route=True)
    evening = flight_label(_flight(dep=dt.datetime(2026, 8, 20, 19, 0)), include_route=True)
    assert morning != evening
    assert "0600z" in morning and "1900z" in evening


def test_flight_label_falls_back_to_id_for_adhoc_with_no_number():
    """Ad-hoc and charter flights legitimately have no flight_no."""
    assert flight_label(_flight(flight_no=None)) == "#4242 · 20 Aug 1900z"


def test_flight_label_never_renders_none_as_text():
    for empty in (None, float("nan"), "", "   "):
        label = flight_label(_flight(flight_no=empty))
        assert "None" not in label and "nan" not in label, label
        assert label.startswith("#4242")


def test_flight_label_with_route():
    assert flight_label(_flight(), include_route=True) == "EPE 786 · 20 Aug 1900z · KHI→LHE"


def test_flight_labels_maps_every_row():
    df = pd.DataFrame([_flight(), _flight(99, None, dt.datetime(2026, 8, 21, 6, 0), "LHE", "DWC")])
    assert flight_labels(df, include_route=False) == {
        4242: "EPE 786 · 20 Aug 1900z",
        99: "#99 · 21 Aug 0600z",
    }


# ------------------------------------------------------------------
# crew_seat_name — the lookup, and the rule behind it
# ------------------------------------------------------------------
# CREW_DISPLAY_NAMES carries what a controller actually calls each
# pilot, because that is not derivable from the stored name: CPT-03 is
# SYED FAHIM MAHMOOD and is called "Fahim". The mechanical rule stays
# as the FALLBACK for anyone unlisted, which is what lets the table be
# filled in one name at a time instead of having to be complete before
# it is correct.


def _seat(crew_id="CPT-01", role="CPT", name="MUHAMMAD WAQAR"):
    return pd.Series({"crew_id": crew_id, "role": role, "name": name})


def test_the_lookup_wins_over_the_mechanical_rule():
    """The whole point. The rule renders SYED FAHIM MAHMOOD as
    `CPT S Mahmood` — correct, unambiguous, and not what anybody on the
    frequency would recognise."""
    row = _seat("CPT-03", "CPT", "SYED FAHIM MAHMOOD")
    assert crew_seat_name(row) == "CPT Fahim"


def test_an_unlisted_crew_member_still_reads_exactly_as_before():
    """The fallback is not a defensive branch — it is the path almost
    every crew member takes, and a table that made unlisted people
    render worse would not be safe to fill in gradually."""
    assert "CPT-01" not in CREW_DISPLAY_NAMES
    assert crew_seat_name(_seat()) == "CPT M Waqar"


def test_a_title_stored_inside_the_name_is_still_stripped():
    """CPT-06 is "CAPT MUHAMMAD ASAD ALI" in production. The lookup
    supersedes this rule for listed crew; it does not replace it."""
    assert crew_seat_name(_seat("CPT-06", "CPT", "CAPT MUHAMMAD ASAD ALI ")) == "CPT M Ali"


def test_the_grade_comes_from_the_record_not_the_table(monkeypatch):
    """So a promotion changes the label without anyone editing
    display_labels.py — which is the reason the table stores the PERSON
    part only."""
    monkeypatch.setitem(CREW_DISPLAY_NAMES, "FO-09", "Bilal")
    assert crew_seat_name(_seat("FO-09", "FO", "MUHAMMAD BILAL")) == "FO Bilal"
    assert crew_seat_name(_seat("FO-09", "CPT", "MUHAMMAD BILAL")) == "CPT Bilal"


def test_a_listed_crew_member_is_named_even_with_no_stored_name(monkeypatch):
    """Checked BEFORE the missing-name branch: a blank name field is
    exactly where knowing what people call this person is worth most,
    and falling through to `CPT CPT-09` would waste the one source that
    still has the answer."""
    monkeypatch.setitem(CREW_DISPLAY_NAMES, "CPT-09", "Kamran")
    assert crew_seat_name(_seat("CPT-09", "CPT", None)) == "CPT Kamran"


def test_an_unlisted_crew_member_with_no_name_falls_back_to_the_id():
    """Never "None" in front of the operator: an id is more use than a
    blank cell."""
    label = crew_seat_name(_seat("LM-01", "LM", None))
    assert label == "LM LM-01"
    assert "None" not in label


def test_every_entry_is_keyed_by_crew_id_not_by_name():
    """A name-keyed table would map six of Air Eagle's ten pilots onto
    one entry — six are stored as some form of "MUHAMMAD". crew_id is
    the foreign key across roster and audit_log, and it does not
    collide."""
    for key, value in CREW_DISPLAY_NAMES.items():
        assert "-" in key and key == key.upper(), (
            f"{key!r} does not look like a crew_id"
        )
        assert value and value == value.strip(), f"{key}: {value!r}"


def test_the_lookup_does_not_leak_into_the_full_identity_label():
    """crew_label() is the AUDIT-facing label — staff id, crew_id and
    the name as STORED. A support conversation is about the record, so
    the friendly name deliberately stops at the roster table."""
    row = pd.Series({"crew_id": "CPT-03", "operator_staff_id": "AE-97",
                     "name": "SYED FAHIM MAHMOOD"})
    assert crew_label(row) == "AE-97 (CPT-03) — SYED FAHIM MAHMOOD"
