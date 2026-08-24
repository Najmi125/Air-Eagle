"""
services/time_entry.py

HHMM time entry at the UI boundary — parsing what a controller types,
and rendering a stored time back into the same form.

Was page-local in pages/7_Schedule_Templates.py until 2026-08-21, when
Control Room and Flight Log needed the same thing. Two consumers is the
point at which a page-local helper stops being page-local: a second copy
would be a second place for the accepted formats and the error wording
to drift apart, and this is input validation the operator sees the
result of. Same reasoning that put services/display_labels.py here.

Text rather than st.time_input, at the operator's request (2026-08-19):
a dropdown is slow when there are four times to enter, and controllers
already write times as 0905.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional, Tuple


def parse_hhmm(raw: str) -> Tuple[Optional[dt.time], Optional[str]]:
    """'0905'/'09:05' -> dt.time(9, 5). Returns (time, None) on success
    or (None, message) on a bad value.

    The empty string is a real value and NOT an error: it returns
    (None, None). That is what lets a caller tell an untouched field
    from a filled one — a distinction st.time_input could never express,
    because it always yields a value (00:00 by default). Schedule
    Templates depends on it to tell a blank leg row from a
    partially-filled one; Flight Log depends on it because a blank
    actual-time field means "this hasn't happened yet", not "midnight".

    Callers that REQUIRE a time must therefore check for None
    themselves — this function's contract is "is this parseable", not
    "is this present".
    """
    text_value = (raw or "").strip().replace(":", "")
    if not text_value:
        return None, None
    if not (text_value.isdigit() and len(text_value) == 4):
        return None, f"{raw.strip()!r} is not a valid time — use 24-hour HHMM, e.g. 0905"
    hours, minutes = int(text_value[:2]), int(text_value[2:])
    if hours > 23 or minutes > 59:
        return None, f"{raw.strip()!r} is not a valid 24-hour time — hours 00-23, minutes 00-59"
    return dt.time(hours, minutes), None


def format_hhmm(value) -> str:
    """A stored time (or datetime) back to the HHMM a controller typed,
    for pre-filling a field. Empty string for None, so it round-trips
    with parse_hhmm()'s treatment of blank."""
    return value.strftime("%H%M") if value else ""
