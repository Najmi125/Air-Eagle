"""
pages/6_Roster_Generation.py

Phase 7's first UI — presentation only. Everything this page calls
already exists and is already tested: services/roster_generator_
service.py's generate_preview()/accept_preview()/publish_window()/
get_open_uncovered_seats(), services/rotation_template_service.py's
get_instances(). No new service logic lives here; this page's only job
is to make the generator's real output legible to a controller and let
them act on it.

SCOPE: this page assumes approved rotation instances already exist.
It does not create or approve rotations — that happens on the
Schedule Templates page (pages/7_Schedule_Templates.py). If no
approved rotations fall in the chosen window, this page says so
plainly and points there rather than failing obscurely on an empty
result.

Rebuilt for the flight-deck crew package (2026-08-13): seats are
Commander/Second Pilot, not CPT/FO, and uncovered is now read from
the durable uncovered_seats table (get_open_uncovered_seats()) rather
than only the in-memory GenerationSummary — it survives a refresh,
and reflects BOTH writers (a generator search that found no legal
pair, and a controller's manual unassign on the Roster page vacating
a rotation-linked seat), not just this page's own last run.

REBUILT AGAIN for preview/accept: Generate no longer writes anything.
It produces a proposal the controller reads and then accepts, and the
accept re-validates every rotation against fresh data before committing
it. What changed for this page is not just a second button — it is that
the screen between the two clicks is now the only place the proposal
exists, and after the accept the SAME screen becomes the report of what
happened to it.
"""
import datetime as dt

import pandas as pd
import streamlit as st

from services import assignment_service, auth_service, crew_service, roster_generator_service, rotation_template_service
from services.display_labels import format_timestamps
from services.roster_generator_service import (
    SEATS, OUTCOME_PROPOSED, OUTCOME_WRITTEN, OUTCOME_REJECTED,
    OUTCOME_UNCOVERED, OUTCOME_ALREADY_COVERED,
)

st.set_page_config(page_title="Roster Generation", page_icon="⚙️", layout="wide")
app_user = auth_service.require_login()
st.title("Roster Generation")

st.markdown(
    "Proposes the Commander + Second Pilot pair for already-**approved** "
    "rotations in a date window, using the same legality gate as every "
    "manual assignment. **Generate writes nothing** — it produces a "
    "proposal you review here. Accept commits it, re-checking every "
    "rotation against current data first. This page doesn't create or "
    "approve rotations; that happens on the Schedule Templates page "
    "(sidebar)."
)

# The preview lives HERE and nowhere else until it is accepted, which
# is a real change from the GenerationSummary this replaced.
#
# That summary was safe to lose on a refresh because the actual effect
# of Generate — the PROPOSED roster rows — was already in the database;
# only the display was in memory. Now the proposal IS the effect, and
# losing it loses work. The page says so out loud below rather than
# quietly persisting it: writing an unreviewed proposal to the database
# to make a refresh survivable would put back exactly the speculative
# write this redesign removed, and would restore the bug where the
# roster table is the only place a half-reviewed roster exists.
# Re-running Generate is the recovery path; it is idempotent and cheap.
if "generation_preview" not in st.session_state:
    st.session_state.generation_preview = None

# Built once per render, reused for every crew-name lookup below —
# same pattern as pages/5_Assistant.py's crew_directory.
crew_directory = {
    row["crew_id"]: row["name"]
    for _, row in crew_service.get_all_crew(active_only=False).iterrows()
}


def crew_label(crew_id):
    if not crew_id:
        return "—"
    return f"{crew_id} ({crew_directory.get(crew_id, 'unknown')})"


def seat_rows(rotations):
    """One display row per rotation, both seats side by side."""
    return pd.DataFrame([
        {
            "Rotation": r.rotation_code,
            "Date": r.rotation_date,
            "Commander": crew_label(r.seats["COMMANDER"].crew_id),
            "Second Pilot": crew_label(r.seats["SECOND_PILOT"].crew_id),
        }
        for r in rotations
    ])


today = dt.date.today()

col1, col2 = st.columns(2)
date_from = col1.date_input("From", value=today, key="gen_date_from")
date_to = col2.date_input("To", value=today + dt.timedelta(days=27), key="gen_date_to")

if date_from > date_to:
    st.error("'From' must not be after 'To'.")
    st.stop()

window_days = (date_to - date_from).days + 1
if window_days > 35:
    st.warning(
        f"This window spans {window_days} days — 28 days is the normal "
        f"operational horizon. A wider window means a longer generation "
        f"wait; narrow it if that wasn't intentional. (Not blocked — "
        f"running two cycles together is a legitimate reason to widen it.)"
    )

