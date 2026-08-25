"""
pages/3_Flight_Log.py

Shown as "Flt Schedule" (operator's wording, 2026-08-21). DISPLAY NAME
ONLY: the file, the `flights` table and `flight_service` all keep their
names, and the sidebar label comes from app.py's
`st.Page(title=...)` — renaming the file would touch every
AppTest.from_file() in the tests and check_reachability.py's entry-point
handling, for a label.

The RECORD half of the operation: what happened, searchable, and where
actuals are entered. Creating a flight belongs to
pages/1_Control_Room.py, which is where a controller ACTS. Both pages
carried an identical seven-field add-flight form until 2026-08-21, so
"where do I add a flight?" had two answers; this page's copy is the one
that went.

Permanent log — cancelled flights stay visible, never hidden or
deleted, per the explicit "a permanent log of all flights" requirement.

Deliberately unstyled, matches pages/2_Crew_Data.py — the home page
(home.py) got its own branding pass (2026-08-10/11), this one hasn't.
"""
import datetime as dt
import streamlit as st

from services import flight_service, assignment_service, auth_service
from services.alert_summary import format_alert_lines
from services.display_labels import flight_label
from services.time_entry import parse_hhmm

st.set_page_config(page_title="Flt Schedule", page_icon="📘", layout="wide")
app_user = auth_service.require_login()
st.title("Flt Schedule")

STATUS_OPTIONS = ["All", "PLANNED", "OPERATED", "CANCELLED", "DISRUPTED"]


# ================= DISPLAY =================
status_choice = st.selectbox("Filter by status", STATUS_OPTIONS)
status_filter = None if status_choice == "All" else status_choice
flights_df = flight_service.get_all_flights(status_filter=status_filter)

st.subheader(f"Flights ({len(flights_df)})")
st.dataframe(flights_df, width="stretch")


# ================= UPDATE / CANCEL EXISTING =================
st.subheader("Record actuals, update status, or cancel a flight")
# That promise was false until 2026-08-21: status could only ever
# become CANCELLED, so "update status" described nothing the page
# could do. The disruption controls below make it true.

if flights_df.empty:
    st.info("No flights yet.")
