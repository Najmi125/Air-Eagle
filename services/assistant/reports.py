"""
services/assistant/reports.py

The seven report functions that execute a resolved
services/assistant/query_parser.ReportRequest against the real
schema, producing a services/reporting.Dataset (exportable as
CSV/XLSX/Markdown via that module). This is the piece that makes the
parser's output actually do something — query_parser.py itself never
touches a database, by design.

Each function reuses an existing canonical read function where one
exists (flight_service.get_all_flights(), assignment_service.
search_roster(), audit_service.get_audit_log(), crew_service.
get_all_crew()) rather than issuing its own parallel SQL, per the
Ownership Table's one-read-path-per-table convention. roster_coverage
is the one template with no existing equivalent read; it's built here
directly, on top of get_all_flights()/get_roster_for_flight().

window_days (rolling, e.g. "last 28 days") vs date_from/date_to
(calendar) are NOT the same thing and are not flattened into one
another anywhere in this file: date_from/date_to always bound which
rows are fetched; window_days, when the parser sets it, additionally
asks utilization() to compute a rolling PEAK inside that fetched
range via core/duty_summary.py's calculate_max_rolling_fdp() — a
different number than the range's own total.

RESOLVED 2026-08-08: query_parser.py's parse() now populates
ReportRequest.status_filter via parse_status() — "cancelled" ->
CANCELLED, "delayed"/"diverted" -> DISRUPTED (flights.status's real
CHECK-constraint values, migrations/002_flights_table.sql). This file
needed no change itself: flight_records() below already passed
request.status_filter through, so the fix was entirely on the parser
side.
"""
from __future__ import annotations

import re

import pandas as pd

from core.duty_summary import (
    calculate_crew_duty_summary,
    calculate_max_rolling_fdp,
    group_roster_rows_into_duties,
)
from services import assignment_service, audit_service, crew_service, flight_service
from services.assistant import query_parser, regulation_reference
from services.reporting import Dataset

CREW_DUTY_HISTORY_HEADERS = (
    "crew_id", "crew_name", "duty_id", "role_assigned", "flight_no",
    "origin", "destination", "dep_time_planned", "arr_time_planned",
    "report_time", "debrief_time", "fdp_hours", "status",
)

FLIGHT_RECORDS_HEADERS = (
    "flight_id", "flight_no", "origin", "destination", "aircraft",
    "dep_time_planned", "arr_time_planned", "dep_time_actual",
    "arr_time_actual", "status", "domestic", "cargo_dg",
)

QUALIFICATION_HEADERS = ("crew_id", "name", "role", "is_active") + tuple(
    assignment_service.QUALIFICATION_EXPIRY_FIELDS.keys()
)

UTILIZATION_BASE_HEADERS = (
    "crew_id", "crew_name", "unique_duties", "total_fdp_hours", "disrupted_duties",
)

# Air Eagle's crew records are CPT/FO only (2026-08-02 operator
# decision — see HANDOVER.md and scripts/import_crew_from_xlsx.py).
# Cockpit coverage is the only thing this report checks structurally;
# everyone else aboard is free text (see ROSTER_COVERAGE_HEADERS).
# These are GRADES, and this report groups by SEAT — the constant is
# used only to tell a cockpit row from an LM/ENGR one when deciding
# whether a missing operating_position is worth reporting.
COCKPIT_COVERAGE_ROLES = ("CPT", "FO")

# Commander / Second Pilot, not CPT / FO. Renamed 2026-08-28 together
# with the grouping underneath them: these columns are SEATS, and a CPT
# occupying the Second Pilot seat is normal under the pair model. While
# they were headed by grade a CPT/CPT pair rendered as two Commanders
# and an UNCOVERED Second Pilot — a flight that was in fact fully
# crewed reading as half-empty.
ROSTER_COVERAGE_HEADERS = (
    "Date", "Flight", "Route", "Commander", "Second Pilot",
    "Other occupants — operating", "Other occupants — non-operating",
    "POB", "Remarks",
)

# Neither covered nor uncovered: a cockpit row that exists but records
# no seat. Impossible for anything written since migration 016, and
# there are ZERO such rows in production (checked 2026-08-28) — but a
# report that silently drops a crew member is worse than one that
# admits it cannot place them, and pre-016 rows would do exactly that.
SEAT_NOT_RECORDED = "Seat not recorded"

