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

st.set_page_config(page_title="Air Eagle OCC", page_icon="✈️", layout="wide")


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
    # The clock emoji further into the string stays as literal text.
    st.success(f"🟢 Database connected — 🕐 {now_utc:%d-%m-%Y %H%M} UTC")
else:
    st.error(f"Database error: {db_status}")

st.write("Choose where to go below, or use the sidebar.")

PAGES = [
    ("pages/1_Control_Room.py", "Control Room", "🛫"),
    ("pages/2_Crew_Data.py", "Crew Data", "👨‍✈️"),
    ("pages/3_Flight_Log.py", "Flight Log", "📘"),
    ("pages/4_Roster.py", "Roster", "🗓️"),
    ("pages/5_Assistant.py", "OCC Assistant", "🤖"),
    ("pages/6_Roster_Generation.py", "Roster Generation", "⚙️"),
    ("pages/7_Schedule_Templates.py", "Schedule Templates", "📋"),
]
# Icon embedded directly in the label string, NOT passed via
# page_link's own icon= parameter. Dropping st.columns() (2026-08-12,
# first attempt) did not fix the icon-stacks-above-label problem the
# operator reported -- confirming it isn't a column-width wrapping
# issue. page_link's icon= layout is decided in Streamlit's compiled
# frontend, not visible from the Python source, so this couldn't be
# verified directly the way most other fixes in this file were. Best
# available reasoning: label text has no special icon-slot behavior
# (confirmed separately for st.success()'s leading-emoji extraction in
# this same file), so folding the icon into plain label text sidesteps
# whatever icon= does on its own. Needs the operator's own eyes on the
# rendered page to actually confirm -- said plainly, not assumed fixed.
for path, label, icon in PAGES:
    st.page_link(path, label=f"{icon} {label}", use_container_width=True)
