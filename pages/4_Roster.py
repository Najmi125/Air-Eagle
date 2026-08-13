"""
pages/4_Roster.py

Assigns crew to flights that already exist in Flight Log — the
scheduled-flight path. Ad-hoc flight creation + assignment together
is Control Room's job (pages/1_Control_Room.py), not this page's.

Collects input, calls services/assignment_service.py exclusively.
No legality logic here — that lives in core/legality/pcaa_ano012_core.py,
called through the service layer.

Rebuilt for the flight-deck crew package (2026-08-12): pilots (CPT/FO)
are assigned as a Commander + Second Pilot pair in one form, via
assign_pair_to_duty() — both validated and committed together, never
one alone. LM/ENGR/Other keep the original single-role form, via
assign_crew_to_duty() (unaffected by the pair model — operating_position
stays None for them). Unassign is duty-scoped
(remove_assignment_from_duty()) — cancels every sector of that
person's own duty in one call, not one flight leg at a time.
"""
import streamlit as st

from services import crew_service, flight_service, assignment_service
from services.assignment_service import SEAT_ELIGIBLE_GRADES
from services.alert_summary import format_alert_lines

st.set_page_config(page_title="Roster", page_icon="🗓️", layout="wide")
st.title("Roster")

OTHER_ROLE_OPTIONS = ["LM", "ENGR", "Other"]


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
                "operating_position": a["operating_position"] or "",
                "duty_id": a["duty_id"], "fdp_hours": a["fdp_hours"],
            })
    if roster_rows:
        import pandas as pd
        st.dataframe(pd.DataFrame(roster_rows), width="stretch")
    else:
        st.info("No crew assigned yet.")


# ================= ASSIGN FLIGHT-DECK PAIR =================
st.subheader("Assign flight-deck pair (Commander + Second Pilot)")

if flights_df.empty:
    st.stop()

commander_pool = crew_service.get_all_crew(active_only=True)
commander_pool = commander_pool[commander_pool["role"].isin(SEAT_ELIGIBLE_GRADES["COMMANDER"])]
second_pilot_pool_full = crew_service.get_all_crew(active_only=True)
second_pilot_pool_full = second_pilot_pool_full[second_pilot_pool_full["role"].isin(SEAT_ELIGIBLE_GRADES["SECOND_PILOT"])]

flight_labels = {
    row["flight_id"]: f"#{row['flight_id']} {row['origin']}→{row['destination']} "
                       f"{row['dep_time_planned']} ({'domestic' if row['domestic'] else 'international'})"
    for _, row in flights_df.iterrows()
}

if commander_pool.empty or second_pilot_pool_full.empty:
    st.warning("Need at least one active CPT (Commander) and one active CPT/FO (Second Pilot) on file.")