# OCC's real-world shorthand for a free-text occupant entry that
# represents more than one person on its own (e.g. "2x AME") —
# confirmed against real data twice now (an earlier "Eng: 2x VAI"
# example, and the operator's own "2x AME" example given alongside
# this reshape). POB needs to count heads, not comma-separated
# segments, so this prefix is recognized without turning the field
# into a structured category list — it's still just free text.
_OCCUPANT_COUNT_PREFIX_RE = re.compile(r"^\s*(\d+)\s*x\b", re.IGNORECASE)


def _count_occupants(free_text) -> int:
    if not free_text:
        return 0
    total = 0
    for segment in str(free_text).split(","):
        segment = segment.strip()
        if not segment:
            continue
        match = _OCCUPANT_COUNT_PREFIX_RE.match(segment)
        total += int(match.group(1)) if match else 1
    return total

AUDIT_COMPLIANCE_HEADERS = (
    "timestamp", "action_type", "affected_crew", "affected_flight",
    "affected_duty", "legality_result", "warning_or_failure_reason", "app_user",
)

REGULATION_HEADERS = ("section", "title", "summary")


def crew_duty_history(request: query_parser.ReportRequest) -> Dataset:
    """Sector-level duty history for a crew member / role over a date
    range. duty_id is kept visible deliberately (see the notes entry
    below) rather than pre-aggregated here, so an operator can see
    exactly which sectors belong to which duty."""
    df = assignment_service.search_roster(
        crew_ids=request.crew_ids or None,
        role=request.role,
        date_from=request.date_from,
        date_to=request.date_to,
    )
    rows = [[row[h] for h in CREW_DUTY_HISTORY_HEADERS] for _, row in df.iterrows()]
    notes = []
    if rows:
        notes.append(
            "fdp_hours, report_time, and debrief_time are duty-level values "
            "that repeat across every sector row sharing the same duty_id "
            "(roster stores one row per sector, not per duty — see "
            "migrations/003_roster_table.sql). Do not sum fdp_hours across "
            "these rows without first grouping by duty_id "
            "(core/duty_summary.py's group_roster_rows_into_duties()) — "
            "this is the single most repeated bug in this platform's history."
        )
    return Dataset.build(
        name="CrewDutyHistory", title="Crew Duty History",
        headers=CREW_DUTY_HISTORY_HEADERS, rows=rows, notes=notes,
    )


def flight_records(request: query_parser.ReportRequest) -> Dataset:
    """Flight log records over a date range, route, or flight number."""
    df = flight_service.get_all_flights(
        status_filter=request.status_filter,
        date_from=request.date_from,
        date_to=request.date_to,
        origin=request.origin,
        destination=request.destination,
        flight_no=request.flight_no,
    )
    rows = [[row[h] for h in FLIGHT_RECORDS_HEADERS] for _, row in df.iterrows()]
    return Dataset.build(
        name="FlightRecords", title="Flight Records",
        headers=FLIGHT_RECORDS_HEADERS, rows=rows,
    )


def _expiry_in_window(row: pd.Series, qual_fields: list[str], date_from, date_to) -> bool:
    """Delegates to assignment_service.expiry_in_window() (2026-08-20).

    Was an independent implementation until the home-page ops banner
    needed the same question answered. One predicate, so the banner and
    this report can never disagree about whether a given crew member's
    documents are expiring — the same reasoning as compute_duty_window()
    routing through build_duty(). qual_fields is retained in the
    signature for call-site compatibility but is now always the full
    tracked set the shared function reads from
    QUALIFICATION_EXPIRY_FIELDS.
    """
    return assignment_service.expiry_in_window(row, date_from, date_to)


