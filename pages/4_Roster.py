"""
pages/4_Roster.py

Assigns crew to flights that already exist in Flight Log — the
scheduled-flight path. Ad-hoc flight creation + assignment together
is Control Room's job (pages/1_Control_Room.py), not this page's.

Collects input, calls services/assignment_service.py exclusively.
No legality logic here — that lives in core/legality/pcaa_ano012_core.py,
called through the service layer.
"""
import streamlit as st

from services import crew_service, flight_service, assignment_service
from services.alert_summary import format_alert_lines

st.set_page_config(page_title="Roster", page_icon="🗓️", layout="wide")
st.title("Roster")

ROLE_OPTIONS = ["CPT", "FO", "LM", "ENGR", "Other"]


# ================= CURRENT ROSTER =================
st.subheader("Current assignments")

flights_df = flight_service.get_all_flights()
if flights_df.empty:
    st.info("No flights in Flight Log yet — add one there first.")
else:
    roster_rows = []
    for _, flight in flights_df.iterrows():
        assigned = assignment_service.get_roster_for_flight(flight["flight_id"])
        for _, a in assigned.iterrows():
            roster_rows.append({
                "flight_id": flight["flight_id"],
                "origin": flight["origin"], "destination": flight["destination"],
                "dep_time_planned": flight["dep_time_planned"],
                "crew_id": a["crew_id"], "role": a["role_assigned"],
                "duty_id": a["duty_id"], "fdp_hours": a["fdp_hours"],
            })
    if roster_rows:
        import pandas as pd
        st.dataframe(pd.DataFrame(roster_rows), width="stretch")
    else:
        st.info("No crew assigned yet.")


# ================= ASSIGN CREW =================
st.subheader("Assign crew to flight(s)")

if flights_df.empty:
    st.stop()

crew_df = crew_service.get_all_crew(active_only=True)
if crew_df.empty:
    st.warning("No active crew on file yet — add crew in Crew Data first.")
    st.stop()

flight_labels = {
    row["flight_id"]: f"#{row['flight_id']} {row['origin']}→{row['destination']} "
                       f"{row['dep_time_planned']} ({'domestic' if row['domestic'] else 'international'})"
    for _, row in flights_df.iterrows()
}

with st.form("assign_crew_form"):
    selected_flight_ids = st.multiselect(
        "Flight(s) forming this duty *",
        options=list(flight_labels.keys()),
        format_func=lambda fid: flight_labels[fid],
    )
    crew_id = st.selectbox(
        "Crew member *",
        options=crew_df["crew_id"],
        format_func=lambda cid: f"{cid} — {crew_df[crew_df['crew_id'] == cid]['name'].values[0]}",
    )
    role_choice = st.selectbox("Role for this assignment *", ROLE_OPTIONS)

    submitted = st.form_submit_button("Check legality and assign")

    if submitted:
        if not selected_flight_ids:
            st.error("Select at least one flight.")
        else:
            # Chronological order, not click order — the caller's
            # decision which flights form a duty must be unambiguous.
            ordered_ids = flights_df[flights_df["flight_id"].isin(selected_flight_ids)] \
                .sort_values("dep_time_planned")["flight_id"].tolist()

            try:
                result = assignment_service.assign_crew_to_duty(crew_id, ordered_ids, role_choice)
            except ValueError as e:
                st.error(str(e))
                result = None

            if result is not None:
                if result.status == "REJECTED":
                    st.error(f"REJECTED — {result.legality_status}")
                    for line in format_alert_lines(result.alert_summary):
                        st.write(line)
                elif result.status == "NEEDS_REVIEW":
                    st.warning(
                        f"HELD FOR MANUAL REVIEW — {result.legality_status}. "
                        f"Nothing was saved. This isn't a known violation, just "
                        f"something the system can't determine automatically — "
                        f"an authorized reviewer needs to make this call."
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
                    st.success(f"ALLOWED — assigned as {role_choice} (duty {result.duty_id})")
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


# ================= UNASSIGN =================
st.subheader("Unassign")

flight_for_unassign = st.selectbox(
    "Flight", options=list(flight_labels.keys()), format_func=lambda fid: flight_labels[fid], key="unassign_flight")
assigned_here = assignment_service.get_roster_for_flight(flight_for_unassign)

if assigned_here.empty:
    st.info("No one assigned to this flight.")
else:
    crew_to_remove = st.selectbox("Crew to unassign", options=assigned_here["crew_id"])
    role_to_remove = assigned_here[assigned_here["crew_id"] == crew_to_remove]["role_assigned"].values[0]
    reason = st.text_input("Reason")
    if st.button("Unassign"):
        assignment_service.remove_assignment(crew_to_remove, flight_for_unassign, role_to_remove, reason=reason or None)
        st.success(f"Unassigned {crew_to_remove}")
        st.rerun()
