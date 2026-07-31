"""
pages/1_Control_Room.py

Ad-hoc / unscheduled / charter flight entry. Builds the flight AND
assigns crew in one gated action — legality is assessed BEFORE
either is saved (services/assignment_service.py's
assign_crew_to_new_flights()). An illegal proposed assignment saves
neither the flight nor the crew record; there's no orphan,
uncrewed flight left behind.

This is the ad-hoc counterpart to pages/4_Roster.py, which assigns
crew to flights that already exist (the scheduled-flight path). Both
ultimately write through the same roster table and the same
legality engine — Control Room's only difference is that flight
creation and crew assignment happen together, gated as one action.
"""
import datetime as dt
import streamlit as st

from services import crew_service, assignment_service

st.set_page_config(page_title="Control Room", page_icon="🛫", layout="wide")
st.title("Control Room — Ad-hoc / Charter")

ROLE_OPTIONS = ["CPT", "FO", "LM", "ENGR", "Other"]

crew_df = crew_service.get_all_crew(active_only=True)
if crew_df.empty:
    st.warning("No active crew on file yet — add crew in Crew Data first.")
    st.stop()

st.subheader("Build flight and assign crew")

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
        crew_id = st.selectbox(
            "Crew member *", options=crew_df["crew_id"],
            format_func=lambda cid: f"{cid} — {crew_df[crew_df['crew_id'] == cid]['name'].values[0]}",
        )
        role_choice = st.selectbox("Role *", ROLE_OPTIONS)

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
                    for alert in result.alerts:
                        st.write(f"- **{alert.rule_code}**: {alert.message}")
                elif result.status == "NEEDS_REVIEW":
                    st.warning(
                        f"HELD FOR MANUAL REVIEW — {result.legality_status}. "
                        f"Nothing was saved — no flight, no assignment. This "
                        f"isn't a known violation, just something the system "
                        f"can't determine automatically — an authorized "
                        f"reviewer needs to make this call."
                    )
                    for alert in result.alerts:
                        st.write(f"- **{alert.rule_code}**: {alert.message}")
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
                        for alert in result.alerts:
                            st.write(f"- **{alert.rule_code}**: {alert.message}")

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