def crew_qualifications(request: query_parser.ReportRequest) -> Dataset:
    """Crew document expiry status. Scoped to active crew — an
    inactive crew member's documents don't affect any scheduling
    decision. When a date range is given, narrows to crew with at
    least one of the 8 tracked documents expiring inside it (all 8
    columns are still shown for a matching row, not just the one that
    triggered inclusion)."""
    df = crew_service.get_all_crew(active_only=True)
    if request.crew_ids:
        df = df[df["crew_id"].isin(request.crew_ids)]
    if request.role:
        df = df[df["role"] == request.role]

    qual_fields = list(assignment_service.QUALIFICATION_EXPIRY_FIELDS.keys())
    notes = []
    if request.date_from or request.date_to:
        if not df.empty:
            mask = df.apply(
                lambda row: _expiry_in_window(row, qual_fields, request.date_from, request.date_to),
                axis=1,
            )
            df = df[mask]
        notes.append(
            "Filtered to crew with at least one of the 8 tracked documents "
            "expiring within the requested date range; crew with no expiry "
            "in that window are omitted even though every document column "
            "is still shown for the rows that do match."
        )

    rows = [[row[h] for h in QUALIFICATION_HEADERS] for _, row in df.iterrows()]
    return Dataset.build(
        name="CrewQualifications", title="Crew Qualifications",
        headers=QUALIFICATION_HEADERS, rows=rows, notes=notes,
    )


def utilization(request: query_parser.ReportRequest) -> Dataset:
    """Per-crew duty/FDP totals over a date range, correctly deduped
    to one row per duty before summing — core/duty_summary.py's first
    real caller anywhere in this app. When request.window_days is set
    (a rolling-window phrasing like "last 28 days"), additionally
    surfaces the peak N-day FDP total found inside the fetched range,
    which is a different number than the range's own total."""
    df = assignment_service.search_roster(
        crew_ids=request.crew_ids or None,
        role=request.role,
        date_from=request.date_from,
        date_to=request.date_to,
    )
    headers = UTILIZATION_BASE_HEADERS
    notes = []
    if request.window_days:
        headers = headers + (f"peak_{request.window_days}_day_fdp_hours",)
        notes.append(
            f"peak_{request.window_days}_day_fdp_hours is the highest total "
            f"FDP found in any rolling {request.window_days}-day window "
            "inside the selected range, not the range's own total — see "
            "core/duty_summary.py's calculate_max_rolling_fdp()."
        )

    rows = []
    if not df.empty:
        for crew_id, group in df.groupby("crew_id"):
            crew_name = group["crew_name"].iloc[0]
            summary = calculate_crew_duty_summary(group)
            row = [
                crew_id, crew_name, summary["unique_duties"],
                summary["total_fdp_hours"], summary["disrupted_duties"],
            ]
            if request.window_days:
                duty_df = group_roster_rows_into_duties(group)
                peak = calculate_max_rolling_fdp(duty_df, window_days=request.window_days)
                row.append(round(peak, 2))
            rows.append(row)

    return Dataset.build(
        name="Utilization", title="Crew Utilization",
        headers=headers, rows=rows, notes=notes,
    )


