"""
services/assistant/regulation_reference.py

Curated, plain-English summaries for the ANO-012 sections this
system actually implements and enforces — deliberately NOT a stored
copy of the regulation text itself. A verified ANO-012 text extract
does exist elsewhere (in the wider assistant bundle, with SHA-256
provenance, per 2026-08-01 discussion), but a curated summary was
chosen for THIS report instead, specifically because it can be kept
from silently drifting: tests/test_assistant_reports.py's boundary
tests exercise the ACTUAL validator at each limit named below (one
unit under/at/over) and fail if this file's numbers ever disagree
with core/legality/pcaa_ano012_core.py's real enforced values — a
second, independently-typed copy of the same numbers has no such
guarantee, it only has a comment promising to stay in sync.

Deliberately excludes AE-CREW-QUAL-001 (the crew qualification gate)
and the not-yet-built age-pairing rule (AE-CREW-PAIR-AGE-001) — both
are confirmed Air Eagle OPERATING decisions, not ANO-012 provisions
(see HANDOVER.md's 2026-08-01 entries on both). A "regulation" lookup
answering with either would misattribute an airline policy to the
regulator, which is worse than saying nothing.

Scope: only the sections core/legality/pcaa_ano012_core.py actually
checks today. Anything else falls through to "not available" — the
same honest-failure principle query_parser.py already applies to an
unresolved question, not a guess dressed up as an answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.duty_builder import (
    DOMESTIC_PRE_FLIGHT_MINUTES, DOMESTIC_POST_FLIGHT_MINUTES,
    INTERNATIONAL_PRE_FLIGHT_MINUTES, INTERNATIONAL_POST_FLIGHT_MINUTES,
)


@dataclass(frozen=True)
class RegulationEntry:
    section: str
    title: str
    summary: str


# D7.1.2's numbers are imported, not retyped — core/duty_builder.py
# already names them, so there's nothing to keep in sync here beyond
# what Python's own import mechanism already guarantees. The other
# sections below have no equivalent named constants in
# core/legality/pcaa_ano012_core.py (they're inline literals inside
# _check_cumulative_limits/required_rest_minutes/etc.) — deliberately
# NOT extracting those into named constants as part of this change,
# to avoid touching the one file this project's own rules protect
# most heavily for a reporting-only feature. Cross-checked instead by
# boundary tests against the validator's actual behavior.
REGULATION_REFERENCE: dict[str, RegulationEntry] = {
    "D7.1.2": RegulationEntry(
        section="D7.1.2",
        title="Report/debrief buffers",
        summary=(
            f"Domestic: report {DOMESTIC_PRE_FLIGHT_MINUTES} min before first "
            f"departure, debrief {DOMESTIC_POST_FLIGHT_MINUTES} min after last "
            f"arrival. International: report {INTERNATIONAL_PRE_FLIGHT_MINUTES} "
            f"min before, debrief {INTERNATIONAL_POST_FLIGHT_MINUTES} min after. "
            f"Any international sector in a duty makes the WHOLE duty use the "
            f"international buffer (core/duty_builder.py build_duty())."
        ),
    ),
    "D8.2.1": RegulationEntry(
        section="D8.2.1",
        title="Maximum FDP (Table 2, acclimatized crew)",
        summary=(
            "Maximum flight duty period depends on report time and sector "
            "count. For 1-2 sectors: 13h00 (report 0600-1459), 12h00 (report "
            "1500-1629 or 0500-0559), 11h00 (report 1630-0459, overnight "
            "band). The limit tightens by 30 min per additional sector beyond "
            "2, up to 6 sectors; 7+ sectors requires prior PCAA approval. Full "
            "table: core/legality/pcaa_ano012_core.py's "
            "_table2_acclimatized()."
        ),
    ),
    "D9.1.1": RegulationEntry(
        section="D9.1.1", title="7-day cumulative duty limit",
        summary="Maximum 60 hours of duty in any rolling 7-day period.",
    ),
    "D9.1.2": RegulationEntry(
        section="D9.1.2", title="14-day cumulative duty limit",
        summary="Maximum 110 hours of duty in any rolling 14-day period.",
    ),
    "D9.1.3": RegulationEntry(
        section="D9.1.3", title="28-day cumulative duty limit",
        summary="Maximum 190 hours of duty in any rolling 28-day period.",
    ),
    "D9.2.1": RegulationEntry(
        section="D9.2.1", title="7-day cumulative flight-time limit",
        summary="Maximum 35 hours of flight time in any rolling 7-day period.",
    ),
    "D9.2.2": RegulationEntry(
        section="D9.2.2", title="30-day cumulative flight-time limit",
        summary="Maximum 100 hours of flight time in any rolling 30-day period.",
    ),
    "D9.2.3": RegulationEntry(
        section="D9.2.3", title="12-month cumulative flight-time limit",
        summary="Maximum 1000 hours of flight time in any rolling 365-day period.",
    ),
    "D21.1": RegulationEntry(
        section="D21.1",
        title="Charter/aerial work rest (aircraft above 5700kg)",
        summary=(
            "Required rest after a duty is the GREATER of 12 hours or twice "
            "the preceding duty's FDP. Confirmed as Air Eagle's applicable "
            "rest rule for its cargo-charter classification (2026-07-19)."
        ),
    ),
    "D23.1": RegulationEntry(
        section="D23.1", title="Mandatory days off per month",
        summary=(
            "At least 5 days free of duty in any calendar month with 20 or "
            "more represented days (duty days plus recorded days off)."
        ),
    ),
    "D23.2": RegulationEntry(
        section="D23.2", title="Seventh consecutive duty day",
        summary=(
            "After 6 consecutive duty days, at least 1 day free of duty is "
            "required before a 7th."
        ),
    ),
    "D25": RegulationEntry(
        section="D25", title="In-flight nutrition",
        summary=(
            "A snack opportunity must be arranged for FDP over 4 hours; a "
            "meal opportunity for FDP over 6 hours. Missing meal/snack "
            "provision data on a duty over 6h holds it for manual review "
            "rather than assuming either way."
        ),
    ),
}


def lookup(section: str) -> Optional[RegulationEntry]:
    """Case/whitespace-tolerant lookup by D-section code. Returns None
    for anything not in REGULATION_REFERENCE — including real ANO-012
    sections this system doesn't implement yet (e.g. D17-D19 split
    duty/standby/reserve) — never a guessed answer."""
    return REGULATION_REFERENCE.get(section.strip().upper())
