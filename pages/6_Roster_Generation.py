"""
pages/6_Roster_Generation.py

Phase 7's first UI — presentation only. Everything this page calls
already exists and is already tested: services/roster_generator_
service.py's generate_for_window()/publish_window()/get_open_
uncovered_seats(), services/rotation_template_service.py's
get_instances(). No new service logic lives here; this page's only
job is to make the generator's real output legible to a controller
and let them act on it.

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
a rotation-linked seat), not just this page's own last run. That's
why it's shown as its own always-current panel, independent of
whether Generate has been clicked this session, right after the date
picker — the most actionable output, first and most prominent, never
buried under a success count.
"""
import datetime as dt

import pandas as pd
import streamlit as st

from services import assignment_service, crew_service, roster_generator_service, rotation_template_service
from services.roster_generator_service import SEATS

st.set_page_config(page_title="Roster Generation", page_icon="⚙️", layout="wide")
st.title("Roster Generation")

st.markdown(
    "Fills the Commander + Second Pilot seats for already-**approved** "
    "rotations in a date window, using the same legality gate as every "
    "manual assignment — then publishes the result. This page doesn't "
    "create or approve rotations; that happens on the Schedule "
    "Templates page (sidebar)."
)

# This page's own first use of st.session_state, holding the
# just-generated GenerationSummary so it survives the rerun a later
# Publish click triggers. This is NOT a repeat of this project's hard
# lesson ("session_state lost unassigned count on refresh -> persist
# to DB, session state is demo-only"): that lesson is about
# OPERATIONAL data the system is responsible for. Here, the real
# effect of Generate — the PROPOSED roster rows — is already durably
# written to the database the moment generate_for_window() returns;
# losing this in-memory summary on a refresh only loses the DISPLAY of
# what just happened. generate_for_window() is idempotent by design,
# so re-running to see the summary again isn't a workaround, it's the
# intended recovery path. Do not "fix" this by persisting
# GenerationSummary to the database — that would solve a problem that
# doesn't exist.
if "generation_summary" not in st.session_state:
    st.session_state.generation_summary = None
    st.session_state.generation_window = None

# Built once per render, reused for every crew-name lookup below —
# same pattern as pages/5_Assistant.py's crew_directory.
crew_directory = {
    row["crew_id"]: row["name"]
    for _, row in crew_service.get_all_crew(active_only=False).iterrows()
}

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
# been run this session, and regardless of which write path (a
# generator search or a manual unassign) left the seat open.
# ------------------------------------------------------------------
st.divider()
st.subheader("Currently uncovered seats")

open_uncovered = roster_generator_service.get_open_uncovered_seats(date_from, date_to)
if open_uncovered.empty:
    st.success("No open uncovered seats in this window.")
else:
    st.error(f"{len(open_uncovered)} seat(s) currently uncovered — action needed.")
    st.dataframe(pd.DataFrame([
        {
            "Rotation": row["rotation_code"],
            "Date": row["rotation_date"],
            "Position": row["operating_position"],
            "Reason": row["reason"],
            "Since": row["generated_at"],
        }
        for _, row in open_uncovered.iterrows()
    ]), width="stretch", hide_index=True)

# ------------------------------------------------------------------
# Pre-generate preview — the same APPROVED + window filter
# generate_for_window() applies internally, computed here too so a
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
        "Re-running Generate only fills genuine gaps and never touches "
        "existing assignments — safe to run again, nothing is lost by doing so."
    )

    if st.button("Generate", type="primary"):
        with st.spinner(f"Filling {rotation_count} rotation(s) — {estimate_text}, this can take a while..."):
            summary = roster_generator_service.generate_for_window(date_from, date_to)
        st.session_state.generation_summary = summary
        st.session_state.generation_window = (date_from, date_to)
        st.rerun()

