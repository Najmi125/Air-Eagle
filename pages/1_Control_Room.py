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
from the pair model.

The single-crew path was REMOVED 2026-08-20 (see the comment further
down for why). A flight is now crewed by a pair or by nobody yet —
there is no third option, because there is no such thing as a flight
operated by one person.

Crew is OPTIONAL as of the same date: submit with a pair and flight +
both assignments are created atomically as before; submit without and
only the flight is created ("charter confirmed, crew TBC"). The two are
deliberately different calls, so the atomic path is untouched by the
optional one.

The crew controls live OUTSIDE the form (not inside it): which fields
the form shows depends on them, and a form batches its own widgets
until submit — same reasoning already established elsewhere in this
codebase for any live-updating control that needs to be read before
submission.
"""
import datetime as dt
import streamlit as st

from services import crew_service, assignment_service, auth_service, flight_service
from services.assignment_service import SEAT_ELIGIBLE_GRADES
from services.alert_summary import format_alert_lines

st.set_page_config(page_title="Control Room", page_icon="🛫", layout="wide")
app_user = auth_service.require_login()
st.title("Control Room — Ad-hoc / Charter")

crew_df = crew_service.get_all_crew(active_only=True)
if crew_df.empty:
    # No longer fatal: a flight can be recorded with crew TBC, which is
    # exactly the state a brand-new deployment is in.
    st.info("No active crew on file yet — flights can still be saved with crew TBC.")

st.subheader("Build flight and assign crew")

# The "Crew type" radio and its single-crew branch were REMOVED
# (2026-08-20). There is no such thing as a flight operated by one crew
# member. That path predated the flight-deck pair model, from when LM
# and AME were crew records assigned individually, and lost its purpose
# when they were removed from the system.
#
# It was already close to unreachable: Crew Data creates only CPT/FO/
# Other, the branch offered roles LM/ENGR/Other, and
# assign_crew_to_new_flights() rejects CPT/FO outright AND requires
# role_assigned to match the person's registered role. The one live
# combination was an "Other" crew member assigned role "Other" — which
# created a flight with one non-pilot aboard and no flight deck at all.
# That is the harmful case, and removing the UI path closes it.
#
# assign_crew_to_new_flights() itself is deliberately KEPT — see its
# docstring.

commander_pool = crew_df[crew_df["role"].isin(SEAT_ELIGIBLE_GRADES["COMMANDER"])]
second_pilot_pool_full = crew_df[crew_df["role"].isin(SEAT_ELIGIBLE_GRADES["SECOND_PILOT"])]
pair_possible = not (commander_pool.empty or second_pilot_pool_full.empty)

# Crew is OPTIONAL (2026-08-20). "Charter confirmed, crew TBC" is a real
# operational state and previously had no path here at all, which forced
# the operator into Flight Log for the same job and made this page look
# redundant. An uncrewed flight is not a new concept in the system:
# Flight Log has always created them, every flights-to-roster join is
# roster-driven so one simply doesn't appear, and
# reports.roster_coverage() already reports an empty seat as UNCOVERED.
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
        # No st.stop() any more: the flight-only path is still available,
        # so a missing Second Pilot must not take the whole page out.
        st.error(
            "No eligible Second Pilot left once the Commander is excluded — "
            "untick 'Assign a flight-deck pair now' to save the flight without crew."
        )

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
    # Free text by design — the system does not classify why someone is
    # aboard. Every real Air Eagle flight carries an LM and AMEs, and
    # until 2026-08-20 there was nowhere to record them: the columns
    # (migrations/010) and reports.roster_coverage()'s display of them
    # both existed, but no page ever wrote them.
    other_occupants_operating = st.text_input(
        "Other occupants — operating (aboard and working, e.g. 'Abdulghani (LM), 2x AME')")
    other_occupants_non_operating = st.text_input(
        "Other occupants — non-operating (aboard, not operating — reason in Remarks)")

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
                # Flight only. Deliberately a DIFFERENT call rather than
                # a mode inside assign_pair_to_new_flights(): that
                # function's guarantee is that an illegal pair leaves no
                # orphan flight, and the cleanest way to preserve it is
                # to not touch it. There is no crew here, so there is no
                # legality gate to run and nothing to be atomic with.
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
