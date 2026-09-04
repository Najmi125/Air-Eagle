"""
pages/4_Roster.py

Assigns crew to flights that already exist — the
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
import pandas as pd

from services import crew_service, flight_service, assignment_service, auth_service
from services.assignment_service import SEAT_ELIGIBLE_GRADES

# The grades that can hold a flight-deck seat at all. Derived from
# SEAT_ELIGIBLE_GRADES rather than retyped, so a grade added there
# cannot quietly become "not cockpit" here — which is what would decide
# whether a missing operating_position reads as an anomaly or as
# normal.
COCKPIT_GRADES = frozenset().union(*SEAT_ELIGIBLE_GRADES.values())
from services.alert_summary import format_alert_lines
from services.display_labels import (crew_label, crew_seat_name, format_timestamps,
                                      flight_label,
                                      flight_labels as build_flight_labels)

st.set_page_config(page_title="Roster", page_icon="🗓️", layout="wide")
app_user = auth_service.require_login()
st.title("Roster")

# Messages that must OUTLIVE an st.rerun() — see HANDOVER 2026-09-03.
# Anything written before st.rerun() is discarded and never reaches the
# browser, so a confirmation written next to the action that triggers
# the rerun is a confirmation nobody sees.
_ROSTER_NOTICES = "roster_notices"

for _notice in st.session_state.pop(_ROSTER_NOTICES, []):
    {"error": st.error, "warning": st.warning}.get(
        _notice["level"], st.success)(_notice["headline"])


def queue_roster_notice(level, headline):
    """Hold a message for the run AFTER the imminent st.rerun()."""
    st.session_state.setdefault(_ROSTER_NOTICES, []).append(
        {"level": level, "headline": headline})

OTHER_ROLE_OPTIONS = ["LM", "ENGR", "Other"]


# Built ONCE, unconditionally, and used by two sections. It sat inside
# `if roster_rows:` until 2026-09-05, which was safe only while the
# one section that used it lived in the same branch — the flagged-for-
# review section below runs whether or not anything is assigned, and
# would have raised NameError on an empty roster. Same shape as the
# stale-module page outage: a name defined in one branch and read from
# another looks fine right up until the first branch does not run.
crew_names = {
    r["crew_id"]: crew_seat_name(r)
    for _, r in crew_service.get_all_crew(active_only=False).iterrows()
}

# ================= CURRENT ROSTER =================
st.subheader("Current assignments")

flights_df = flight_service.get_all_flights()
if flights_df.empty:
    st.info("No flights yet — create one in Control Room first.")
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
                # `a["operating_position"] or ""` was NOT safe: when
                # every roster row on a flight has a NULL seat — an
                # LM/ENGR-only flight — pandas gives the column
                # float64, and `nan or ""` evaluates to nan because nan
                # is TRUTHY. The unassign selectbox then concatenated a
                # float to a string and the page raised TypeError.
                # Latent rather than live, since Air Eagle holds no
                # LM/ENGR crew records today; found by a fixture that
                # did (2026-09-03).
                "operating_position": ("" if pd.isna(a["operating_position"])
                                       else str(a["operating_position"])),
                "duty_id": a["duty_id"], "fdp_hours": a["fdp_hours"],
            })
    # ONE ROW PER FLIGHT, not one per crew member per sector
    # (2026-09-03). The seats are the point: a controller reads a
    # flight and wants to know who is in each one. crew_id, role and
    # duty_id were internal identifiers on a screen nobody debugs from,
    # and flight_id has a flight number.
    #
    # COMMANDER / SECOND PILOT, not PIC / SIC. PIC and SIC are the
    # operator's own words and migrations/016 records them as
    # equivalent — but roster_coverage's headers were standardised on
    # Commander/Second Pilot on 2026-08-28, and two screens naming one
    # concept differently is worse than either name. A DELIBERATE
    # choice, not the schema leaking through.
    if roster_rows:

        by_flight = {}
        for row in roster_rows:
            seats = by_flight.setdefault(row["flight_id"], {})
            position = row["operating_position"]
            if position in ("COMMANDER", "SECOND_PILOT"):
                seats[position] = row["crew_id"]
            elif row["role"] in COCKPIT_GRADES:
                # A CPT or FO holding NO recorded seat is an ANOMALY —
                # someone occupies a flight-deck position the data
                # failed to record, and dropping them hides a real
                # assignment. Same treatment as roster_coverage.
                seats.setdefault("seatless", []).append(row["crew_id"])
            # An LM or ENGR has no operating position BY DESIGN — they
            # are outside the flight-deck model entirely, so there is
            # nothing here to omit. Deliberately NOT the same case as
            # the line above, and kept apart in code because the same
            # NULL column means opposite things depending on grade;
            # conflating them is how the anomaly gets swallowed.

        def seat_cell(seats, position):
            crew_id = seats.get(position)
            return crew_names.get(crew_id, crew_id) if crew_id else "UNCOVERED"

        table = []
        for _, flight in flights_df.iterrows():
            seats = by_flight.get(flight["flight_id"])
            # A flight with no flight-deck assignment at all does not
            # belong in "Current assignments" — including one because
            # somebody loaded cargo would be noise, and it is the same
            # reason a wholly uncrewed flight stays out. Roster
            # Generation's uncovered panel is where those live.
            if not seats:
                continue
            entry = {
                "Flight": flight_label(flight),
                "Route": f'{flight["origin"]}→{flight["destination"]}',
                "Commander": seat_cell(seats, "COMMANDER"),
                "Second Pilot": seat_cell(seats, "SECOND_PILOT"),
            }
            if seats.get("seatless"):
                entry["Seat not recorded"] = ", ".join(
                    crew_names.get(cid, cid) for cid in seats["seatless"])
            table.append(entry)

        if table:
            st.dataframe(format_timestamps(pd.DataFrame(table)),
                         width="stretch", hide_index=True)
        else:
            st.info("No crew assigned yet.")
    else:
        st.info("No crew assigned yet.")


# ================= DUTIES FLAGGED FOR REVIEW =================
# PLACED HERE, above the assignment forms, for two reasons. The
# forms below begin `if flights_df.empty: st.stop()`, so anything
# after them silently never renders on a database with no flights.
# And a duty nobody has looked at yet is the first thing a
# controller should meet on this page, not the last.
# Added 2026-09-05 with crew-change revalidation, and it is the half
# that makes the other half safe to ship.
#
# Until now NOTHING could clear a NEEDS_REVIEW flag. The only writer
# was _recompute_one_duty_after_delay(); no service, page or migration
# ever reversed it, and no page even LISTED the flagged duties. Adding
# a second flagger — a crew correction, which can flag many duties at
# once — without an exit would have made the correction path something
# people avoid rather than use, and a safety flag nobody can close is a
# safety flag people learn to ignore.
#
# Clearing is deliberately human and deliberately explained: the flag
# does not record "the data is bad", it records "nobody has looked at
# this duty since the data changed". Only a person can retire that, and
# the reason goes in the audit trail.
st.divider()
st.subheader("Duties flagged for review")

# WRAPPED, like every other read this codebase has learned to wrap: a
# section added to a page must not be able to take that page off the
# air. The Roster page's real job is assigning crew, and a listing of
# flagged duties is an affordance on top of it — the same reasoning as
# the Schedule Templates delete control, which DID take its page down
# on 2026-08-19 before it was wrapped.
try:
    flagged = assignment_service.duties_needing_review()
except Exception as exc:
    flagged = None
    st.caption(f"Flagged duties unavailable ({type(exc).__name__}).")

if flagged is None:
    pass
elif flagged.empty:
    st.caption("No duties are currently flagged for review.")
else:
    st.warning(
        f"{flagged['duty_id'].nunique()} duty(ies) need a human to look at "
        f"them. They were flagged because something they depend on changed "
        f"after they were written — a crew record corrected, or a delay "
        f"recorded — and nothing clears that automatically."
    )
    st.dataframe(
        format_timestamps(pd.DataFrame([
            {
                "Duty": row["duty_id"],
                "Crew": crew_names.get(row["crew_id"], row["crew_id"]),
                "Seat": (row["operating_position"] or "").replace("_", " ").title(),
                "Flight": f'{row["flight_no"] or "#" + str(row["flight_id"])} '
                          f'{row["origin"]}→{row["destination"]}',
                "Reports": row["report_time"],
            }
            for _, row in flagged.iterrows()
        ])),
        width="stretch", hide_index=True,
    )

    review_choice = st.selectbox(
        "Duty to clear", sorted(flagged["duty_id"].unique()), key="review_clear_choice")
    review_reason = st.text_input(
        "What did you check? (required)", key="review_clear_reason")

    if st.button("Clear review flag"):
        try:
            cleared = assignment_service.clear_duty_review_flag(
                review_choice, review_reason, app_user=app_user)
        except ValueError as exc:
            # A reason is required, and saying so beats a traceback.
            st.error(str(exc))
        else:
            if cleared:
                queue_roster_notice(
                    "success",
                    f"Review flag cleared on duty {review_choice} "
                    f"({cleared} roster row(s) back to PLANNED).")
            else:
                queue_roster_notice(
                    "warning",
                    f"Duty {review_choice} was not flagged — nothing changed.")
            st.rerun()


# ================= ASSIGN FLIGHT-DECK PAIR =================
# LABEL ONLY (operator request, 2026-09-05). The form underneath is
# untouched: it still calls assignment_service.assign_pair_to_duty(),
# which validates the Commander and the Second Pilot TOGETHER and
# commits both or neither — the atomic-pair guarantee — and still
# runs the full legality check plus the downstream swap-alert scan on
# every submission. Renaming a heading cannot reach any of that; it is
# checked here rather than asserted because "label only" is the kind of
# claim that is worth being able to point at.
st.subheader("Replace crew")

if flights_df.empty:
    st.stop()

commander_pool = crew_service.get_all_crew(active_only=True)
commander_pool = commander_pool[commander_pool["role"].isin(SEAT_ELIGIBLE_GRADES["COMMANDER"])]
second_pilot_pool_full = crew_service.get_all_crew(active_only=True)
second_pilot_pool_full = second_pilot_pool_full[second_pilot_pool_full["role"].isin(SEAT_ELIGIBLE_GRADES["SECOND_PILOT"])]

# Flight number leading, not flight_id: a controller thinks "EPE 786".
# The date is part of it because numbers repeat daily, and the route
# distinguishes two same-numbered options. flight_id stays the
# identifier — it is only the LABEL that changes.
flight_labels = build_flight_labels(flights_df, include_route=True)

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
            format_func=lambda cid: crew_label(commander_pool[commander_pool["crew_id"] == cid].iloc[0]),
        )
        second_pilot_options = second_pilot_pool_full[second_pilot_pool_full["crew_id"] != commander_id]
        second_pilot_id = col2.selectbox(
            "Second Pilot (CPT or FO) *", options=second_pilot_options["crew_id"],
            format_func=lambda cid: crew_label(second_pilot_options[second_pilot_options["crew_id"] == cid].iloc[0]),
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
                    result = assignment_service.assign_pair_to_duty(
                        commander_id, second_pilot_id, ordered_ids, app_user=app_user)
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
            format_func=lambda cid: crew_label(crew_df[crew_df["crew_id"] == cid].iloc[0]),
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
                    result = assignment_service.assign_crew_to_duty(
                        crew_id, ordered_ids, role_choice, app_user=app_user)
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
            row["crew_id"], row["duty_id"], reason=reason or None, app_user=app_user)
        st.success(f"Unassigned {row['crew_id']} from duty {row['duty_id']}")
        st.rerun()