def roster_coverage(request: query_parser.ReportRequest) -> Dataset:
    """One row per (non-cancelled) flight in range. Coverage is CPT/FO
    only — Air Eagle's crew records are CPT/FO only (2026-08-02
    operator decision) — a cockpit column shows UNCOVERED only when
    that seat has no assigned crew_id; occupant columns never trigger
    it, they're informational, not a coverage check.

    "Other occupants — operating"/"— non-operating" are plain OCC-
    entered free text pulled straight from flights.other_occupants_
    operating/other_occupants_non_operating (migrations/010) — this
    function does not classify, parse names, or cross-reference them
    against crew at all; "the system doesn't classify why someone is
    aboard" is the operator's own stated position. POB is the one
    place free text gets interpreted at all, and only to count heads
    (see _count_occupants()'s "Nx ROLE" shorthand)."""
    flights = flight_service.get_all_flights(
        date_from=request.date_from, date_to=request.date_to,
    )
    if not flights.empty:
        flights = flights[flights["status"] != "CANCELLED"]
        if request.origin:
            flights = flights[flights["origin"] == request.origin]
        if request.destination:
            flights = flights[flights["destination"] == request.destination]

    rows = []
    any_uncovered = False
    seatless_notes = []
    for _, flight in flights.iterrows():
        # include_proposed=True (2026-08-04): a seat the roster
        # generator has proposed but OCC hasn't published yet is a
        # real candidate for that seat, not an empty one — showing it
        # as UNCOVERED here would be false, since "does this rotation
        # have a candidate" is exactly what this report answers for a
        # reviewing controller. Unlike get_roster_for_crew()/
        # get_roster_for_flight()'s own default (PROPOSED hidden,
        # "crew sees only published"), this is the one deliberate
        # exception.
        roster = assignment_service.get_roster_for_flight(
            flight["flight_id"], include_proposed=True)

        dep = flight["dep_time_planned"]
        date_value = dep.date() if hasattr(dep, "date") else dep

        # Grouped by operating_position (the SEAT), never by
        # role_assigned (the GRADE). Under the flight-deck pair model a
        # CPT may legitimately occupy the Second Pilot seat, so a
        # grade-based split reported a fully-crewed CPT/CPT flight as
        # two Commanders and an UNCOVERED Second Pilot — which is what
        # this looked like on 2026-08-31 in production. Third instance
        # of that conflation; see search_roster()'s own column comment.
        commander_ids = sorted(
            roster.loc[roster["operating_position"] == "COMMANDER", "crew_id"].tolist())
        second_pilot_ids = sorted(
            roster.loc[roster["operating_position"] == "SECOND_PILOT", "crew_id"].tolist())

        # A cockpit crew member with no seat recorded belongs in neither
        # column, and must not simply vanish from the report — see
        # SEAT_NOT_RECORDED.
        seatless_ids = sorted(roster.loc[
            roster["role_assigned"].isin(COCKPIT_COVERAGE_ROLES)
            & roster["operating_position"].isna(), "crew_id"].tolist())

        commander_cell = ", ".join(commander_ids) if commander_ids else "UNCOVERED"
        second_pilot_cell = ", ".join(second_pilot_ids) if second_pilot_ids else "UNCOVERED"
        if seatless_ids:
            # Named in a note against this flight rather than pushed into
            # a seat cell: putting them in one column would assert a seat
            # the data does not record, and putting them in both would
            # assert two. The point is to say "this person is aboard and
            # I cannot tell you where they are sitting" — which is a
            # different statement from either seat cell.
            seatless_notes.append(
                f"{flight['flight_no']} ({date_value}): {SEAT_NOT_RECORDED} for "
                f"{', '.join(seatless_ids)}")
        if not commander_ids or not second_pilot_ids:
            any_uncovered = True

        operating = flight.get("other_occupants_operating") or ""
        non_operating = flight.get("other_occupants_non_operating") or ""
        # Seatless cockpit crew are aboard whether or not their seat was
        # recorded, so they count toward POB — dropping them here would
        # under-report persons on board, which is the one number on this
        # report that is not about seats at all.
        pob = (len(commander_ids) + len(second_pilot_ids) + len(seatless_ids)
               + _count_occupants(operating) + _count_occupants(non_operating))

        rows.append([
            date_value, flight["flight_no"],
            f'{flight["origin"]}-{flight["destination"]}',
            commander_cell, second_pilot_cell, operating, non_operating, pob,
            flight.get("remarks") or "",
        ])

    notes = []
    if rows:
        notes.append(
            "Commander/Second Pilot are SEATS (operating_position), not "
            "grades: a CPT may legitimately occupy the Second Pilot seat, "
            "so both columns can show a CPT. They read UNCOVERED only when "
            "that seat has no assigned crew_id — the two occupant columns "
            "are OCC-entered "
            "free text (who else is aboard and what they're doing there) "
            "and never trigger UNCOVERED themselves. POB counts the two "
            "cockpit seats plus every name in both occupant columns, "
            "including OCC's own 'Nx ROLE' shorthand for more than one "
            "person in a single entry (e.g. '2x AME' counts as 2)."
        )
    if any_uncovered:
        notes.append("At least one flight in this range has an uncovered cockpit seat.")
    if seatless_notes:
        # Loud, and listed individually. A cockpit row with no seat
        # cannot be produced by any code path since migration 016, so if
        # this ever appears it is a data-integrity finding rather than a
        # display nicety — the crew member IS counted in POB but cannot
        # be placed in a seat.
        notes.append(
            "Cockpit crew aboard whose SEAT is not recorded — counted in POB but "
            "shown in neither seat column, and not treated as covering a seat: "
            + "; ".join(seatless_notes))

    return Dataset.build(
        name="RosterCoverage", title="Roster Coverage",
        headers=ROSTER_COVERAGE_HEADERS, rows=rows, notes=notes,
    )


