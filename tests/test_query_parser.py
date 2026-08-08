"""
tests/test_query_parser.py

Pure-logic tests for services/assistant/query_parser.py — no DB
fixture needed (same pattern as test_duty_summary.py), since the
parser deliberately takes the crew directory as an argument rather
than querying for it.

Uses the REAL Air Eagle crew names from the operator's data file,
because two of them genuinely collide on "MAHMOOD" — that ambiguity
is a real property of this operator's roster, not a synthetic edge
case, and the parser must surface it rather than guess.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services.assistant.query_parser import (
    parse, parse_dates, parse_crew, parse_role, parse_status, describe_capabilities,
)

CREW = {
    "CPT-01": "MUHAMMAD WAQAR",
    "CPT-02": "MUHAMMAD SALEEM",
    "CPT-03": "SYED FAHIM MAHMOOD",
    "CPT-04": "TAHIR MAHMOOD RAJA",
    "CPT-05": "ADNAN SARWAR KHAN",
    "CPT-06": "MUHAMMAD ASAD ALI",
    "FO-01": "IBTISAM MUZZAFAR",
    "FO-02": "MUHAMMAD WASIM",
    "FO-03": "MUHAMMAD SHAHBAZ",
    "FO-04": "MUHAMMAD SULEMAN AZIZ",
}
TODAY = dt.date(2026, 8, 1)


def _parse(q):
    return parse(q, crew_directory=CREW, today=TODAY)


# ------------------------------------------------------------------
# Template routing
# ------------------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("Show Capt Tahir's flights from 1 to 31 July", "crew_duty_history"),
    ("what did Shahbaz fly last week", "crew_duty_history"),
    ("waqar duties last 7 days", "crew_duty_history"),
    ("show the flight log for July", "flight_records"),
    ("which flights were cancelled in June", "flight_records"),
    ("all KHI-DWC flights last month", "flight_records"),
    ("whose medical expires in the next 30 days", "crew_qualifications"),
    ("show crew document expiry status", "crew_qualifications"),
    ("duty hours per pilot last 28 days", "utilization"),
    ("how many hours has Waqar flown", "utilization"),
    ("crew roster for next 14 days", "roster_coverage"),
    ("flights with missing crew", "roster_coverage"),
    ("everything blocked this week", "audit_compliance"),
    ("show me all overrides last month", "audit_compliance"),
    ("what does the ANO say about minimum rest", "regulation"),
    ("D7.1.2 reporting times", "regulation"),
    ("what is the maximum FDP for 2 sectors", "regulation"),
])
def test_template_routing(question, expected):
    result = _parse(question)
    assert result.resolved, f"{question!r} -> unresolved: {result.reason}"
    assert result.template == expected


# ------------------------------------------------------------------
# Crew resolution — including the real name collision
# ------------------------------------------------------------------

def test_possessive_name_resolves():
    """'Tahir's flights' is one of the most natural phrasings an OCC
    controller uses. An earlier version tokenized "tahir's" as a
    single word and silently failed to identify anyone."""
    assert _parse("Show Capt Tahir's flights in July").crew_ids == ["CPT-04"]


def test_crew_id_resolves_exactly():
    assert _parse("show CPT-01 duties last week").crew_ids == ["CPT-01"]


def test_ambiguous_shared_surname_is_surfaced_not_guessed():
    """Air Eagle really has both SYED FAHIM MAHMOOD (CPT-03) and
    TAHIR MAHMOOD RAJA (CPT-04). 'mahmood' identifies two people;
    picking one would be a guess presented as fact."""
    result = _parse("show mahmood duties last month")
    assert result.resolved is False
    assert result.reason == "ambiguous crew name"
    assert result.ambiguous_crew["mahmood"] == ["CPT-03", "CPT-04"]


def test_ambiguity_resolved_by_a_second_distinguishing_token():
    """'tahir mahmood' is NOT ambiguous — 'tahir' pins it to CPT-04,
    so the shared 'mahmood' token no longer needs to be asked about."""
    result = _parse("show tahir mahmood duties last month")
    assert result.resolved is True
    assert result.crew_ids == ["CPT-04"]


def test_minor_misspelling_still_resolves():
    assert _parse("shahbez duties last week").crew_ids == ["FO-03"]


def test_unknown_name_resolves_no_crew_rather_than_wrong_crew():
    result = _parse("show Smith duties last week")
    assert result.crew_ids == []


def test_common_first_name_is_ambiguous_across_many_crew():
    """Six of ten Air Eagle crew are named MUHAMMAD. That token alone
    must never resolve to one person."""
    result = _parse("show muhammad duties last week")
    assert result.resolved is False
    assert len(result.ambiguous_crew.get("muhammad", [])) > 1


# ------------------------------------------------------------------
# Dates
# ------------------------------------------------------------------

def test_explicit_day_range_with_month():
    d_from, d_to, _ = parse_dates("from 1 to 31 July", TODAY)
    assert d_from == dt.date(2026, 7, 1)
    assert d_to == dt.date(2026, 7, 31)


def test_iso_date_range():
    d_from, d_to, _ = parse_dates("between 2026-06-05 and 2026-06-20", TODAY)
    assert (d_from, d_to) == (dt.date(2026, 6, 5), dt.date(2026, 6, 20))


def test_rolling_window_is_flagged_as_such():
    """'last 28 days' is a ROLLING window, which is how D9.1.3 is
    defined — not a calendar range. Flattening the distinction would
    misrepresent cumulative limits."""
    d_from, d_to, window = parse_dates("last 28 days", TODAY)
    assert window == 28
    assert d_from == TODAY - dt.timedelta(days=28)


def test_bare_month_never_resolves_to_the_future():
    """In August 2026, 'December' means Dec 2025, not a date that
    hasn't happened yet."""
    d_from, d_to, _ = parse_dates("show December", TODAY)
    assert d_from.year == 2025 and d_from.month == 12