# ------------------------------------------------------------------
# Currently uncovered — durable, from uncovered_seats directly. Always
# current for the selected window regardless of whether Generate has
# been run this session, and regardless of which write path (an
# accepted generation or a manual unassign) left the seat open.
#
# A preview that has NOT been accepted contributes nothing here, which
# is correct: a proposal a controller walked away from must not have
# edited the durable record of what is open.
# ------------------------------------------------------------------
st.divider()
st.subheader("Currently uncovered seats")

open_uncovered = roster_generator_service.get_open_uncovered_seats(date_from, date_to)
if open_uncovered.empty:
    st.success("No open uncovered seats in this window.")
else:
    st.error(f"{len(open_uncovered)} seat(s) currently uncovered — action needed.")
    # "Since" is a real timestamp; "Date" is a plain date and is left
    # alone by format_timestamps, which is what keeps its year.
    st.dataframe(format_timestamps(pd.DataFrame([
        {
            "Rotation": row["rotation_code"],
            "Date": row["rotation_date"],
            "Position": row["operating_position"],
            "Reason": row["reason"],
            "Since": row["generated_at"],
        }
        for _, row in open_uncovered.iterrows()
    ])), width="stretch", hide_index=True)

# ------------------------------------------------------------------
# Pre-generate preview — the same APPROVED + window filter
# generate_preview() applies internally, computed here too so a
# controller sees what they're about to run before running it.
# ------------------------------------------------------------------
approved = rotation_template_service.get_instances(status="APPROVED")
if not approved.empty:
    approved = approved[
        (approved["rotation_date"] >= date_from) & (approved["rotation_date"] <= date_to)
    ]
rotation_count = len(approved)

if rotation_count == 0:
    st.info(
        "No approved rotations in this window. Rotations need to be "
        "approved first, on the Schedule Templates page (sidebar) "
        "— there's nothing to generate here until one exists."
    )
else:
    est_seconds = rotation_count * (120 / 36)  # measured: ~2 min for 36 rotations x 2 seats
    estimate_text = f"~{round(est_seconds)}s" if est_seconds < 60 else f"~{est_seconds / 60:.1f} min"
    st.write(f"**{rotation_count}** approved rotation(s) in this window — estimated time {estimate_text}.")
    st.caption(
        "Generate only proposes crew for genuine gaps and never touches "
        "existing assignments — safe to run again, and running it again "
        "writes nothing either."
    )

    if st.button("Generate", type="primary"):
        with st.spinner(f"Checking {rotation_count} rotation(s) — {estimate_text}, this can take a while..."):
            preview = roster_generator_service.generate_preview(date_from, date_to, app_user=app_user)
        st.session_state.generation_preview = preview
        st.rerun()

