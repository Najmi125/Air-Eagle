"""
pages/5_Assistant.py

The OCC assistant's UI — presentation only. Every piece this page
calls already exists and is already tested: services/assistant/
query_parser.py (parse()), services/assistant/reports.py (run_report()),
services/reporting.py (dataset_to_xlsx()/report_filename()). No new
service logic lives here.

SCOPE (operator decision, 2026-08-08): this assistant generates tables
only. It never answers a legality or decision question — the parser's
own refusal layer (query_parser.is_decision_question()) enforces that
before this page ever sees a ReportRequest for that class of question.

THE ONE HARD REQUIREMENT: show the interpretation before the results,
every time, including when the result is empty. The parser can resolve
CONFIDENTLY to the wrong template or wrong parameters (wrong month,
wrong crew member) on an ordinary reporting question — the refusal
layer only catches decision-shaped phrasing, not ordinary misreads. A
wrong table that looks identical to a right one is worse than an error
message. An empty table is exactly when a controller most needs to see
what was understood, since the likelier cause is a misparse than a
genuinely empty period — so the interpretation renders unconditionally
above whatever comes next, never withheld because there's "nothing to
show yet."
"""
import dataclasses

import pandas as pd
import streamlit as st

from services import crew_service, auth_service
from services.display_labels import format_timestamps
from services.assistant import query_parser, reports
from services.reporting import AIR_EAGLE, dataset_to_xlsx, report_filename

st.set_page_config(page_title="OCC Assistant", page_icon="🤖", layout="wide")
app_user = auth_service.require_login()
st.title("OCC Assistant")

st.markdown(
    "Ask a question about crew duties, flight records, document expiry, "
    "utilization, roster coverage, audit history, or ANO-012 regulations. "
    "**This assistant generates tables from records that already exist — "
    "it never answers a legality or decision question** (\"can X fly\", "
    "\"who can replace Y\"); for those, use the Roster page, which runs "
    "the real legality check."
)

EXAMPLE_QUESTIONS = [
    "What did Waqar fly last week?",
    "Show the flight log for July",
    "Whose medical expires in the next 30 days?",
    "How many hours has Waqar flown this month?",
    "Crew roster for next 14 days",
    "Show me all blocked assignments last month",
    "What does D21.1 say about minimum rest?",
]
st.markdown("**Example questions:**\n" + "\n".join(f"- {q}" for q in EXAMPLE_QUESTIONS))

# This is the first use of st.session_state anywhere in this repo, and
# a hard lesson on record ("session_state lost unassigned count on
# refresh -> persist unassigned duties in DB, session state is
# demo-only") could make this LOOK like a repeat of that mistake. It
# isn't: that lesson is about OPERATIONAL data the system is
# responsible for — losing it meant losing information nobody could
# recover. What's held here is a transient UI query result; if it
# vanishes on refresh, the controller retypes the question and nothing
# is lost. Nothing operational depends on it, nothing is the system's
# record of anything. Do not "fix" this by persisting query results to
# the database — that would reintroduce complexity this page has no
# reason to carry.
if "assistant_result" not in st.session_state:
    st.session_state.assistant_result = None
    st.session_state.assistant_question = ""
    # Incremented on every NEWLY SUBMITTED question, used inside the
    # editable-date-range widgets' own key below. A real Streamlit
    # gotcha, confirmed directly: once a widget's key has session
    # state, its `value=` parameter is ignored on later reruns — so
    # without this, asking a second, different-dated question would
    # silently leave the date_input widgets showing the FIRST
    # question's stale dates, which the "changed from result" check
    # further down would then mistake for a manual edit and use to
    # silently overwrite the second question's correctly-parsed dates.
    # A fresh generation number forces fresh widgets (and therefore
    # the real default) on every new question, while edits WITHIN one
    # question's result still persist normally via the same key.
    st.session_state.assistant_generation = 0

# Built once per render, reused for parse()'s own name resolution and
# every later name lookup (ambiguous-crew listing, the interpretation
# line) — one query instead of three. active_only=False deliberately:
# a controller asking about a deactivated crew member's history (e.g.
# an audit question) is a real, legitimate use of crew_duty_history;
# restricting name resolution to active crew would make that fail to
# resolve for no real reason. known_airports is left at parse()'s own
# built-in default; it already covers every real Air Eagle route.
crew_directory = {
    row["crew_id"]: row["name"]
    for _, row in crew_service.get_all_crew(active_only=False).iterrows()
}

