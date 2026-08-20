"""
home.py

The Home page's actual content — registered as a page via
st.navigation() in app.py, not the entry point itself (see app.py's
own docstring for why the entry script can't both route AND render
its own separate content under st.navigation()). Tested directly via
AppTest.from_file("home.py"), exactly like every other page in
pages/ — this file IS a page, just one that happens to live at the
repo root instead of under pages/.

Background image stays scoped to this page only: this CSS only ever
executes when THIS script runs, and st.navigation() only ever executes
the one selected page's script — so the seven working pages stay clean
by construction, unchanged from the original reasoning.

No dark overlay on the photo itself (removed 2026-08-11, operator
request — the original dim treatment made the image read too dark).
Legibility doesn't depend on it: the actual title/status/nav text sits
inside .block-container's own near-opaque white panel below, which is
what protects readability regardless of how bright the underlying
photo is.
"""
import base64
import datetime as dt

import streamlit as st
from db.db import test_connection
from services import assignment_service, auth_service, roster_generator_service

st.set_page_config(page_title="Air Eagle OCC", page_icon="✈️", layout="wide")
app_user = auth_service.require_login()


def _background_css() -> str:
    with open("assets/AE-image.jpg", "rb") as f:
        encoded = base64.b64encode(f.read()).decode()
    return f"""
    <style>
    .stApp {{
        background-image: url("data:image/jpeg;base64,{encoded}");
        background-size: cover;
        background-position: center;
        background-attachment: fixed;
    }}
    .block-container {{
        background: rgba(255, 255, 255, 0.92);
        border-radius: 12px;
        padding: 1.5rem 2rem;
        border-left: 4px solid #CDAF6F;
    }}
    </style>
    """


st.markdown(_background_css(), unsafe_allow_html=True)


def _title_html() -> str:
    # Logo inline in place of the "Air Eagle" text, at roughly double
    # the old sidebar logo's 32px "large" size — st.title() can't mix
    # an image with styled text, so this is raw markup, same
    # base64-embedding technique as the background above. The
    # trailing text is colored to match the logo's own sampled navy
    # (#001A7B, the same value already used for config.toml's
    # primaryColor) rather than a second, separately-chosen color.
    with open("assets/logo.png", "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()
    return f"""
    <div style="display:flex; align-items:center; gap:0.75rem; margin-bottom:1rem;">
        <img src="data:image/png;base64,{logo_b64}" style="height:64px;">
        <span style="font-size:2.25rem; font-weight:700; color:#001A7B;">
            — Operations Control Centre
        </span>
    </div>
    """


st.markdown(_title_html(), unsafe_allow_html=True)

# A render-time snapshot, not a live-ticking clock: Streamlit only
# re-renders on interaction, and a real tick would need an
# auto-refresh dependency (streamlit-autorefresh, not currently in
# requirements.txt) this isn't a live-monitoring screen enough to need.
now_utc = dt.datetime.now(dt.timezone.utc)

db_status = test_connection()
if db_status is True:
    # The leading emoji (only) gets extracted into st.success()'s own
    # icon slot, not left in the body -- confirmed directly (2026-08-11).
    #
    # "checked", not a clock face (2026-08-20). This was rendered as
    # "🕐 {time} UTC" and read as a live UTC clock, which it never was —
    # it is a render-time snapshot, so on a page left open it showed a
    # time ten minutes stale. Static would have been tolerable; WRONG is
    # not, on a system where times drive legality and a controller reads
    # UTC off the screen while entering duty times.
    #
    # Relabelled rather than removed: worded as the moment the check
    # happened, the timestamp does a real job — it says how fresh the
    # connection status beside it is. Removing it would lose that. No
    # auto-refresh: this page does no other work, and a refresh loop is
    # a dependency and a wakeup cycle for a line of text.
    st.success(f"🟢 Database connected — checked {now_utc:%d-%m-%Y %H%M} UTC")
else:
    st.error(f"Database error: {db_status}")


# ================= OPS STATUS =================
# Two things the operator wants on landing. Presentation over existing
# services — no query logic here.
#
# Gated on db_status, which was just established one line above. Not
# merely an optimisation: try/except catches a FAILING query but not a
# HANGING one, and against an unreachable database these two queries sit
# in connection retries until they time out. That took the home page
# from instant to over three seconds when the DB was down — the page was
# still correct and still rendered, it just stopped being usable at the
# moment the operator most needs to see something. Asking a database we
# have already been told is unreachable was never going to work; not
# asking is both faster and simpler than making the failure prettier.
#
# Each half is ALSO wrapped independently, for the case where the
# connection is up but one query fails on its own (a missing table, a
# migration not yet applied). This page's job is to load and point at
# the other pages, and a status banner must never be what stops it doing
# that — same rule as the Schedule Templates delete affordance that took
# its page down on 2026-08-19.
st.subheader("Today")

if db_status is not True:
    st.caption("Ops status unavailable while the database is unreachable.")
else:
    ops_col1, ops_col2 = st.columns(2)

    with ops_col1:
        try:
            today = dt.date.today()
            uncovered = roster_generator_service.get_open_uncovered_seats(today, today)
            st.metric("Uncovered rotation seats today", len(uncovered))
            # Precise on purpose. get_open_uncovered_seats() reads the
            # uncovered_seats table, which ONLY the roster generator
            # populates and only for rotation instances. An ad-hoc flight
            # saved without crew never appears there, so this is not "all
            # uncovered work" — see HANDOVER.md for why that boundary
            # exists and what would trigger widening it.
            st.caption(
                "Rotation-generated seats only. Ad-hoc flights saved with crew TBC "
                "are not counted here — check Flight Log for those."
            )
        except Exception as e:
            st.caption(f"Uncovered seats unavailable ({type(e).__name__}).")

    with ops_col2:
        try:
            expiry = assignment_service.qualification_expiry_counts()
            # Split, never summed. The legality gate treats expiry <=
            # duty_date as already expired, so a document expiring TODAY is
            # blocking assignments right now rather than "due soon". One
            # combined number would hide that distinction behind a word
            # that implies there is still time.
            st.metric("Crew with expired documents", expiry["expired"])
            st.metric(
                f"Crew with documents expiring in {expiry['horizon_days']} days",
                expiry["expiring"],
            )
            st.caption("Renew in Crew Data — expiry fields are on the edit form.")
        except Exception as e:
            st.caption(f"Document expiry unavailable ({type(e).__name__}).")

st.write("Use the sidebar to navigate.")
# Page-link buttons (2026-08-12: added, then removed same day) were
# genuinely redundant with the sidebar's own automatic page list --
# st.navigation() renders that regardless of anything on this page, so
# this text was never inaccurate even while the buttons existed. Per
# the operator's own call: one navigation surface, not two.