# ------------------------------------------------------------------
# The proposal, and after Accept, the report of what happened to it.
# Deliberately the SAME section: a controller who clicks Accept should
# find the answer where the question was, not in a new panel elsewhere.
# ------------------------------------------------------------------
preview = st.session_state.generation_preview
if preview is not None:
    st.divider()
    proposed = preview.by_outcome(OUTCOME_PROPOSED)
    uncovered = preview.by_outcome(OUTCOME_UNCOVERED)
    already = preview.by_outcome(OUTCOME_ALREADY_COVERED)

    if not preview.is_accepted:
        st.subheader("Proposed roster — not yet written")
        st.caption(f"For the window {preview.date_from} – {preview.date_to}.")

        st.info(
            "**Nothing has been written to the database.** These pairings "
            "exist only on this screen. Accept commits them; leaving this "
            "page discards them, and re-running Generate rebuilds them."
        )

        if proposed:
            st.write(f"**{len(proposed)}** rotation(s) proposed — "
                     f"{preview.seat_count(OUTCOME_PROPOSED)} seat(s).")
            st.dataframe(seat_rows(proposed), width="stretch", hide_index=True)
        else:
            st.warning("Nothing to propose — no rotation in this window has a fillable seat.")

        if uncovered:
            st.error(
                f"{len(uncovered)} rotation(s) could not be crewed. Accepting "
                f"records these as uncovered seats so they show in the panel "
                f"above and don't get forgotten."
            )
            with st.expander("Why each one could not be crewed"):
                for rotation in uncovered:
                    st.markdown(f"**{rotation.rotation_code} · {rotation.rotation_date}**")
                    st.caption(rotation.outcome_reason or "no detail")

        if already:
            st.caption(f"{len(already)} rotation(s) were already fully crewed before this run.")
            with st.expander("Show already-covered rotations"):
                st.dataframe(seat_rows(already), width="stretch", hide_index=True)

        # Per-seat duty counts across the proposal — the fairness check,
        # available BEFORE anything is committed rather than after, which
        # is the point of proposing first.
        st.markdown("**Duty counts in this proposal (fairness check)**")
        fair_col1, fair_col2 = st.columns(2)
        for col, position in ((fair_col1, SEATS[0]), (fair_col2, SEATS[1])):
            counts: dict = {}
            for rotation in proposed:
                seat = rotation.seats[position]
                if seat.crew_id and not seat.already_real:
                    counts[seat.crew_id] = counts.get(seat.crew_id, 0) + 1
            col.markdown(f"*{position.replace('_', ' ').title()}*")
            if counts:
                col.dataframe(pd.DataFrame([
                    {"Crew": crew_label(cid), "Duties proposed": n}
                    for cid, n in sorted(counts.items())
                ]), width="stretch", hide_index=True)
            else:
                col.caption("No seats proposed for this position.")

        st.caption(
            "Accept re-checks every rotation against current data before "
            "writing it. A rotation that no longer passes is left "
            "unwritten and reported here; the rest still commit."
        )
        if st.button("Accept", type="primary", disabled=not (proposed or uncovered)):
            with st.spinner("Re-checking and writing..."):
                roster_generator_service.accept_preview(preview, app_user=app_user)
            st.rerun()

    else:
        # ----------------------------------------------------------
        # Accepted. The rejected rotations are the whole point of this
        # view, so they lead; the successes are collapsed.
        # ----------------------------------------------------------
        written = preview.by_outcome(OUTCOME_WRITTEN)
        rejected = preview.by_outcome(OUTCOME_REJECTED)

        st.subheader("Accepted")
        st.caption(f"For the window {preview.date_from} – {preview.date_to}.")

        if rejected:
            st.error(
                f"**{len(written)} rotation(s) written, {len(rejected)} refused "
                f"on re-check.** The refused rotation(s) below were proposed "
                f"but no longer pass — crew data or other duties changed "
                f"between Generate and Accept. Nothing was written for them."
            )
            for rotation in rejected:
                st.markdown(
                    f"**{rotation.rotation_code} · {rotation.rotation_date}** — "
                    f"proposed {crew_label(rotation.seats['COMMANDER'].crew_id)} / "
                    f"{crew_label(rotation.seats['SECOND_PILOT'].crew_id)}"
                )
                st.caption(rotation.outcome_reason or "no detail")
            st.info(
                "**Run Generate again to re-crew these.** The rotations that "
                "just committed have changed what is legal for the ones that "
                "didn't — the pilots proposed above were chosen against a "
                "roster that no longer exists, so re-offering them would be "
                "proposing from stale information. A fresh Generate sees the "
                "rows that were just written."
            )
        else:
            st.success(f"All {len(written)} proposed rotation(s) were written.")

        if uncovered:
            st.warning(
                f"{len(uncovered)} rotation(s) were recorded as uncovered — "
                f"see the panel at the top of this page, which is now current."
            )

        if written:
            with st.expander(f"Show the {len(written)} rotation(s) written"):
                st.dataframe(seat_rows(written), width="stretch", hide_index=True)

        st.caption(
            "This proposal has been used and can't be accepted again. "
            "Generate produces a fresh one."
        )

# ------------------------------------------------------------------
# Publish — deliberately independent of whether Generate ran in THIS
# session. Computed fresh from the database on every render for the
# currently selected window, so a controller returning later to
# publish something accepted earlier (e.g. after reviewing/rejecting
# proposals on the Roster page) doesn't need to re-run Generate first.
# ------------------------------------------------------------------
st.divider()
st.subheader("Publish")
st.caption(
    "Publishing promotes accepted assignments to PLANNED for this window "
    "— that's what makes them visible to crew. Each rotation is "
    "re-validated fresh right before it flips (crew data or other "
    "duties may have changed since it was accepted); a rotation that "
    "fails re-validation, or no longer has both seats filled, is "
    "skipped and left unpublished rather than blocking the rest of "
    "the window."
)

proposed_rows = assignment_service.search_roster(date_from=date_from, date_to=date_to, include_proposed=True)
proposed_count = int((proposed_rows["status"] == "PROPOSED").sum()) if not proposed_rows.empty else 0
st.write(f"**{proposed_count}** accepted-but-unpublished roster row(s) in this window.")

st.info(
    "To reject an accepted assignment before publishing, unassign it on "
    "the Roster page — that marks it CANCELLED, so publishing skips it "
    "and publishes the rest. To reject one BEFORE it is written, don't "
    "accept the proposal above."
)

if st.button("Publish", disabled=(proposed_count == 0)):
    published = roster_generator_service.publish_window(date_from, date_to, app_user=app_user)
    remaining = assignment_service.search_roster(date_from=date_from, date_to=date_to, include_proposed=True)
    remaining_count = int((remaining["status"] == "PROPOSED").sum()) if not remaining.empty else 0
    st.success(f"Published {published} roster row(s).")
    if remaining_count:
        st.warning(
            f"{remaining_count} row(s) remain unpublished — the rotation(s) "
            f"behind them failed re-validation or weren't fully paired "
            f"at publish time. Check the panel above, or the Roster page."
        )
    st.rerun()