else:
    # Labelled by flight number and date; flight_id stays the
    # identifier and the selection value.
    selected_id = st.selectbox(
        "Select flight", flights_df["flight_id"],
        format_func=lambda fid: flight_label(
            flights_df[flights_df["flight_id"] == fid].iloc[0], include_route=True),
    )
    selected = flight_service.get_flight(selected_id)

    if selected is not None:
        with st.form("edit_flight_form"):
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**{selected['origin']} → {selected['destination']}**")
                st.write(f"Status: {selected['status']}")
                actual_dep_date = st.date_input("Actual departure date", value=None)
                actual_dep_time_raw = st.text_input("Actual departure time (UTC HHMM)")
            with col2:
                actual_arr_date = st.date_input("Actual arrival date", value=None)
                actual_arr_time_raw = st.text_input("Actual arrival time (UTC HHMM)")
                cancel_reason = st.text_input("Cancellation reason (if cancelling)")

            # Aircraft was not editable ANYWHERE before 2026-08-21: it
            # is in flight_service.UPDATABLE_FIELDS and settable when
            # Control Room creates a flight, but no page exposed it
            # afterwards — so a flight recorded without one could never
            # be corrected. Small version of the qualification-renewal
            # gap, and the same fix: expose the field.
            #
            # The default fills an EMPTY value only. `selected["aircraft"]
            # or AIRCRAFT_DEFAULT` keeps whatever is already stored and
            # never overwrites it, and the pre-filled registration is
            # visible in the field before saving, so a controller
            # correcting a genuinely different airframe sees and clears
            # it rather than having it applied behind them.
            edit_aircraft = st.text_input(
                "Aircraft",
                value=selected["aircraft"] or flight_service.AIRCRAFT_DEFAULT or "")

            # Pre-filled from the flight, unlike the actual-time fields
            # above (which are blank because entering one is an event,
            # not a correction). Editing these is how an occupant list
            # gets corrected after the fact — a late AME swap, say.
            edit_occupants_operating = st.text_input(
                "Other occupants — operating",
                value=selected["other_occupants_operating"] or "")
            edit_occupants_non_operating = st.text_input(
                "Other occupants — non-operating",
                value=selected["other_occupants_non_operating"] or "")

            # DISRUPTED is a MANUAL label — the system never applies it.
            # The control offered depends on the flight's current status,
            # rather than a free dropdown of all four states: PLANNED ->
            # OPERATED is automatic (both actuals recorded) and needs no
            # widget, OPERATED and CANCELLED are terminal, and the only
            # transitions a controller makes by hand are into and out of
            # DISRUPTED.
            #
            # The un-disrupt control names its OUTCOME rather than
            # offering "PLANNED" and quietly landing somewhere else: on a
            # flight with both actual times, removing the label yields
            # OPERATED, because the flight flew. Where it lands is
            # decided by recorded fact, not by the caller.
            disruption_reason = None
            if selected["status"] == "PLANNED":
                disruption_reason = st.text_input(
                    "Disruption reason (required to mark DISRUPTED)")
                disrupt_label = "Mark DISRUPTED"
                undisrupt_label = None
            elif selected["status"] == "DISRUPTED":
                disruption_reason = st.text_input(
                    "Reason for clearing the DISRUPTED label (required)")
                disrupt_label = None
                both_actuals = (selected["dep_time_actual"] is not None
                                and selected["arr_time_actual"] is not None)
                undisrupt_label = (
                    "Clear DISRUPTED → OPERATED" if both_actuals
                    else "Clear DISRUPTED → PLANNED")
            else:
                disrupt_label = undisrupt_label = None
                st.caption(
                    f"Status {selected['status']} is final — no manual status "
                    f"changes are available for this flight."
                )

            col_save, col_cancel = st.columns(2)
            with col_save:
                save_submitted = st.form_submit_button("Save changes")
            with col_cancel:
                cancel_submitted = st.form_submit_button("Cancel this flight")

            disrupt_submitted = (
                st.form_submit_button(disrupt_label) if disrupt_label else False)
            undisrupt_submitted = (
                st.form_submit_button(undisrupt_label) if undisrupt_label else False)

            if save_submitted:
                # parse_hhmm() returns (None, None) for blank, which is
                # the normal case here: an empty actual-time field means
                # "this hasn't happened yet", not midnight. Only a
                # malformed value is an error.
                actual_dep_time, dep_error = parse_hhmm(actual_dep_time_raw)
                actual_arr_time, arr_error = parse_hhmm(actual_arr_time_raw)

                dep_actual = (dt.datetime.combine(actual_dep_date, actual_dep_time)
                              if actual_dep_date and actual_dep_time else None)
                arr_actual = (dt.datetime.combine(actual_arr_date, actual_arr_time)
                              if actual_arr_date and actual_arr_time else None)

                # Occupants go through the plain update_flight() path,
                # kept SEPARATE from the actual-times call below rather
                # than folded into it. That call is
                # update_flight_actual_times_and_revalidate(), which
                # re-runs the legality gate on every affected duty —
                # correcting a name on an occupant list must not drag a
                # crew member's duty back through FDP revalidation.
                # Only written when changed, so an untouched edit form
                # produces no write and no audit row.
                occupants_changed = (
                    (edit_occupants_operating or None) != selected["other_occupants_operating"]
                    or (edit_occupants_non_operating or None) != selected["other_occupants_non_operating"]
                    or (edit_aircraft or None) != selected["aircraft"]
                )
                # A malformed time short-circuits everything: showing
                # both "bad time" and "nothing to save" for one submit
                # would be two messages describing one mistake.
                if dep_error or arr_error:
                    st.error(dep_error or arr_error)
                else:
                    if occupants_changed:
                        flight_service.update_flight(selected_id, {
                            "aircraft": edit_aircraft or None,
                            "other_occupants_operating": edit_occupants_operating or None,
                            "other_occupants_non_operating": edit_occupants_non_operating or None,
                        }, app_user=app_user)
                        st.success(f"Updated flight {selected_id} details")

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
                    elif not occupants_changed:
                        # Only when NOTHING was submitted. Saving an
                        # occupant change alone is a complete action, and
                        # must not be told it did nothing.
                        st.warning("Nothing to save — enter actual times, or change aircraft or occupants.")

            if disrupt_submitted:
                try:
                    flight_service.set_flight_disrupted(
                        selected_id, reason=disruption_reason or "", app_user=app_user)
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.success(f"Flight {selected_id} marked DISRUPTED")
                    st.rerun()

            if undisrupt_submitted:
                try:
                    new_status = flight_service.clear_flight_disruption(
                        selected_id, reason=disruption_reason or "", app_user=app_user)
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.success(f"DISRUPTED cleared — flight {selected_id} is now {new_status}")
                    st.rerun()

            if cancel_submitted:
                assignment_service.cancel_flight_and_roster(
                    selected_id, reason=cancel_reason or None, app_user=app_user)
                st.success(f"Cancelled flight {selected_id}")
                st.rerun()
