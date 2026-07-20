"""
app.py

Home page. Deliberately unstyled — UI/branding work is parked for
later. This just needs to correctly wire page config, DB status, and
navigation. See pages/2_Crew_Data.py for the first real feature page.
"""
import streamlit as st
from db.db import test_connection

st.set_page_config(page_title="Air Eagle OCC", page_icon="✈️", layout="wide")

st.title("Air Eagle — Operations Control Centre")

db_status = test_connection()
if db_status is True:
    st.success("Database connected")
else:
    st.error(f"Database error: {db_status}")

st.write("Use the sidebar to navigate.")
