"""
pages/1_Control_Room.py

Where a controller ACTS: see the operation, and create work.
pages/3_Flight_Log.py is the RECORD — what happened, searchable, and
where actuals are entered. That split is the whole point of the
2026-08-21 restructure: both pages previously carried an identical
seven-field add-flight form, so "where do I add a flight?" had two
answers and neither page had a clear job.

Three sections, in the order a controller uses them:

  1. Operational status — the day on one screen. Uncovered rotation
     seats, document expiries, and today's flights with their cockpit
     seats.
  2. Add a flight — the single place a flight is created.
  3. Crew it, optionally — a flight-deck pair, or crew TBC.

Flight creation and crew assignment happen together, gated as one
action, when a pair is supplied: legality is assessed BEFORE either is
saved, so an illegal proposed assignment saves neither the flight nor
the crew record and leaves no orphan flight behind.

Rebuilt for the flight-deck crew package (2026-08-12): pilots (CPT/FO)
are built + assigned as a Commander + Second Pilot pair, via
assign_pair_to_new_flights(). The single-crew path was REMOVED
2026-08-20 (see the comment below): a flight is crewed by a pair or by
nobody yet, because there is no such thing as a flight operated by one
person. Crew became OPTIONAL the same day — submit without a pair and
only the flight is created ("charter confirmed, crew TBC").

TWO ORDERING RULES hold this page together, both of which look wrong
until you hit the failure they prevent:

  * The crew controls live OUTSIDE the form. Which fields render
    depends on them, and a form batches its own widgets until submit,
    so a control that must be READ before submission cannot be inside
    one.
  * The submit HANDLER runs after section 3, not inside the form. The
    form only declares `submitted`. Section 3 renders below section 2
    for the operator, so the handler must come after both or
    commander_id does not exist yet when it runs — a NameError on the
    first click, from a layout that reads as correct.
"""
import datetime as dt

import pandas as pd
import streamlit as st

from db.db import test_connection
from services import (assignment_service, auth_service, crew_service,
                      flight_service, roster_generator_service)
from services.alert_summary import format_alert_lines
from services.assignment_service import SEAT_ELIGIBLE_GRADES
from services.display_labels import crew_label, flight_label
from services.time_entry import parse_hhmm

st.set_page_config(page_title="Control Room", page_icon="🛫", layout="wide")
app_user = auth_service.require_login()
st.title("Control Room")

# Air Eagle operates a single B737. The registration hasn't been
# supplied yet, so the field stays free text and this stays None —
# set it to the registration and every new flight pre-fills.
# ONE-LINE CHANGE, deliberately isolated here rather than inlined.
AIRCRAFT_DEFAULT = None


# ==================================================================
# 1. Operational status
# ==================================================================
st.header("1. Operational status")

# Gated on db_status, not merely wrapped. try/except catches a FAILING
# query but not a HANGING one, and against an unreachable database
# these sit in connection retries until they time out — which took the
# home page from instant to over three seconds when this banner lived
# there (2026-08-20). Asking a database already known to be unreachable
# was never going to work; not asking is faster and simpler than making
# the failure prettier.
#
# Each block is ALSO wrapped independently, for a connection that is up
# while one query fails on its own (a missing table, a migration not
# yet applied). This page has to render under pressure: a status board
# must never be what stops a controller creating a flight.
db_status = test_connection()

if db_status is not True:
    st.error(f"Database error: {db_status}")
    st.caption("Operational status unavailable while the database is unreachable.")
