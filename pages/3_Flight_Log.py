"""
pages/3_Flight_Log.py

Collects input, calls services/flight_service.py, displays output.
Permanent log — cancelled flights stay visible, never hidden or
deleted, per the explicit "Flight Log ... a permanent log of all
flights" requirement.

Deliberately unstyled, matches pages/2_Crew_Data.py — the home page
(home.py) got its own branding pass (2026-08-10/11), this one hasn't.
"""
import datetime as dt
import streamlit as st

from services import flight_service, assignment_service, auth_service
from services.alert_summary import format_alert_lines

st.set_page_config(page_title="Flight Log", page_icon="📘", layout="wide")
app_user = auth_service.require_login()
st.title("Flight Log")

STATUS_OPTIONS = ["All", "PLANNED", "OPERATED", "CANCELLED", "DISRUPTED"]


# ================= DISPLAY =================
status_choice = st.selectbox("Filter by status", STATUS_OPTIONS)
status_filter = None if status_choice == "All" else status_choice
flights_df = flight_service.get_all_flights(status_filter=status_filter)

st.subheader(f"Flights ({len(flights_df)})")
st.dataframe(flights_df, width="stretch")


# ================= ADD NEW FLIGHT =================
st.subheader("Add flight")

with st.form("add_flight_form", clear_on_submit=True):
    col1, col2 = st.columns(2)

    with col1:
        flight_no = st.text_input("Flight No (optional — ad-hoc flights may not have one yet)")
        origin = st.text_input("Origin *")
        destination = st.text_input("Destination *")
        aircraft = st.text_input("Aircraft")
        domestic = st.radio("Domestic or international? *", ["Domestic", "International"], horizontal=True)

    with col2:
        dep_date = st.date_input("Departure date *", value=dt.date.today())
        dep_time = st.time_input("Departure time (UTC) *", value=dt.time(5, 0))
        arr_date = st.date_input("Arrival date *", value=dt.date.today())
        arr_time = st.time_input("Arrival time (UTC) *", value=dt.time(7, 0))

    cargo_dg = st.checkbox("Carries dangerous goods (DG)")
    remarks = st.text_area("Remarks")

    submitted = st.form_submit_button("Add flight")

    if submitted:
        try:
            new_id = flight_service.add_flight({
                "flight_no": flight_no or None,
                "origin": origin,
                "destination": destination,
                "aircraft": aircraft or None,
                "dep_time_planned": dt.datetime.combine(dep_date, dep_time),
                "arr_time_planned": dt.datetime.combine(arr_date, arr_time),
                "domestic": domestic == "Domestic",
                "cargo_dg": cargo_dg,
                "remarks": remarks or None,
            }, app_user=app_user)
            st.success(f"Added flight {new_id}")
            st.rerun()
        except ValueError as e:
            st.error(str(e))


# ================= UPDATE / CANCEL EXISTING =================
st.subheader("Record actuals, update status, or cancel a flight")

if flights_df.empty:
    st.info("No flights yet.")
else:
    selected_id = st.selectbox("Select flight", flights_df["flight_id"])
    selected = flight_service.get_flight(selected_id)

    if selected is not None:
        with st.form("edit_flight_form"):
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**{selected['origin']} → {selected['destination']}**")
                st.write(f"Status: {selected['status']}")
                actual_dep_date = st.date_input("Actual departure date", value=None)
                actual_dep_time = st.time_input("Actual departure time (UTC)", value=None)
            with col2:
                actual_arr_date = st.date_input("Actual arrival date", value=None)
                actual_arr_time = st.time_input("Actual arrival time (UTC)", value=None)
                cancel_reason = st.text_input("Cancellation reason (if cancelling)")

            col_save, col_cancel = st.columns(2)
            with col_save:
                save_submitted = st.form_submit_button("Save actual times")
            with col_cancel:
                cancel_submitted = st.form_submit_button("Cancel this flight")

            if save_submitted:
                dep_actual = (dt.datetime.combine(actual_dep_date, actual_dep_time)
                              if actual_dep_date and actual_dep_time else None)
                arr_actual = (dt.datetime.combine(actual_arr_date, actual_arr_time)
                              if actual_arr_date and actual_arr_time else None)
                if dep_actual or arr_actual:
                    outcomes = assignment_service.update_flight_actual_times_and_revalidate(
                        selected_id, dep_time_actual=dep_actual, arr_time_actual=arr_actual,
                        app_user=app_user)
                    st.success(f"Updated flight {selected_id}")
                    for outcome in outcomes:
                        result = outcome["validation_result"]
                        if result.status in ("ILLEGAL", "NEEDS_MANUAL_REVIEW"):
                            st.warning(
                                f"⚠️ {outcome['crew_id']}'s duty {outcome['duty_id']} "
                                f"flagged NEEDS_REVIEW after this delay — {result.status.value}."
                            )
                            for line in format_alert_lines(outcome["alert_summary"]):
                                st.write(line)
                        if outcome["downstream_conflicts"]:
                            st.error(
                                f"⚠️ Swap alert — this delay breaks the legality of "
                                f"{len(outcome['downstream_conflicts'])} already-scheduled "
                                f"future duty(ies) for {outcome['crew_id']}:"
                            )
                            for conflict in outcome["downstream_conflicts"]:
                                st.write(
                                    f"- Duty {conflict.duty_id} ({conflict.role_assigned}, "
                                    f"reports {conflict.report_time}): "
                                    + (f"legal candidates: {', '.join(conflict.candidates)}"
                                       if conflict.candidates else "**no legal candidates found**")
                                )
                    st.rerun()
                else:
                    st.warning("No actual times entered.")

            if cancel_submitted:
                assignment_service.cancel_flight_and_roster(
                    selected_id, reason=cancel_reason or None, app_user=app_user)
                st.success(f"Cancelled flight {selected_id}")
                st.rerun()
