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

from services.display_labels import (crew_label, crew_labels, flight_label,
                                      flight_labels)


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