def test_last_month_crosses_month_boundary_correctly():
    d_from, d_to, _ = parse_dates("last month", TODAY)
    assert (d_from, d_to) == (dt.date(2026, 7, 1), dt.date(2026, 7, 31))


def test_february_length_is_calendar_correct():
    d_from, d_to, _ = parse_dates("February", dt.date(2024, 6, 1))
    assert d_to == dt.date(2024, 2, 29)  # 2024 is a leap year


def test_next_n_days_looks_forward():
    d_from, d_to, _ = parse_dates("next 14 days", TODAY)
    assert d_from == TODAY
    assert d_to == TODAY + dt.timedelta(days=14)


# ------------------------------------------------------------------
# Roles and routes
# ------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("show captain duties", "CPT"),
    ("first officer roster", "FO"),
    ("loadmaster duties", "LM"),
    ("AME assignments", "ENGR"),
])
def test_role_parsing(text, expected):
    assert parse_role(text) == expected


def test_route_is_extracted():
    result = _parse("all KHI-DWC flights last month")
    assert result.origin == "KHI"
    assert result.destination == "DWC"


# ------------------------------------------------------------------
# status_filter (2026-08-08 fix — parse() previously never populated
# it, even though these are the exact words already scored as
# flight_records keywords; see query_parser.py's own STATUS_KEYWORDS
# comment for why "delayed"/"diverted" both map to DISRUPTED, not two
# separate values that don't exist on flights.status)
# ------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("which flights were cancelled in June", "CANCELLED"),
    ("show delayed flights last week", "DISRUPTED"),
    ("any diverted flights this month", "DISRUPTED"),
])
def test_status_parsing(text, expected):
    assert parse_status(text) == expected


def test_status_parsing_none_when_unmentioned():
    assert parse_status("show all flights last month") is None


def test_parse_populates_status_filter_end_to_end():
    result = _parse("which flights were cancelled in June")
    assert result.template == "flight_records"
    assert result.status_filter == "CANCELLED"


def test_parse_leaves_status_filter_none_when_unmentioned():
    result = _parse("all KHI-DWC flights last month")
    assert result.status_filter is None


# ------------------------------------------------------------------
# Failure behaviour — the safety-relevant part
# ------------------------------------------------------------------

def test_unparseable_question_fails_honestly():
    """The required failure mode: say so, don't guess. A confident
    answer to a misread question is worse than no answer."""
    result = _parse("why did we cancel the 804 on Tuesday")
    assert result.resolved is False
    assert result.template is None or result.reason != "ok"


def test_empty_question_is_rejected():
    assert _parse("").resolved is False
    assert _parse("   ").resolved is False


def test_unresolved_query_is_recorded_for_later_tuning():
    """Unmatched text is retained so keyword lists can grow from real
    logged usage rather than guesswork."""
    result = _parse("blah blah nonsense")
    assert result.resolved is False
    assert result.unmatched_text == "blah blah nonsense"


def test_capabilities_are_listable_for_the_failure_message():
    caps = describe_capabilities()
    assert len(caps) == 7
    assert all(":" in c for c in caps)


def test_no_crew_directory_still_parses_template_and_dates():
    """The parser must degrade gracefully if the crew directory is
    unavailable — template and dates still resolve."""
    result = parse("show the flight log for July", crew_directory={}, today=TODAY)
    assert result.resolved is True
    assert result.template == "flight_records"
    assert result.date_from == dt.date(2026, 7, 1)
