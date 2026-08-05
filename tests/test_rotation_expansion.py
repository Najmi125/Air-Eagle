"""
tests/test_rotation_expansion.py

Pure logic — no database needed. Both real Air Eagle rotations are
expanded here and fed through the ACTUAL core/duty_builder.py engine
(unchanged), re-deriving the exact report/debrief/FDP/rest numbers
recorded in HANDOVER.md's 2026-08-04 entry — not just asserting
expand_template()'s own output looks plausible in isolation.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.rotation_expansion import TemplateLeg, expand_template
from core.duty_builder import build_duty, FlightLeg
from core.legality.pcaa_ano012_core import ANO012CoreValidator, CrewMember, Duty, DutyType

EPE_786_787_LEGS = [
    TemplateLeg(leg_order=1, origin="KHI", destination="LHE",
                dep_time=dt.time(19, 0), arr_time=dt.time(20, 45),
                flight_no="EPE 786", domestic=True),
    TemplateLeg(leg_order=2, origin="LHE", destination="KHI",
                dep_time=dt.time(22, 0), arr_time=dt.time(23, 45),
                flight_no="EPE 787", domestic=True),
]
EPE_786_787_DAYS = [1, 2, 3, 4, 5]  # Mon-Fri

EPE_802_805_LEGS = [
    TemplateLeg(leg_order=1, origin="KHI", destination="LHE",
                dep_time=dt.time(1, 45), arr_time=dt.time(3, 30),
                flight_no="EPE 802", domestic=False),
    TemplateLeg(leg_order=2, origin="LHE", destination="DWC",
                dep_time=dt.time(4, 30), arr_time=dt.time(8, 0),
                flight_no="EPE 804", domestic=False),
    TemplateLeg(leg_order=3, origin="DWC", destination="KHI",
                dep_time=dt.time(9, 0), arr_time=dt.time(11, 0),
                flight_no="EPE 805", domestic=False),
]
EPE_802_805_DAYS = [2, 4, 5, 6]  # Tue/Thu/Fri/Sat


def _to_flight_legs(expanded_legs):
    return [
        FlightLeg(dep_time=leg.dep_time_planned, arr_time=leg.arr_time_planned,
                   origin=leg.origin, destination=leg.destination)
        for leg in expanded_legs
    ]


def test_domestic_rotation_produces_the_exact_hand_verified_numbers():
    """EPE 786/787: report 1815Z, debrief 0000Z, FDP 5.75h, D21 rest
    12h (floor wins) — the numbers recorded in HANDOVER.md's
    2026-08-04 entry, re-derived here through the real engine, not
    asserted by fiat."""
    drafts = expand_template(EPE_786_787_DAYS, EPE_786_787_LEGS,
                              dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert len(drafts) == 1
    assert drafts[0].rotation_date == dt.date(2026, 8, 3)

    duty_result = build_duty(_to_flight_legs(drafts[0].legs), domestic=True)
    assert duty_result.report_time == dt.datetime(2026, 8, 3, 18, 15)
    assert duty_result.debrief_time == dt.datetime(2026, 8, 4, 0, 0)
    assert duty_result.fdp_hours == pytest.approx(5.75)

    validator = ANO012CoreValidator()
    crew = CrewMember(crew_id="CPT-01", name="Test", home_base="KHI")
    prev = Duty(duty_type=DutyType.FDP, start_utc=duty_result.report_time,
                end_utc=duty_result.debrief_time, crew_id="CPT-01", duty_id="D-1")
    nxt = Duty(duty_type=DutyType.FDP, start_utc=duty_result.report_time,
               end_utc=duty_result.debrief_time, crew_id="CPT-01", duty_id="D-2")
    rest_minutes, _ = validator.required_rest_minutes(prev, nxt, crew)
    assert rest_minutes == 12 * 60


def test_international_rotation_produces_the_exact_hand_verified_numbers():
    """EPE 802/804/805: report 0045Z, debrief 1130Z, FDP 10.75h, D21
    rest 21.5h (scales above the 12h floor)."""
    drafts = expand_template(EPE_802_805_DAYS, EPE_802_805_LEGS,
                              dt.date(2026, 8, 4), dt.date(2026, 8, 4))
    assert len(drafts) == 1

    duty_result = build_duty(_to_flight_legs(drafts[0].legs), domestic=False)
    assert duty_result.report_time == dt.datetime(2026, 8, 4, 0, 45)
    assert duty_result.debrief_time == dt.datetime(2026, 8, 4, 11, 30)
    assert duty_result.fdp_hours == pytest.approx(10.75)

    validator = ANO012CoreValidator()
    crew = CrewMember(crew_id="CPT-01", name="Test", home_base="KHI")
    prev = Duty(duty_type=DutyType.FDP, start_utc=duty_result.report_time,
                end_utc=duty_result.debrief_time, crew_id="CPT-01", duty_id="D-1")
    nxt = Duty(duty_type=DutyType.FDP, start_utc=duty_result.report_time,
               end_utc=duty_result.debrief_time, crew_id="CPT-01", duty_id="D-2")
    rest_minutes, _ = validator.required_rest_minutes(prev, nxt, crew)
    assert rest_minutes == int(21.5 * 60)


def test_days_of_week_filters_to_exactly_the_matching_weekdays():
    """A full week must produce exactly 5 Mon-Fri dates for 786/787
    and exactly 4 Tue/Thu/Fri/Sat dates for 802/804/805 — 2026-08-03
    is a Monday."""
    week_start = dt.date(2026, 8, 3)
    week_end = week_start + dt.timedelta(days=6)

    domestic_drafts = expand_template(EPE_786_787_DAYS, EPE_786_787_LEGS, week_start, week_end)
    assert [d.rotation_date.isoformat() for d in domestic_drafts] == [
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
    ]

    intl_drafts = expand_template(EPE_802_805_DAYS, EPE_802_805_LEGS, week_start, week_end)
    assert [d.rotation_date.isoformat() for d in intl_drafts] == [
        "2026-08-04", "2026-08-06", "2026-08-07", "2026-08-08",
    ]


def test_legs_are_ordered_by_leg_order_regardless_of_input_order():
    shuffled = [EPE_802_805_LEGS[2], EPE_802_805_LEGS[0], EPE_802_805_LEGS[1]]
    drafts = expand_template(EPE_802_805_DAYS, shuffled, dt.date(2026, 8, 4), dt.date(2026, 8, 4))
    assert [leg.leg_order for leg in drafts[0].legs] == [1, 2, 3]
    assert [leg.flight_no for leg in drafts[0].legs] == ["EPE 802", "EPE 804", "EPE 805"]


def test_day_offset_rolls_the_date_forward():
    """Neither real rotation needs day_offset > 0, but the schema
    claims to support a leg departing a later calendar day than the
    rotation's nominal start — confirm it actually works."""
    legs = [
        TemplateLeg(leg_order=1, origin="KHI", destination="LHE",
                    dep_time=dt.time(23, 0), arr_time=dt.time(23, 50), day_offset=0, domestic=True),
        TemplateLeg(leg_order=2, origin="LHE", destination="KHI",
                    dep_time=dt.time(1, 0), arr_time=dt.time(2, 0), day_offset=1, domestic=True),
    ]
    drafts = expand_template([1], legs, dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    assert drafts[0].legs[0].dep_time_planned == dt.datetime(2026, 8, 3, 23, 0)
    assert drafts[0].legs[1].dep_time_planned == dt.datetime(2026, 8, 4, 1, 0)


def test_leg_crossing_midnight_within_the_same_day_offset_is_rejected():
    """The deliberate scope limitation: a single leg's arr_time must
    be after its dep_time within the same day_offset. A leg that
    itself crosses midnight must use day_offset on a later leg
    instead, not be silently accepted here."""
    legs = [
        TemplateLeg(leg_order=1, origin="KHI", destination="LHE",
                    dep_time=dt.time(23, 30), arr_time=dt.time(1, 0), day_offset=0, domestic=True),
    ]
    with pytest.raises(ValueError, match="not supported"):
        expand_template([1], legs, dt.date(2026, 8, 3), dt.date(2026, 8, 3))


def test_date_from_after_date_to_raises():
    with pytest.raises(ValueError):
        expand_template(EPE_786_787_DAYS, EPE_786_787_LEGS, dt.date(2026, 8, 10), dt.date(2026, 8, 3))


def test_empty_days_of_week_raises():
    with pytest.raises(ValueError):
        expand_template([], EPE_786_787_LEGS, dt.date(2026, 8, 3), dt.date(2026, 8, 3))


def test_empty_legs_raises():
    with pytest.raises(ValueError):
        expand_template(EPE_786_787_DAYS, [], dt.date(2026, 8, 3), dt.date(2026, 8, 3))


def test_no_matching_weekday_in_window_produces_no_drafts():
    """A window entirely on weekdays the template doesn't operate
    must produce an empty list, not an error."""
    # 2026-08-01/02 is Sat/Sun -- 786/787 only runs Mon-Fri.
    drafts = expand_template(EPE_786_787_DAYS, EPE_786_787_LEGS,
                              dt.date(2026, 8, 1), dt.date(2026, 8, 2))
    assert drafts == []
