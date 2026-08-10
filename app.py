"""
app.py

Home page. Wires page config, DB status, and navigation, plus the
first branding pass (2026-08-10): sidebar logo (st.logo(), needed on
every page individually — see pages/*.py, st.logo() sends a
per-script-run message with no app-wide persistence, confirmed
directly from Streamlit's own source) and a background image scoped
to THIS page only.

Scoping is structural, not a guard that had to be added: this CSS
block only ever executes when app.py itself runs, and Streamlit's
page navigation never re-executes app.py's script body while another
page is open — so the seven working pages stay clean by construction.

Legibility (this is a screen read at 0300 during a live disruption,
not just a home page): two layers, not one. A dark overlay is baked
into the background image itself (dims the whole photo, not just
behind text), and Streamlit's own .block-container — the real element
every st.* call below already renders inside, no markup change needed
— gets a semi-opaque background on top of that. st.success()/
st.error() already carry their own solid-ish backgrounds regardless;
the panel mainly protects the plain st.title()/st.write() text that
has none of its own.
"""
import base64

import streamlit as st
from db.db import test_connection

st.set_page_config(page_title="Air Eagle OCC", page_icon="✈️", layout="wide")
st.logo("assets/logo.png", size="large")


def _background_css() -> str:
    with open("assets/AE-image.jpg", "rb") as f:
        encoded = base64.b64encode(f.read()).decode()
    return f"""
    <style>
    .stApp {{
        background-image: linear-gradient(rgba(10, 15, 35, 0.6), rgba(10, 15, 35, 0.6)),
                           url("data:image/jpeg;base64,{encoded}");
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

st.title("Air Eagle — Operations Control Centre")

db_status = test_connection()
if db_status is True:
    st.success("Database connected")
else:
    st.error(f"Database error: {db_status}")

st.write("Use the sidebar to navigate.")
