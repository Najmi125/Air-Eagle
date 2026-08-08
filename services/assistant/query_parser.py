"""
services/assistant/query_parser.py

Deterministic natural-language query parser for the OCC assistant.
No LLM, no API call, no network. Turns an OCC controller's question
into a ReportRequest (template + parameters) that a report function
can execute against the real schema.

WHY DETERMINISTIC (decided 2026-08-01, after measuring a prototype):
Air Eagle's assistant is a REPORTING tool, not an advisor — it
returns records that already exist, never a recommendation and never
a legality determination (that stays with
core/legality/pcaa_ano012_core.py via the service layer, unchanged).
That narrow job means the parser only has to fill in a form:
which template, which crew, which dates. For ~10 crew, 2 fixed
rotations and a handful of trained users, that's a controlled
vocabulary — not open-ended public input — so keyword and pattern
matching handles it without a model.

What this buys, all of which matter for a safety-critical system
under a regulator: zero API cost, zero latency, no crew PII ever
leaving the operator's infrastructure, works with no network, fully
unit-testable without mocking a provider, and every decision is
inspectable and explainable.

What it costs: it will not generalize to phrasings nobody
anticipated. That's an accepted trade — the failure mode is
"I didn't understand, here's what I can show you", which is safe and
self-correcting, rather than a confident answer to a misread
question. Unresolved queries are logged (see parse().unmatched_text)
so the keyword lists can be grown from real evidence rather than
guesswork — the same discipline used everywhere else in this repo.

DESIGN NOTE — scoring, not first-match:
An earlier prototype used "longest keyword list wins", which
mis-routed 'show the flight log for July' to crew duty history
because both templates contain 'flight'. Templates here carry
weighted keywords plus negative keywords, and the winner needs a
minimum margin over the runner-up. Below that margin the parser
returns AMBIGUOUS with the candidates, rather than guessing.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional


# ------------------------------------------------------------------
# Result types
# ------------------------------------------------------------------

@dataclass
class ReportRequest:
    """A parsed query, ready for a report function to execute."""
    template: Optional[str] = None
    crew_ids: list[str] = field(default_factory=list)
    role: Optional[str] = None
    date_from: Optional[dt.date] = None
    date_to: Optional[dt.date] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    flight_no: Optional[str] = None
    status_filter: Optional[str] = None
    window_days: Optional[int] = None

    # Parse diagnostics — never silently swallowed.
    resolved: bool = False
    reason: str = ""
    candidates: list[str] = field(default_factory=list)
    ambiguous_crew: dict[str, list[str]] = field(default_factory=dict)
    unmatched_text: str = ""


# ------------------------------------------------------------------
# Templates
# ------------------------------------------------------------------
# weight 3 = strongly distinctive phrase, 2 = good signal, 1 = weak.
# "negative" keywords subtract — this is what separates templates
# that share common words like "flight".

TEMPLATES: dict[str, dict] = {
    "crew_duty_history": {
        "positive": {
            "duties": 3, "duty history": 3, "flew": 3, "operated": 2,
            "what did": 2, "roster of": 2, "flights": 1,
            "duty": 2, "schedule for": 2, "fly": 2, "flying": 2,
        },
        "negative": {"flight log": 3, "expire": 3, "expiry": 3, "blocked": 3,
                      "uncovered": 3, "ano": 3, "cumulative": 2},
        "description": "A crew member's duties over a date range",
    },
    "flight_records": {
        "positive": {
            "flight log": 4, "flight records": 4, "cancelled": 4,
            "sectors": 2, "route": 2, "actual times": 3, "operated flights": 3,
            "flights between": 3, "all flights": 2, "delayed": 3, "diverted": 3,
        },
        "negative": {"expire": 3, "blocked": 3, "ano": 3, "hours": 2},
        "description": "Flight records over a date range or route",
    },
    "crew_qualifications": {
        "positive": {
            "expire": 4, "expiry": 4, "expiring": 4, "document": 3,
            "documents": 3, "medical": 3, "licence": 3, "license": 3,
            "validity": 3, "valid": 2, "current": 2, "sim check": 3,
            "route check": 3, "qualification": 3,
        },
        "negative": {"flight log": 2, "blocked": 2, "ano": 3},
        "description": "Crew document expiry status",
    },
    "utilization": {
        "positive": {
            "hours": 3, "utilisation": 4, "utilization": 4, "cumulative": 4,
            "how many hours": 4, "total fdp": 4, "block time": 3,
            "flight hours": 4, "duty hours": 4, "1000": 3, "annual": 2,
        },
        "negative": {"expire": 3, "ano": 3, "blocked": 2},
        "description": "Duty/flight hours per crew over a rolling window",
    },
    "roster_coverage": {
        "positive": {
            "roster": 3, "coverage": 4, "uncovered": 4, "unassigned": 4,
            "missing crew": 4, "no crew": 4, "crew package": 4,
            "who is on": 3, "incomplete": 3, "rotation": 2,
        },
        "negative": {"expire": 3, "ano": 3, "hours": 2},
        "description": "Roster and crew coverage over a date range",
    },
    "audit_compliance": {
        "positive": {
            "blocked": 4, "rejected": 4, "held": 3, "override": 4,
            "audit": 4, "violation": 4, "breach": 3, "illegal": 3,
            "needs review": 3, "what happened": 3,
        },
        "negative": {"ano": 3, "expire": 2},
        "description": "Audit trail: blocked, held, and overridden actions",
    },
    "regulation": {
        "positive": {
            "ano": 4, "regulation": 4, "what does": 2, "say about": 3,
            "rule": 3, "minimum rest": 4, "maximum fdp": 4, "max fdp": 4,
            "limit is": 3, "allowed to": 2, "requirement": 3,
        },
        "negative": {"show me": 1, "list": 1, "export": 2},
        "description": "ANO-012 regulation lookup",
    },
}

# A bare D-section reference (D7.1.2, D21.1) is a near-certain
# regulation query regardless of other wording.
SECTION_RE = re.compile(r"\bD\d+(?:\.\d+){0,4}\b", re.IGNORECASE)

MIN_SCORE = 2          # below this, nothing matched confidently
MIN_MARGIN = 2         # winner must beat runner-up by this much


# ------------------------------------------------------------------
# Date parsing
# ------------------------------------------------------------------

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def _month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last)


def parse_dates(question: str, today: dt.date) -> tuple[Optional[dt.date], Optional[dt.date], Optional[int]]:
    """
    Returns (date_from, date_to, window_days). window_days is set for
    rolling-window phrasings ('last 28 days') because cumulative
    limits are defined as rolling windows, not calendar ranges — the
    distinction matters to D9.1.x/D9.2.x and must not be flattened.
    """
    q = question.lower()

    # ISO range: 2026-07-01 to 2026-07-31
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:to|and|-|—|until|through)\s*(\d{4}-\d{2}-\d{2})", q)
    if m:
        return dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2)), None

    # "1 to 31 July" / "1-31 July"
    m = re.search(r"(\d{1,2})\s*(?:to|-|—|until|through)\s*(\d{1,2})\s+([a-z]+)", q)
    if m and m.group(3) in MONTHS:
        month = MONTHS[m.group(3)]
        year = today.year if month <= today.month else today.year - 1
        return dt.date(year, month, int(m.group(1))), dt.date(year, month, int(m.group(2))), None

    # "last N days" — rolling window
    m = re.search(r"(?:last|past|previous)\s+(\d+)\s+days?", q)
    if m:
        n = int(m.group(1))
        return today - dt.timedelta(days=n), today, n

    # "next N days" — forward looking
    m = re.search(r"next\s+(\d+)\s+days?", q)
    if m:
        n = int(m.group(1))
        return today, today + dt.timedelta(days=n), None

    if "last month" in q:
        first_this = today.replace(day=1)
        prev_end = first_this - dt.timedelta(days=1)
        return _month_bounds(prev_end.year, prev_end.month) + (None,)

    if "this month" in q:
        return _month_bounds(today.year, today.month) + (None,)

    if "last week" in q:
        return today - dt.timedelta(days=7), today, 7

    if "this week" in q:
        return today - dt.timedelta(days=today.weekday()), today, None

    if "yesterday" in q:
        y = today - dt.timedelta(days=1)
        return y, y, None

    if "today" in q or "tonight" in q:
        return today, today, None

    if "this year" in q:
        return dt.date(today.year, 1, 1), today, None

    # Bare month name — assume most recent occurrence, never future.
    for name, month in MONTHS.items():
        if re.search(rf"\b{re.escape(name)}\b", q):
            year = today.year if month <= today.month else today.year - 1
            return _month_bounds(year, month) + (None,)

    return None, None, None


# ------------------------------------------------------------------
# Crew resolution
# ------------------------------------------------------------------

ROLE_KEYWORDS = [
    ("first officer", "FO"), ("loadmaster", "LM"), ("load master", "LM"),
    ("captain", "CPT"), ("capt", "CPT"), ("cpt", "CPT"),
    ("engineer", "ENGR"), ("ame", "ENGR"), ("engr", "ENGR"),
    ("fo", "FO"), ("lm", "LM"),
]

_STOPWORDS = {
    "show", "list", "give", "flights", "flight", "duties", "duty", "from",
    "between", "last", "next", "this", "week", "month", "year", "days",
    "hours", "crew", "roster", "report", "export", "please", "what", "when",
    "which", "their", "there", "about", "expire", "expiry", "medical",
}


def parse_role(question: str) -> Optional[str]:
    q = question.lower()
    for keyword, role in ROLE_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", q):
            return role
    return None


# Mapped to flights.status's actual CHECK-constraint values
# (migrations/002_flights_table.sql: PLANNED/OPERATED/CANCELLED/
# DISRUPTED) — not invented values. "delayed"/"diverted" both mean
# "didn't go as planned but did fly", which is what DISRUPTED already
# covers; there's no separate DELAYED/DIVERTED status to map to.
STATUS_KEYWORDS = [
    ("cancelled", "CANCELLED"),
    ("delayed", "DISRUPTED"),
    ("diverted", "DISRUPTED"),
]


def parse_status(question: str) -> Optional[str]:
    """
    2026-08-08 fix: previously never called from parse(), so
    ReportRequest.status_filter stayed None even when a question named
    a status — "which flights were cancelled in June" correctly routed
    to flight_records but returned ALL flights in June, not just
    cancelled ones, since reports.flight_records() already passes
    status_filter through to flight_service.get_all_flights() and had
    nothing to pass. Same keyword vocabulary already scored in
    TEMPLATES["flight_records"]'s positive keywords (cancelled/delayed/
    diverted) — reused here, not a second list to keep in sync.
    """
    q = question.lower()
    for keyword, status in STATUS_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", q):
            return status
    return None


def parse_crew(question: str, crew_directory: dict[str, str]) -> tuple[list[str], dict[str, list[str]]]:
    """
    Resolve crew references against the real crew directory
    ({crew_id: name}).

    Returns (resolved_ids, ambiguous) where `ambiguous` maps the
    matched token to the several crew_ids it could mean. Ambiguity is
    SURFACED, never silently resolved — Air Eagle's real crew list
    contains both 'SYED FAHIM MAHMOOD' and 'TAHIR MAHMOOD RAJA', so
    'mahmood' genuinely identifies two people. Picking one would be a
    guess presented as fact; asking is correct.
    """
    q = question.lower()
    resolved: set[str] = set()
    ambiguous: dict[str, list[str]] = {}

    # Exact crew_id (CPT-04) — unambiguous, highest priority.
    for crew_id in crew_directory:
        if re.search(rf"\b{re.escape(crew_id.lower())}\b", q):
            resolved.add(crew_id)
    if resolved:
        return sorted(resolved), {}

    # Name-part matching. Build token -> [crew_id] across all names.
    token_map: dict[str, list[str]] = {}
    for crew_id, name in crew_directory.items():
        for part in str(name).split():
            part = part.strip().lower()
            if len(part) < 4 or part in _STOPWORDS:
                continue
            token_map.setdefault(part, []).append(crew_id)

    # Split possessives before matching: "tahir's" must resolve to
    # "tahir". Without this, every "<name>'s flights" phrasing —
    # among the most natural ways an OCC controller asks — silently
    # failed to identify the crew member.
    raw_words = re.findall(r"[a-z][a-z']*", q)
    question_words = []
    for raw in raw_words:
        for piece in raw.split("'"):
            if len(piece) >= 4 and piece not in _STOPWORDS:
                question_words.append(piece)

    for word in question_words:
        if word in token_map:
            owners = token_map[word]
            if len(owners) == 1:
                resolved.add(owners[0])
            else:
                ambiguous[word] = sorted(owners)
            continue
        # Fuzzy: tolerate minor misspelling, but only at high similarity.
        for token, owners in token_map.items():
            if SequenceMatcher(None, word, token).ratio() >= 0.84:
                if len(owners) == 1:
                    resolved.add(owners[0])
                else:
                    ambiguous[token] = sorted(owners)
                break

    # If a more specific token already identified one of an ambiguous
    # token's owners, the ambiguity is satisfied — drop it. Do NOT
    # resolve to the leftover owner: in "tahir mahmood", "tahir"
    # pins CPT-04, and the shared "mahmood" must not then also pull
    # in CPT-03. An earlier version did exactly that, silently adding
    # a crew member the user never referred to.
    for token in list(ambiguous):
        if any(c in resolved for c in ambiguous[token]):
            del ambiguous[token]

    return sorted(resolved), ambiguous


# ------------------------------------------------------------------
# Route / airport
# ------------------------------------------------------------------

AIRPORT_RE = re.compile(r"\b([A-Z]{3})\s*(?:-|—|to|/)\s*([A-Z]{3})\b")


def parse_route(question: str, known_airports: set[str]) -> tuple[Optional[str], Optional[str]]:
    m = AIRPORT_RE.search(question.upper())
    if m:
        origin, destination = m.group(1), m.group(2)
        if origin in known_airports and destination in known_airports:
            return origin, destination
    return None, None


def parse_flight_no(question: str) -> Optional[str]:
    m = re.search(r"\b(?:EPE\s*)?(\d{3})\b", question.upper())
    if m and re.search(r"\bEPE\b|\bflight\s+\d{3}\b", question.upper()):
        return f"EPE {m.group(1)}"
    return None


# ------------------------------------------------------------------
# Template scoring
# ------------------------------------------------------------------

def score_templates(question: str) -> dict[str, int]:
    q = question.lower()
    scores: dict[str, int] = {}
    for name, spec in TEMPLATES.items():
        score = 0
        for phrase, weight in spec["positive"].items():
            if phrase in q:
                score += weight
        for phrase, weight in spec["negative"].items():
            if phrase in q:
                score -= weight
        scores[name] = score

    # A bare ANO section reference is decisive for regulation lookup.
    if SECTION_RE.search(question):
        scores["regulation"] = scores.get("regulation", 0) + 6

    return scores


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def parse(
    question: str,
    *,
    crew_directory: dict[str, str] | None = None,
    known_airports: set[str] | None = None,
    today: dt.date | None = None,
) -> ReportRequest:
    """
    Parse an OCC question into a ReportRequest.

    crew_directory: {crew_id: name} from crew_service.get_all_crew().
    Passed in rather than queried here so this module stays pure and
    testable with no DB — same principle as core/duty_summary.py.
    """
    crew_directory = crew_directory or {}
    known_airports = known_airports or {"KHI", "LHE", "DWC", "ISB", "JED", "DXB"}
    today = today or dt.date.today()

    text = (question or "").strip()
    if not text:
        return ReportRequest(resolved=False, reason="empty question")

    scores = score_templates(text)

    # Parameter-informed tie-breaking. The parameters a question
    # carries are themselves evidence of what it's asking for, and
    # keyword scores alone can't separate templates that legitimately
    # share vocabulary ("flights" belongs to several). Measured:
    # without this, 'all KHI-DWC flights last month' and 'list all
    # flights for FO Ibtisam in June' both mis-routed or tied, purely
    # because 'flights' is common to both templates.
    crew_ids_early, ambiguous_early = parse_crew(text, crew_directory)
    origin_early, destination_early = parse_route(text, known_airports)

    if crew_ids_early or ambiguous_early:
        # Naming a person points at that person's duty history.
        scores["crew_duty_history"] = scores.get("crew_duty_history", 0) + 3
    if origin_early and destination_early:
        # Naming a route points at flight records, not one crew member.
        scores["flight_records"] = scores.get("flight_records", 0) + 3

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_name, best_score = ranked[0]
    runner_score = ranked[1][1] if len(ranked) > 1 else 0

    if best_score < MIN_SCORE:
        return ReportRequest(
            resolved=False,
            reason="no template matched confidently",
            candidates=[n for n, _ in ranked[:3]],
            unmatched_text=text,
        )

    if best_score - runner_score < MIN_MARGIN:
        tied = [n for n, s in ranked if s == best_score]
        return ReportRequest(
            resolved=False,
            reason="ambiguous between templates",
            candidates=tied,
            unmatched_text=text,
        )

    date_from, date_to, window_days = parse_dates(text, today)
    crew_ids, ambiguous_crew = parse_crew(text, crew_directory)
    origin, destination = parse_route(text, known_airports)

    if ambiguous_crew:
        return ReportRequest(
            template=best_name,
            resolved=False,
            reason="ambiguous crew name",
            ambiguous_crew=ambiguous_crew,
            date_from=date_from,
            date_to=date_to,
            unmatched_text=text,
        )

    return ReportRequest(
        template=best_name,
        crew_ids=crew_ids,
        role=parse_role(text),
        date_from=date_from,
        date_to=date_to,
        origin=origin,
        destination=destination,
        flight_no=parse_flight_no(text),
        status_filter=parse_status(text),
        window_days=window_days,
        resolved=True,
        reason="ok",
    )


def describe_capabilities() -> list[str]:
    """Shown to the user when a query can't be resolved — so the
    failure is actionable rather than a dead end."""
    return [f"{name}: {spec['description']}" for name, spec in TEMPLATES.items()]
