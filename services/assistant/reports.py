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

Known, pre-existing gap in query_parser.py (not fixed here — that
file is already merged and tested, and this piece of work is scoped
to the report functions, not the parser): parse() never actually
populates ReportRequest.status_filter, even though "cancelled" /
"delayed" / "diverted" are scoring keywords for flight_records. A
question like "which flights were cancelled in June" correctly routes
to flight_records but currently returns ALL flights in June, not just
cancelled ones, because request.status_filter stays None. Flagged in
HANDOVER.md as a follow-up; flight_records() below does pass
request.status_filter through, so this fixes itself the moment the
parser starts setting it.
"""
from __future__ import annotations

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

# CPT/FO/LM/ENGR are the 4 roles confirmed present on real Air Eagle
# rotations. "Other Crew" from the crew data template is deliberately
# deferred for v1 — see roster_coverage()'s docstring.
REQUIRED_COVERAGE_ROLES = ("CPT", "FO", "LM", "ENGR")

ROSTER_COVERAGE_BASE_HEADERS = (
    "flight_id", "flight_no", "origin", "destination", "dep_time_planned",
)

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
    for field_name in qual_fields:
        value = row[field_name]
        if value is None or pd.isna(value):
            continue
        expiry = value.date() if hasattr(value, "date") else value
        if date_from and expiry < date_from:
            continue
        if date_to and expiry > date_to:
            continue
        return True
    return False


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
    """One row per (non-cancelled) flight in range, with each of the 4
    confirmed-required roles shown as a comma-joined list of assigned
    crew_ids — a role can legitimately hold more than one person (real
    Air Eagle data shows a flight with 2 engineers assigned). A role
    is UNCOVERED only when its list is empty; this does NOT check for
    a specific required count per role, because that count isn't
    confirmed for this operator (see the notes entry below and
    HANDOVER.md). "Other Crew" from the crew data template is
    deferred for v1 — not one of the 4 roles this covers."""
    flights = flight_service.get_all_flights(
        date_from=request.date_from, date_to=request.date_to,
    )
    if not flights.empty:
        flights = flights[flights["status"] != "CANCELLED"]
        if request.origin:
            flights = flights[flights["origin"] == request.origin]
        if request.destination:
            flights = flights[flights["destination"] == request.destination]

    headers = ROSTER_COVERAGE_BASE_HEADERS + REQUIRED_COVERAGE_ROLES + ("uncovered_roles",)
    rows = []
    for _, flight in flights.iterrows():
        roster = assignment_service.get_roster_for_flight(flight["flight_id"])
        row = [
            flight["flight_id"], flight["flight_no"], flight["origin"],
            flight["destination"], flight["dep_time_planned"],
        ]
        uncovered = []
        for role in REQUIRED_COVERAGE_ROLES:
            assigned = sorted(roster.loc[roster["role_assigned"] == role, "crew_id"].tolist())
            row.append(", ".join(assigned))
            if not assigned:
                uncovered.append(role)
        row.append(", ".join(uncovered))
        rows.append(row)

    notes = []
    if rows:
        notes.append(
            "A role column is UNCOVERED only when zero crew are assigned to "
            "it on that flight — this does not check for a specific "
            "required count. The required count per role is unconfirmed for "
            "this operator: real data shows at least one flight with 2 "
            "engineers assigned ('Eng: 2x VAI'), so a role showing more "
            "than one crew_id is expected, not an error. See HANDOVER.md."
        )
    return Dataset.build(
        name="RosterCoverage", title="Roster Coverage",
        headers=headers, rows=rows, notes=notes,
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
