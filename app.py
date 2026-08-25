"""
app.py

Entry point / router only (2026-08-11, moved to st.navigation()).
Registers every page explicitly and hands off to whichever is
selected; the entry script never renders its own content separately
from the router itself, so the Home page's actual content lives in
home.py, registered here as just another page (default=True) exactly
like the other seven.

Why st.navigation() instead of renaming app.py to Home.py to fix the
sidebar's "app" label: read streamlit/source_util.py directly first —
the classic pages/-directory pattern derives every nav label purely
from the script's own filename via regex, with no override available,
so a rename really was the only way to change that label under that
pattern. But renaming the entry script requires manually updating
Streamlit Community Cloud's "Main file path" setting — outside this
repo, in lockstep with the merge, with nothing in the codebase able to
warn if it's missed, discovered only after an apparently-successful
deploy. st.navigation()'s st.Page(title=...) sets the sidebar label
directly, decoupled from filename, so app.py never needs to be
renamed at all — confirmed directly, not assumed, that the existing
seven pages/*.py files need zero changes to work this way: each still
calls its own st.set_page_config() when reached via pg.run(), and
AppTest.from_file("pages/...") is unaffected since it execs a page's
script directly with no dependency on the entry point either way.
"""
import streamlit as st

pg = st.navigation([
    st.Page("home.py", title="Home", icon="🏠", default=True),
    st.Page("pages/1_Control_Room.py", title="Control Room", icon="🛫"),
    st.Page("pages/2_Crew_Data.py", title="Crew Data", icon="👨‍✈️"),
    st.Page("pages/3_Flight_Log.py", title="Flt Schedule", icon="📘"),
    st.Page("pages/4_Roster.py", title="Roster", icon="🗓️"),
    st.Page("pages/5_Assistant.py", title="OCC Assistant", icon="🤖"),
    st.Page("pages/6_Roster_Generation.py", title="Roster Generation", icon="⚙️"),
    st.Page("pages/7_Schedule_Templates.py", title="Schedule Templates", icon="📋"),
])
pg.run()
