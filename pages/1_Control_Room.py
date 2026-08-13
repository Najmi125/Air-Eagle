"""
pages/1_Control_Room.py

Ad-hoc / unscheduled / charter flight entry. Builds the flight AND
assigns crew in one gated action — legality is assessed BEFORE
either is saved. An illegal proposed assignment saves neither the
flight nor the crew record; there's no orphan, uncrewed flight left
behind.

This is the ad-hoc counterpart to pages/4_Roster.py, which assigns
crew to flights that already exist (the scheduled-flight path). Both
ultimately write through the same roster table and the same
legality engine — Control Room's only difference is that flight
creation and crew assignment happen together, gated as one action.

Rebuilt for the flight-deck crew package (2026-08-12): pilots (CPT/FO)
are built + assigned as a Commander + Second Pilot pair, via
assign_pair_to_new_flights() — a real cockpit needs a real pair
regardless of ad-hoc vs. scheduled, so this page is no longer exempt
from the pair model. LM/ENGR/Other keep the original single-crew form,
via assign_crew_to_new_flights() (unaffected — operating_position
stays None for them).

"Crew type" lives OUTSIDE the form (not inside it): which fields the
form shows depends on it, and a form batches its own widgets until
submit — same reasoning already established elsewhere in this
codebase for any live-updating control that needs to be read before
submission.
"""
import datetime as dt
import streamlit as st

from services import crew_service, assignment_service
from services.assignment_service import SEAT_ELIGIBLE_GRADES
from services.alert_summary import format_alert_lines

st.set_page_config(page_title="Control Room", page_icon="🛫", layout="wide")
st.title("Control Room — Ad-hoc / Charter")

OTHER_ROLE_OPTIONS = ["LM", "ENGR", "Other"]

crew_df = crew_service.get_all_crew(active_only=True)
if crew_df.empty:
    st.warning("No active crew on file yet — add crew in Crew Data first.")
    st.stop()

st.subheader("Build flight and assign crew")

crew_type = st.radio(
    "Crew type *", ["Flight-deck pair (Commander + Second Pilot)", "Single crew (LM / ENGR / Other)"],
    key="control_room_crew_type",
)
is_pair = crew_type.startswith("Flight-deck")

commander_pool = crew_df[crew_df["role"].isin(SEAT_ELIGIBLE_GRADES["COMMANDER"])]
second_pilot_pool_full = crew_df[crew_df["role"].isin(SEAT_ELIGIBLE_GRADES["SECOND_PILOT"])]

if is_pair and (commander_pool.empty or second_pilot_pool_full.empty):
    st.warning("Need at least one active CPT (Commander) and one active CPT/FO (Second Pilot) on file.")
    st.stop()

if is_pair:
    commander_id = st.selectbox(
        "Commander (must be CPT) *", options=commander_pool["crew_id"],
        format_func=lambda cid: f"{cid} — {commander_pool[commander_pool['crew_id'] == cid]['name'].values[0]}",
        key="control_room_commander",
    )
    second_pilot_options = second_pilot_pool_full[second_pilot_pool_full["crew_id"] != commander_id]
    second_pilot_id = st.selectbox(
        "Second Pilot (CPT or FO) *", options=second_pilot_options["crew_id"],
        format_func=lambda cid: f"{cid} — {second_pilot_options[second_pilot_options['crew_id'] == cid]['name'].values[0]}",
        key="control_room_second_pilot",
    ) if not second_pilot_options.empty else None
    if second_pilot_id is None:
        st.error("No eligible Second Pilot left once the Commander is excluded.")
        st.stop()

with st.form("control_room_form"):
    col1, col2 = st.columns(2)

    with col1:
        origin = st.text_input("Origin *")
        destination = st.text_input("Destination *")
        aircraft = st.text_input("Aircraft")
        domestic = st.radio("Domestic or international? *", ["Domestic", "International"], horizontal=True)
        cargo_dg = st.checkbox("Carries dangerous goods (DG)")

    with col2:
        dep_date = st.date_input("Departure date *", value=dt.date.today())
        dep_time = st.time_input("Departure time (UTC) *", value=dt.time(5, 0))
        arr_date = st.date_input("Arrival date *", value=dt.date.today())
        arr_time = st.time_input("Arrival time (UTC) *", value=dt.time(7, 0))
        if not is_pair:
            crew_id = st.selectbox(
                "Crew member *", options=crew_df["crew_id"],
                format_func=lambda cid: f"{cid} — {crew_df[crew_df['crew_id'] == cid]['name'].values[0]}",
            )
            role_choice = st.selectbox("Role *", OTHER_ROLE_OPTIONS)

    remarks = st.text_area("Remarks")

    submitted = st.form_submit_button("Check legality and save")

    if submitted:
        if not origin or not destination:
            st.error("Origin and destination are required.")
        else:
            flights_data = [{
                "origin": origin,
                "destination": destination,
                "aircraft": aircraft or None,
                "dep_time_planned": dt.datetime.combine(dep_date, dep_time),
                "arr_time_planned": dt.datetime.combine(arr_date, arr_time),
                "domestic": domestic == "Domestic",
                "cargo_dg": cargo_dg,
                "remarks": remarks or None,
            }]

            if is_pair:
                try:
                    result, flight_ids = assignment_service.assign_pair_to_new_flights(
                        commander_id, second_pilot_id, flights_data)
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
            else:
                try:
                    result, flight_ids = assignment_service.assign_crew_to_new_flights(
                        crew_id, flights_data, role_choice)
                except ValueError as e:
                    st.error(str(e))
                    result = None

                if result is not None:
                    if result.status == "REJECTED":
                        st.error(
                            f"REJECTED — {result.legality_status}. "
                            f"Nothing was saved — no flight, no assignment."
                        )
                        for line in format_alert_lines(result.alert_summary):
                            st.write(line)
                    elif result.status == "NEEDS_REVIEW":
                        st.warning(
                            f"HELD FOR MANUAL REVIEW — {result.legality_status}. "
                            f"Nothing was saved — no flight, no assignment. This "
                            f"isn't a known violation, just something the system "
                            f"can't determine automatically — an authorized "
                            f"reviewer needs to make this call."
                        )
                        for line in format_alert_lines(result.alert_summary):
                            st.write(line)
                        if result.computed_report_time:
                            st.write(
                                f"Computed duty (not saved): report "
                                f"{result.computed_report_time}, debrief "
                                f"{result.computed_debrief_time}, "
                                f"FDP {result.computed_fdp_hours}h"
                            )
                    else:
                        st.success(f"ALLOWED — flight {flight_ids[0]} saved, {crew_id} assigned as {role_choice}")
                        if result.legality_status != "LEGAL":
                            st.warning(f"Status: {result.legality_status}")
                            for line in format_alert_lines(result.alert_summary):
                                st.write(line)

                        if result.downstream_conflicts:
                            st.error(
                                f"⚠️ Swap alert — this assignment breaks the legality of "
                                f"{len(result.downstream_conflicts)} already-scheduled future "
                                f"duty(ies) for {crew_id}:"
                            )
                            for conflict in result.downstream_conflicts:
                                st.write(
                                    f"- Duty {conflict.duty_id} ({conflict.role_assigned}, "
                                    f"reports {conflict.report_time}): "
                                    + (f"legal candidates: {', '.join(conflict.candidates)}"
                                       if conflict.candidates else "**no legal candidates found**")
                                )
                        st.rerun()