else:
    with st.form("assign_pair_form"):
        pair_flight_ids = st.multiselect(
            "Flight(s) forming this duty *",
            options=list(flight_labels.keys()),
            format_func=lambda fid: flight_labels[fid],
            key="pair_flights",
        )
        col1, col2 = st.columns(2)
        commander_id = col1.selectbox(
            "Commander (must be CPT) *", options=commander_pool["crew_id"],
            format_func=lambda cid: f"{cid} — {commander_pool[commander_pool['crew_id'] == cid]['name'].values[0]}",
        )
        second_pilot_options = second_pilot_pool_full[second_pilot_pool_full["crew_id"] != commander_id]
        second_pilot_id = col2.selectbox(
            "Second Pilot (CPT or FO) *", options=second_pilot_options["crew_id"],
            format_func=lambda cid: f"{cid} — {second_pilot_options[second_pilot_options['crew_id'] == cid]['name'].values[0]}",
        ) if not second_pilot_options.empty else None

        pair_submitted = st.form_submit_button("Check legality and assign pair")

        if pair_submitted:
            if not pair_flight_ids:
                st.error("Select at least one flight.")
            elif second_pilot_id is None:
                st.error("No eligible Second Pilot left once the Commander is excluded.")
            else:
                ordered_ids = flights_df[flights_df["flight_id"].isin(pair_flight_ids)] \
                    .sort_values("dep_time_planned")["flight_id"].tolist()

                try:
                    result = assignment_service.assign_pair_to_duty(commander_id, second_pilot_id, ordered_ids)
                except ValueError as e:
                    st.error(str(e))
                    result = None

                if result is not None:
                    if result.status == "REJECTED":
                        st.error(f"REJECTED — Commander: {result.validation.commander_status}, "
                                  f"Second Pilot: {result.validation.second_pilot_status}")
                        for line in format_alert_lines(result.validation.commander_alert_summary):
                            st.write(f"Commander — {line}")
                        for line in format_alert_lines(result.validation.second_pilot_alert_summary):
                            st.write(f"Second Pilot — {line}")
                        for alert in result.validation.pair_alerts:
                            st.write(f"Pair — {alert.message}")
                    elif result.status == "NEEDS_REVIEW":
                        st.warning(
                            "HELD FOR MANUAL REVIEW. Nothing was saved for either seat. This isn't a "
                            "known violation, just something the system can't determine automatically "
                            "— an authorized reviewer needs to make this call."
                        )
                        for line in format_alert_lines(result.validation.commander_alert_summary):
                            st.write(f"Commander — {line}")
                        for line in format_alert_lines(result.validation.second_pilot_alert_summary):
                            st.write(f"Second Pilot — {line}")
                        for alert in result.validation.pair_alerts:
                            st.write(f"Pair — {alert.message}")
                    else:
                        st.success(
                            f"ALLOWED — {commander_id} assigned as Commander, "
                            f"{second_pilot_id} assigned as Second Pilot (duties "
                            f"{result.commander_duty_id} / {result.second_pilot_duty_id})"
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


# ================= ASSIGN LM/ENGR/OTHER =================
st.subheader("Assign other crew (LM / ENGR / Other)")

crew_df = crew_service.get_all_crew(active_only=True)
if crew_df.empty:
    st.warning("No active crew on file yet — add crew in Crew Data first.")
else:
    with st.form("assign_crew_form"):
        selected_flight_ids = st.multiselect(
            "Flight(s) forming this duty *",
            options=list(flight_labels.keys()),
            format_func=lambda fid: flight_labels[fid],
            key="other_flights",
        )
        crew_id = st.selectbox(
            "Crew member *",
            options=crew_df["crew_id"],
            format_func=lambda cid: f"{cid} — {crew_df[crew_df['crew_id'] == cid]['name'].values[0]}",
            key="other_crew",
        )
        role_choice = st.selectbox("Role for this assignment *", OTHER_ROLE_OPTIONS)

        submitted = st.form_submit_button("Check legality and assign")

        if submitted:
            if not selected_flight_ids:
                st.error("Select at least one flight.")
            else:
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
st.caption(
    "Unassigning removes the selected crew member from EVERY sector of "
    "their own duty, not just one flight leg — a partial unassign used "
    "to leave a multi-sector duty corrupted (stale report/debrief/FDP "
    "on the surviving sectors), so this is duty-scoped, not per-flight."
)

if flights_df.empty or not roster_rows:
    st.info("No active assignments to unassign.")
else:
    import pandas as pd
    active_df = pd.DataFrame(roster_rows).drop_duplicates(subset=["crew_id", "duty_id"])
    unassign_choice = st.selectbox(
        "Assignment to remove",
        options=list(active_df.index),
        format_func=lambda i: (
            f"{active_df.loc[i, 'crew_id']} — {active_df.loc[i, 'role']}"
            f"{' (' + active_df.loc[i, 'operating_position'] + ')' if active_df.loc[i, 'operating_position'] else ''} "
            f"— duty {active_df.loc[i, 'duty_id']}"
        ),
        key="unassign_choice",
    )
    reason = st.text_input("Reason", key="unassign_reason")
    if st.button("Unassign"):
        row = active_df.loc[unassign_choice]
        assignment_service.remove_assignment_from_duty(
            row["crew_id"], row["duty_id"], reason=reason or None)
        st.success(f"Unassigned {row['crew_id']} from duty {row['duty_id']}")
        st.rerun()
