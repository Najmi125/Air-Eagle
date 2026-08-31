"""`2003z`, not `20:03:35` — the operator's own convention, and the
one place it is decided.

Two things here are load-bearing beyond formatting:

  * SECONDS ARE DROPPED EVERYWHERE. FDP, rest and the D7.1.2 buffers
    are all defined in minutes; rendering `:35` implies a precision the
    data does not have.
  * PLAIN DATES ARE NOT TOUCHED. `25 Aug 2003z` drops the year, which
    is right for a schedule read inside a month-long window and wrong
    for a crew qualification — showing a medical expiring 2026-07-01 as
    "01 Jul" removes the digit that says whether the pilot may fly.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from services.display_labels import format_timestamps, utc_stamp


# ---------------- utc_stamp ----------------

def test_a_timestamp_reads_as_date_plus_zulu_time():
    assert utc_stamp(dt.datetime(2026, 8, 25, 20, 3, 35)) == "25 Aug 2003z"


def test_seconds_are_dropped():
    """Nothing in this system operates at second granularity."""
    a = utc_stamp(dt.datetime(2026, 8, 25, 20, 3, 0))
    b = utc_stamp(dt.datetime(2026, 8, 25, 20, 3, 59))
    assert a == b == "25 Aug 2003z"


def test_the_date_can_be_dropped_where_context_already_gives_it():
    assert utc_stamp(dt.datetime(2026, 8, 25, 20, 3), with_date=False) == "2003z"


def test_midnight_is_rendered_not_treated_as_missing():
    """0000z is a real departure time. A falsy-looking value must not
    disappear the way an empty one does."""
    assert utc_stamp(dt.datetime(2026, 8, 25, 0, 0)) == "25 Aug 0000z"
    assert utc_stamp(dt.time(0, 0)) == "0000z"


def test_missing_values_render_as_empty_not_as_none_or_nat():
    """These land straight in a table cell: an empty cell reads as "not
    recorded", "NaT" reads as a bug."""
    for missing in (None, pd.NaT, float("nan")):
        assert utc_stamp(missing) == "", repr(missing)


def test_a_plain_date_keeps_no_invented_time():
    """A date carries no clock, so rendering 0000z would invent one."""
    assert utc_stamp(dt.date(2026, 8, 25)) == "25 Aug"


def test_a_pandas_timestamp_is_handled_like_a_datetime():
    """Every frame on every page comes back from pandas, so this is the
    common case rather than the exotic one."""
    assert utc_stamp(pd.Timestamp("2026-08-25 20:03:35")) == "25 Aug 2003z"


# ---------------- format_timestamps ----------------

def test_datetime_columns_are_converted():
    df = pd.DataFrame({"report_time": [dt.datetime(2026, 8, 25, 4, 15)],
                       "crew_id": ["CPT-01"]})
    out = format_timestamps(df)
    assert out["report_time"].iloc[0] == "25 Aug 0415z"
    assert out["crew_id"].iloc[0] == "CPT-01"


def test_expiry_dates_keep_their_year():
    """THE exclusion that matters. A medical expiring 2026-07-01 must
    not render as "01 Jul" — the year is what says whether the pilot
    may fly, and a qualification table spans years by definition."""
    df = pd.DataFrame({"crew_id": ["CPT-01"],
                       "medical_expiry": [dt.date(2026, 7, 1)],
                       "licence_expiry": [dt.date(2030, 6, 1)]})
    out = format_timestamps(df)
    assert out["medical_expiry"].iloc[0] == dt.date(2026, 7, 1)
    assert out["licence_expiry"].iloc[0] == dt.date(2030, 6, 1)


def test_dict_built_tables_are_converted():
    """Several pages build their tables from Python dicts, and those
    were exactly the ones showing a raw 2026-08-25 20:03:35.

    pandas coerces a column of pure datetimes to datetime64 even from
    dicts, so this lands on the dtype branch rather than the object one;
    the object branch is what catches dt.time and mixed frames. Asserted
    on the OUTPUT rather than the dtype, because which branch handles it
    is pandas' business and has changed between versions."""
    df = pd.DataFrame([{"when": dt.datetime(2026, 8, 25, 20, 3, 35), "what": "x"}])
    out = format_timestamps(df)
    assert out["when"].iloc[0] == "25 Aug 2003z"
    assert out["what"].iloc[0] == "x"


def test_leg_times_are_converted_because_a_clock_has_no_year_to_lose():
    df = pd.DataFrame([{"dep_time": dt.time(1, 45), "arr_time": dt.time(3, 30)}])
    out = format_timestamps(df)
    assert out["dep_time"].iloc[0] == "0145z"
    assert out["arr_time"].iloc[0] == "0330z"


def test_a_mixed_column_is_left_alone_rather_than_half_converted():
    """Half a column in one format and half in another is worse than
    either format."""
    df = pd.DataFrame([{"v": dt.datetime(2026, 8, 25, 20, 3)}, {"v": "TBC"}])
    assert list(format_timestamps(df)["v"]) == [dt.datetime(2026, 8, 25, 20, 3), "TBC"]


def test_the_caller_s_frame_is_not_mutated():
    """Formatting is the last thing before display, never a change to
    what the page computes from."""
    df = pd.DataFrame({"when": [dt.datetime(2026, 8, 25, 20, 3)]})
    format_timestamps(df)
    assert df["when"].iloc[0] == pd.Timestamp("2026-08-25 20:03")


def test_an_empty_frame_is_returned_unharmed():
    df = pd.DataFrame(columns=["when"])
    assert format_timestamps(df).empty