def audit_compliance(request: query_parser.ReportRequest) -> Dataset:
    """Audit trail of blocked/held actions over a date range — scoped
    to audit_service.COMPLIANCE_ACTION_TYPES, the actual subset this
    codebase writes today (no "override" action_type exists yet
    despite this template's description mentioning overridden
    actions — see audit_service.py's own comment on that gap)."""
    df = audit_service.get_audit_log(
        date_from=request.date_from,
        date_to=request.date_to,
        action_types=audit_service.COMPLIANCE_ACTION_TYPES,
    )
    if request.crew_ids and not df.empty:
        df = df[df["affected_crew"].isin(request.crew_ids)]
    rows = [[row[h] for h in AUDIT_COMPLIANCE_HEADERS] for _, row in df.iterrows()]
    return Dataset.build(
        name="AuditCompliance", title="Audit: Blocked & Held Actions",
        headers=AUDIT_COMPLIANCE_HEADERS, rows=rows,
    )


def regulation(request: query_parser.ReportRequest, question: str) -> Dataset:
    """ANO-012 regulation lookup, derived from services/assistant/
    regulation_reference.py's curated entries.

    question is required here because ReportRequest doesn't carry the
    original question text once resolved (query_parser.py deliberately
    doesn't store it) — the caller of run_report() passes the raw
    question through so the D-section can be re-extracted with the
    same query_parser.SECTION_RE the parser itself used to route here
    in the first place."""
    match = query_parser.SECTION_RE.search(question or "")
    section = match.group(0).upper() if match else None
    entry = regulation_reference.lookup(section) if section else None

    if entry is None:
        note = (
            "No D-section reference (e.g. 'D7.1.2') found in the question."
            if not section else
            f"{section} is not yet covered by this lookup — only sections "
            "this system actually implements and enforces are included "
            "(see services/assistant/regulation_reference.py)."
        )
        return Dataset.build(
            name="Regulation", title="Regulation Lookup",
            headers=REGULATION_HEADERS, rows=[], notes=[note],
        )

    return Dataset.build(
        name="Regulation", title="Regulation Lookup",
        headers=REGULATION_HEADERS,
        rows=[[entry.section, entry.title, entry.summary]],
    )


# Every template except "regulation" (which needs the extra `question`
# argument, handled explicitly in run_report() below) maps directly to
# a report function. The assertion ties this table to
# query_parser.TEMPLATES as the single source of truth for valid
# template names, so a future template added to the parser without a
# matching report function fails at import time, not silently at
# runtime.
REPORT_FUNCTIONS = {
    "crew_duty_history": crew_duty_history,
    "flight_records": flight_records,
    "crew_qualifications": crew_qualifications,
    "utilization": utilization,
    "roster_coverage": roster_coverage,
    "audit_compliance": audit_compliance,
}

assert set(REPORT_FUNCTIONS) | {"regulation"} == set(query_parser.TEMPLATES), (
    "services/assistant/reports.py's REPORT_FUNCTIONS has drifted from "
    "query_parser.TEMPLATES — add or remove a report function to match."
)


def run_report(request: query_parser.ReportRequest, question: str = "") -> Dataset:
    """
    Dispatcher: executes a resolved ReportRequest against the real
    schema. Callers are responsible for checking request.resolved (and
    handling request.candidates/ambiguous_crew/unmatched_text) before
    calling this — turning an unresolved parse into a user-facing
    message is not this function's job, so it raises rather than
    guessing.

    question is only used for the regulation template (see
    regulation()'s docstring for why it can't come from request
    itself); harmless to pass for every other template.
    """
    if not request.resolved or not request.template:
        raise ValueError(
            f"run_report() requires a resolved ReportRequest (reason: {request.reason!r})"
        )
    if request.template == "regulation":
        return regulation(request, question)
    func = REPORT_FUNCTIONS.get(request.template)
    if func is None:
        raise ValueError(f"Unknown report template: {request.template!r}")
    return func(request)