else:
    today = dt.date.today()
    status_col1, status_col2 = st.columns(2)

    with status_col1:
        try:
            uncovered = roster_generator_service.get_open_uncovered_seats(today, today)
            st.metric("Uncovered rotation seats today", len(uncovered))
            # Precise on purpose. get_open_uncovered_seats() reads the
            # uncovered_seats table, which ONLY the roster generator
            # populates and only for rotation instances. An ad-hoc
            # flight saved with crew TBC never appears there — and this
            # page is what makes those easy to create. The flights board
            # below is where they show up.
            st.caption("Rotation-generated seats only — ad-hoc flights appear in the board below.")
        except Exception as e:
            st.caption(f"Uncovered seats unavailable ({type(e).__name__}).")

    with status_col2:
        try:
            expiry = assignment_service.qualification_expiry_counts()
            # Split, never summed. The legality gate treats
            # expiry <= duty_date as already expired, so a document
            # expiring TODAY is blocking assignments right now rather
            # than "due soon". One combined number would hide that
            # behind a word that implies there is still time.
            st.metric("Crew with expired documents", expiry["expired"])
            st.metric(
                f"Crew with documents expiring in {expiry['horizon_days']} days",
                expiry["expiring"],
            )
        except Exception as e:
            st.caption(f"Document expiry unavailable ({type(e).__name__}).")

    st.subheader("Today's flights")
    try:
        todays_flights = flight_service.get_all_flights(date_from=today, date_to=today)
        if todays_flights.empty:
            st.info("No flights scheduled today.")
        else:
            # TWO queries for the whole day, not one per flight. The
            # seat comes from roster.operating_position, NOT from
            # role_assigned: under the pair model a CPT can legitimately
            # occupy the Second Pilot seat, so reading coverage off the
            # grade reports the wrong seat filled. That grade-versus-
            # position conflation is exactly what the flight-deck crew
            # package existed to remove. operating_position was added to
            # search_roster() for this (2026-08-21).
            todays_roster = assignment_service.search_roster(
                date_from=today, date_to=today, include_proposed=True)

            seats_by_flight = {}
            if not todays_roster.empty:
                for _, r in todays_roster.iterrows():
                    seats_by_flight.setdefault(r["flight_id"], {})[
                        r["operating_position"]] = r["crew_id"]

            rows = []
            for _, f in todays_flights.iterrows():
                seats = seats_by_flight.get(f["flight_id"], {})
                rows.append({
                    "Flight": flight_label(f),
                    "Route": f"{f['origin']}→{f['destination']}",
                    "Dep (UTC)": f["dep_time_planned"],
                    "Arr (UTC)": f["arr_time_planned"],
                    "Status": f["status"],
                    "Commander": seats.get("COMMANDER") or "UNCOVERED",
                    "Second Pilot": seats.get("SECOND_PILOT") or "UNCOVERED",
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    except Exception as e:
        st.caption(f"Today's flights unavailable ({type(e).__name__}).")


st.divider()

crew_df = crew_service.get_all_crew(active_only=True)
if crew_df.empty:
    # Not fatal: a flight can be recorded with crew TBC, which is
    # exactly the state a brand-new deployment is in.
    st.info("No active crew on file yet — flights can still be saved with crew TBC.")


# ==================================================================
# 2. Add a flight
# ==================================================================
st.header("2. Add a flight")

with st.form("control_room_form"):
    col1, col2 = st.columns(2)

    with col1:
        # Flight No added 2026-08-21 — Flight Log's form had it and this
        # one didn't, and a charter may well carry one. Optional,
        # because an ad-hoc flight legitimately may not have a number
        # yet; flight_service treats it as nullable for that reason.
        flight_no = st.text_input("Flight No (optional — ad-hoc flights may not have one yet)")
        origin = st.text_input("Origin *")
        destination = st.text_input("Destination *")
        aircraft = st.text_input("Aircraft", value=AIRCRAFT_DEFAULT or "")
        domestic = st.radio("Domestic or international? *", ["Domestic", "International"], horizontal=True)
        cargo_dg = st.checkbox("Carries dangerous goods (DG)")

    with col2:
        # HHMM text, not st.time_input: a dropdown is slow and
        # controllers already write times as 0905. Safe inside this
        # st.form — the Enter-to-commit gap that bit Schedule Templates
        # needs a bare st.button, because a form commits its fields
        # together on submit.
        dep_date = st.date_input("Departure date *", value=dt.date.today())
        dep_time_raw = st.text_input("Departure time (UTC HHMM) *", value="0500")
        arr_date = st.date_input("Arrival date *", value=dt.date.today())
        arr_time_raw = st.text_input("Arrival time (UTC HHMM) *", value="0700")

    # Free text by design — the system does not classify why someone is
    # aboard. Every real Air Eagle flight carries an LM and AMEs, and
    # until 2026-08-20 there was nowhere to record them.
    other_occupants_operating = st.text_input(
        "Other occupants — operating (aboard and working, e.g. 'Abdulghani (LM), 2x AME')")
    other_occupants_non_operating = st.text_input(
        "Other occupants — non-operating (aboard, not operating — reason in Remarks)")

    remarks = st.text_area("Remarks")

    # Declares `submitted` only. The handler is below section 3 — see
    # the module docstring's ordering rules.
    submitted = st.form_submit_button("Check legality and save")


# ==================================================================
# 3. Crew it, optionally
# ==================================================================
st.header("3. Crew it")

# The "Crew type" radio and its single-crew branch were REMOVED
# (2026-08-20). There is no such thing as a flight operated by one crew
# member. That path predated the flight-deck pair model, from when LM
# and AME were crew records assigned individually.
#
# It was already close to unreachable: Crew Data creates only CPT/FO/
# Other, the branch offered roles LM/ENGR/Other, and
# assign_crew_to_new_flights() rejects CPT/FO outright AND requires
# role_assigned to match the person's registered role. The one live
# combination was an "Other" crew member assigned role "Other" — which
# created a flight with one non-pilot aboard and no flight deck at all.
#
# assign_crew_to_new_flights() itself is deliberately KEPT — see its
# docstring.

commander_pool = crew_df[crew_df["role"].isin(SEAT_ELIGIBLE_GRADES["COMMANDER"])] \
    if not crew_df.empty else crew_df
second_pilot_pool_full = crew_df[crew_df["role"].isin(SEAT_ELIGIBLE_GRADES["SECOND_PILOT"])] \
    if not crew_df.empty else crew_df
pair_possible = not (crew_df.empty or commander_pool.empty or second_pilot_pool_full.empty)

assign_pair = st.checkbox(
    "Assign a flight-deck pair now", value=True,
    key="control_room_assign_pair",
    help="Untick for 'charter confirmed, crew TBC' — the flight is saved with no crew, "
         "and both cockpit seats show as UNCOVERED until someone is assigned.",
)

if assign_pair and not pair_possible:
    # Never silently save an uncrewed flight when a pair was asked for.
    st.warning(
        "No eligible flight-deck pair on file — need at least one active CPT (Commander) "
        "and one active CPT/FO (Second Pilot). Untick 'Assign a flight-deck pair now' to "
        "save the flight with no crew."
    )

commander_id = None
second_pilot_id = None
if assign_pair and pair_possible:
    commander_id = st.selectbox(
        "Commander (must be CPT) *", options=commander_pool["crew_id"],
        format_func=lambda cid: crew_label(commander_pool[commander_pool["crew_id"] == cid].iloc[0]),
        key="control_room_commander",
    )
    second_pilot_options = second_pilot_pool_full[second_pilot_pool_full["crew_id"] != commander_id]
    second_pilot_id = st.selectbox(
        "Second Pilot (CPT or FO) *", options=second_pilot_options["crew_id"],
        format_func=lambda cid: crew_label(second_pilot_options[second_pilot_options["crew_id"] == cid].iloc[0]),
        key="control_room_second_pilot",
    ) if not second_pilot_options.empty else None
    if second_pilot_id is None:
        # No st.stop(): the flight-only path is still available, so a
        # missing Second Pilot must not take the whole page out.
        st.error(
            "No eligible Second Pilot left once the Commander is excluded — "
            "untick 'Assign a flight-deck pair now' to save the flight without crew."
        )


# ==================================================================
# Submit handler — deliberately below section 3
# ==================================================================
if submitted:
    dep_time, dep_error = parse_hhmm(dep_time_raw)
    arr_time, arr_error = parse_hhmm(arr_time_raw)

    if not origin or not destination:
        st.error("Origin and destination are required.")
    elif dep_error or arr_error:
        st.error(dep_error or arr_error)
    elif dep_time is None or arr_time is None:
        # parse_hhmm() returns (None, None) for blank — a real value it
        # reports rather than an error, because a blank field means
        # something different elsewhere. Here both times are required.
        st.error("Departure and arrival times are required — 24-hour HHMM, e.g. 0905.")
    else:
        flights_data = [{
            "flight_no": flight_no or None,
            "origin": origin,
            "destination": destination,
            "aircraft": aircraft or None,
            "dep_time_planned": dt.datetime.combine(dep_date, dep_time),
            "arr_time_planned": dt.datetime.combine(arr_date, arr_time),
            "domestic": domestic == "Domestic",
            "cargo_dg": cargo_dg,
            "other_occupants_operating": other_occupants_operating or None,
            "other_occupants_non_operating": other_occupants_non_operating or None,
            "remarks": remarks or None,
        }]

        have_pair = bool(commander_id and second_pilot_id)

        if assign_pair and not have_pair:
            # Asked for a pair, none available. Refuse rather than
            # quietly downgrading to an uncrewed flight — the
            # controller's stated intent was to crew it.
            st.error(
                "No flight-deck pair available to assign. Nothing was saved. "
                "Untick 'Assign a flight-deck pair now' to save the flight with no crew."
            )
        elif not have_pair:
            # Flight only. Deliberately a DIFFERENT call rather than a
            # mode inside assign_pair_to_new_flights(): that function's
            # guarantee is that an illegal pair leaves no orphan flight,
            # and the cleanest way to preserve it is to not touch it.
            # There is no crew here, so there is no legality gate to run
            # and nothing to be atomic with.
            new_id = flight_service.add_flight(flights_data[0], app_user=app_user)
            st.success(
                f"Flight {new_id} saved with no crew assigned (crew TBC). "
                f"Both cockpit seats will show as UNCOVERED until assigned in Roster."
            )
            st.rerun()
        else:
            try:
                result, flight_ids = assignment_service.assign_pair_to_new_flights(
                    commander_id, second_pilot_id, flights_data, app_user=app_user)
            except ValueError as e:
                st.error(str(e))
                result = None

            if result is not None:
                if result.status == "REJECTED":
                    st.error(
                        f"REJECTED — Commander: {result.validation.commander_status}, "
                        f"Second Pilot: {result.validation.second_pilot_status}. "
                        f"Nothing was saved — no flight, no assignment."
                    )
                    for line in format_alert_lines(result.validation.commander_alert_summary):
                        st.write(f"Commander — {line}")
                    for line in format_alert_lines(result.validation.second_pilot_alert_summary):
                        st.write(f"Second Pilot — {line}")
                    for alert in result.validation.pair_alerts:
                        st.write(f"Pair — {alert.message}")
                elif result.status == "NEEDS_REVIEW":
                    st.warning(
                        "HELD FOR MANUAL REVIEW. Nothing was saved — no flight, no "
                        "assignment. This isn't a known violation, just something the "
                        "system can't determine automatically — an authorized "
                        "reviewer needs to make this call."
                    )
                    for line in format_alert_lines(result.validation.commander_alert_summary):
                        st.write(f"Commander — {line}")
                    for line in format_alert_lines(result.validation.second_pilot_alert_summary):
                        st.write(f"Second Pilot — {line}")
                    for alert in result.validation.pair_alerts:
                        st.write(f"Pair — {alert.message}")
                else:
                    st.success(
                        f"ALLOWED — flight {flight_ids[0]} saved, {commander_id} assigned "
                        f"as Commander, {second_pilot_id} assigned as Second Pilot"
                    )
                    for alert in result.validation.pair_alerts:
                        st.info(f"Pair — {alert.message}")

                    for label, conflicts in (
                        ("Commander", result.commander_downstream_conflicts),
                        ("Second Pilot", result.second_pilot_downstream_conflicts),
                    ):
                        if conflicts:
                            st.error(
                                f"⚠️ Swap alert — this assignment breaks the legality of "
                                f"{len(conflicts)} already-scheduled future duty(ies) for the {label}:"
                            )
                            for conflict in conflicts:
                                st.write(
                                    f"- Duty {conflict.duty_id} ({conflict.role_assigned}, "
                                    f"reports {conflict.report_time}): "
                                    + (f"legal candidates: {', '.join(conflict.candidates)}"
                                       if conflict.candidates else "**no legal candidates found**")
                                )
                    st.rerun()