with st.form("ask_form"):
    question = st.text_input("Ask a question")
    asked = st.form_submit_button("Ask")

if asked and question.strip():
    st.session_state.assistant_result = query_parser.parse(question, crew_directory=crew_directory)
    st.session_state.assistant_question = question
    st.session_state.assistant_generation += 1

result = st.session_state.assistant_result
raw_question = st.session_state.assistant_question

if result is None:
    st.stop()

if not result.resolved:
    if result.reason == query_parser.DECISION_REDIRECT:
        st.warning(result.reason)
    elif result.reason == "ambiguous crew name":
        st.info("Which crew member did you mean?")
        for token, crew_ids in result.ambiguous_crew.items():
            options = ", ".join(f"{cid} ({crew_directory.get(cid, 'unknown')})" for cid in crew_ids)
            st.write(f"**\"{token}\"** could mean: {options}")
    elif result.reason == "ambiguous between templates":
        st.info("Your question could mean more than one thing. Which did you mean?")
        for name in result.candidates:
            st.write(f"- **{name}**: {query_parser.TEMPLATES[name]['description']}")
    else:
        st.info("I couldn't understand that as one of the questions I can answer. Try one of these:")
        for line in query_parser.describe_capabilities():
            st.write(f"- {line}")
    st.stop()

# ------------------------------------------------------------------
# Editable date range — the shown range can be corrected without
# retyping the whole question. Not offered for "regulation": it has no
# date dimension at all (keyed by D-section only), so editing dates
# there would be inert and confusing.
# ------------------------------------------------------------------
if result.template != "regulation":
    gen = st.session_state.assistant_generation
    col1, col2 = st.columns(2)
    new_from = col1.date_input("From", value=result.date_from, key=f"edit_date_from_{gen}")
    new_to = col2.date_input("To", value=result.date_to, key=f"edit_date_to_{gen}")
    new_from = new_from or None
    new_to = new_to or None
    if (new_from, new_to) != (result.date_from, result.date_to):
        # window_days is tied to the ORIGINAL relative phrasing ("last
        # 28 days") — cleared here since a manually-edited range no
        # longer matches whatever phrase produced it; keeping it would
        # silently compute a rolling peak against a window nobody
        # actually asked for.
        result = dataclasses.replace(result, date_from=new_from, date_to=new_to, window_days=None)
        st.session_state.assistant_result = result
        st.rerun()

# ------------------------------------------------------------------
# Interpretation — always shown, always before the results.
# ------------------------------------------------------------------
parts = [f"**{query_parser.TEMPLATES[result.template]['description']}**"]
if result.crew_ids:
    crew_desc = ", ".join(f"{cid} ({crew_directory.get(cid, 'unknown')})" for cid in result.crew_ids)
    parts.append(f"Crew: {crew_desc}")
if result.role:
    parts.append(f"Role: {result.role}")
if result.date_from or result.date_to:
    date_desc = f"{result.date_from or '…'} – {result.date_to or '…'}"
    if result.window_days:
        date_desc += f" (rolling {result.window_days}-day window)"
    parts.append(date_desc)
else:
    parts.append("Date range: not specified")
if result.status_filter:
    parts.append(f"Status: {result.status_filter}")
if result.origin or result.destination:
    parts.append(f"Route: {result.origin or '?'}–{result.destination or '?'}")
if result.flight_no:
    parts.append(f"Flight: {result.flight_no}")

st.info("Understood as: " + " · ".join(parts))

# ------------------------------------------------------------------
# Results
# ------------------------------------------------------------------
dataset = reports.run_report(result, raw_question)

if dataset.rows:
    # Screen only. The CSV/XLSX downloads below deliberately keep ISO
    # timestamps — see reporting.dataset_to_csv().
    st.dataframe(format_timestamps(pd.DataFrame(dataset.rows, columns=dataset.headers)),
                 width="stretch")
else:
    st.info("No matching records.")

for note in dataset.notes:
    st.warning(note)

if dataset.rows:
    xlsx_bytes = dataset_to_xlsx(dataset)
    filename = report_filename(dataset, AIR_EAGLE, "xlsx")
    st.download_button(
        "Download as Excel",
        data=xlsx_bytes,
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