# ------------------------------------------------------------------
# Results
# ------------------------------------------------------------------
summary = st.session_state.generation_summary
if summary is not None:
    gen_from, gen_to = st.session_state.generation_window
    st.divider()
    st.subheader("Results")
    st.caption(f"For the last-generated window: {gen_from} – {gen_to}")

    # Uncovered is NOT re-shown here — the "Currently uncovered seats"
    # panel above is durable (reads uncovered_seats directly) and
    # already reflects this run's outcome on this rerun; duplicating it
    # from the in-memory summary would risk the two disagreeing.
    if summary.uncovered:
        st.warning(
            f"{len(summary.uncovered)} seat(s) newly uncovered this run "
            f"— see the panel above for the real reasons."
        )
    else:
        st.success("Every seat this run attempted was filled or already covered.")

    # Per-seat duty counts derived from `filled` only — GenerationSummary.
    # filled already has exactly one SeatResult per seat successfully
    # filled THIS run (duty-level, not sector-level), so counting
    # crew_id occurrences per operating_position is the duty count
    # directly.
    st.markdown("**Duty counts this run (fairness check)**")
    fair_col1, fair_col2 = st.columns(2)
    for col, position in ((fair_col1, SEATS[0]), (fair_col2, SEATS[1])):
        counts: dict = {}
        for s in summary.filled:
            if s.operating_position == position and s.crew_id:
                counts[s.crew_id] = counts.get(s.crew_id, 0) + 1
        col.markdown(f"*{position.replace('_', ' ').title()}*")
        if counts:
            counts_df = pd.DataFrame([
                {"Crew": f"{cid} ({crew_directory.get(cid, 'unknown')})", "Duties filled": n}
                for cid, n in sorted(counts.items())
            ])
            col.dataframe(counts_df, width="stretch", hide_index=True)
        else:
            col.caption("No seats filled this run.")

    # De-emphasized — confirmation the idempotency worked, not news.
    st.caption(
        f"{len(summary.already_covered)} seat(s) were already covered "
        f"before this run."
    )
    if summary.already_covered:
        with st.expander("Show already-covered seats"):
            st.dataframe(pd.DataFrame([
                {"Rotation": s.rotation_code, "Date": s.rotation_date,
                 "Position": s.operating_position,
                 "Crew": f"{s.crew_id} ({crew_directory.get(s.crew_id, 'unknown')})"}
                for s in summary.already_covered
            ]), width="stretch", hide_index=True)

    with st.expander("Show all filled seats (raw)"):
        if summary.filled:
            st.dataframe(pd.DataFrame([
                {"Rotation": s.rotation_code, "Date": s.rotation_date,
                 "Position": s.operating_position,
                 "Crew": f"{s.crew_id} ({crew_directory.get(s.crew_id, 'unknown')})"}
                for s in summary.filled
            ]), width="stretch", hide_index=True)
        else:
            st.caption("Nothing filled this run.")

# ------------------------------------------------------------------
# Publish — deliberately independent of whether Generate ran in THIS
# session. Computed fresh from the database on every render for the
# currently selected window, so a controller returning later to
# publish something generated earlier (e.g. after reviewing/rejecting
# proposals on the Roster page) doesn't need to re-run Generate first.
# ------------------------------------------------------------------
st.divider()
st.subheader("Publish")
st.caption(
    "Publishing promotes PROPOSED assignments to PLANNED for this window "
    "— that's what makes them visible to crew. Each rotation is "
    "re-validated fresh right before it flips (crew data or other "
    "duties may have changed since it was proposed); a rotation that "
    "fails re-validation, or no longer has both seats filled, is "
    "skipped and left as PROPOSED rather than blocking the rest of "
    "the window."
)

proposed_rows = assignment_service.search_roster(date_from=date_from, date_to=date_to, include_proposed=True)
proposed_count = int((proposed_rows["status"] == "PROPOSED").sum()) if not proposed_rows.empty else 0
st.write(f"**{proposed_count}** PROPOSED roster row(s) in this window.")

st.info(
    "To reject a proposed assignment before publishing, unassign it on "
    "the Roster page — that marks it CANCELLED, so publishing skips it "
    "and publishes the rest. That's the whole review mechanism; no "
    "separate approval step exists."
)

if st.button("Publish", disabled=(proposed_count == 0)):
    published = roster_generator_service.publish_window(date_from, date_to)
    remaining = assignment_service.search_roster(date_from=date_from, date_to=date_to, include_proposed=True)
    remaining_count = int((remaining["status"] == "PROPOSED").sum()) if not remaining.empty else 0
    st.success(f"Published {published} roster row(s).")
    if remaining_count:
        st.warning(
            f"{remaining_count} row(s) remain PROPOSED — the rotation(s) "
            f"behind them failed re-validation or weren't fully paired "
            f"at publish time. Check the panel above, or the Roster page."
        )
    st.rerun()
