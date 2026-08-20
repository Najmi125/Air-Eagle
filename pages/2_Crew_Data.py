"""
pages/2_Crew_Data.py

Collects input, calls services/crew_service.py, displays output.
Does not write SQL directly (Section 17), does not create or alter
the crew table (migrations/001_crew_table.sql owns that exclusively).

Deliberately unstyled — the home page (home.py) got its own branding
pass (2026-08-10/11), this page hasn't; UI work here is parked for later.
"""
import datetime as dt

import pandas as pd
import streamlit as st

from services import crew_service, auth_service

st.set_page_config(page_title="Crew Data", page_icon="👨‍✈️", layout="wide")
app_user = auth_service.require_login()
st.title("Crew Data")

# CPT/FO only, per the operator's 2026-08-02 decision: Air Eagle's
# crew records are CPT and FO — LM/AME are the operator's own
# operational responsibility and are never tracked as crew here (see
# scripts/import_crew_from_xlsx.py's EXCLUDED_ROLES and HANDOVER.md).
# "Other" stays as the escape hatch for a genuinely unanticipated role
# the operator hasn't described yet — not for LM/AME, which have a
# specific, deliberate answer, not an open one.
#
# services/crew_service.py's role handling (ROLE_SYNONYMS, ROLE_PREFIXES,
# _normalize_role()) is untouched — this is a page-level (Air-Eagle-
# specific) restriction on what this form offers, not a platform-wide
# rule. FTLguard itself still fully supports LM/ENGR crew records for
# a future client; this form just doesn't invite creating them here.
ROLE_OPTIONS = ["CPT", "FO", "Other"]


def _expiry_input(label: str, row, field: str):
    """A qualification-expiry date field, marked when it has lapsed.

    ALWAYS EDITABLE. An expired document is precisely the one that
    needs renewing, so the marking is in the label only — never
    `disabled=True`, which would lock the operator out of the exact
    field they came to change.

    The boundary follows the legality gate rather than convention:
    services/assignment_service.py treats `expiry <= duty_date` as
    already expired, i.e. a document is valid strictly BEFORE its own
    expiry date, not through it. So a document expiring today is
    already invalid today, and is marked as such here. Deliberately
    stricter than the everyday reading of an expiry date, and
    deliberately the same rule the roster uses — a field that said
    "valid" while the roster refused the assignment would be worse than
    no marking at all.

    No key=: see the comment on the edit form below for why an explicit
    key would break the crew-switching behaviour these rely on.
    """
    value = row[field]
    expired = value is not None and not pd.isna(value) and value <= dt.date.today()
    return st.date_input(f"{label} ⚠ expired" if expired else label, value=value)


# ================= DISPLAY =================
show_inactive = st.checkbox("Show inactive crew too")
crew_df = crew_service.get_all_crew(active_only=not show_inactive)

st.subheader(f"Crew ({len(crew_df)})")
st.dataframe(crew_df, width="stretch")


# ================= ADD NEW CREW =================
st.subheader("Add crew member")

with st.form("add_crew_form", clear_on_submit=True):
    col1, col2, col3 = st.columns(3)

    with col1:
        name = st.text_input("Name *")
        role_choice = st.selectbox("Role *", ROLE_OPTIONS)
        role_other = st.text_input("Role (if Other)") if role_choice == "Other" else None
        operator_staff_id = st.text_input("Operator Staff ID")
        base = st.text_input("Base")
        nationality = st.text_input("Nationality")
        date_of_birth = st.date_input("Date of Birth", value=None)

    with col2:
        phone = st.text_input("Phone")
        email = st.text_input("Email")
        license_no = st.text_input("License No")
        license_expiry = st.date_input("License Expiry", value=None)
        medical_expiry = st.date_input("Medical Expiry", value=None)

    with col3:
        ir_expiry = st.date_input("IR (Instrument Rating) Expiry", value=None)
        sim_expiry = st.date_input("SIM Expiry", value=None)
        route_check_expiry = st.date_input("Route Check Expiry", value=None)
        sep_expiry = st.date_input("SEP Expiry", value=None)
        crm_expiry = st.date_input("CRM Expiry", value=None)
        dg_expiry = st.date_input("DG Expiry", value=None)

    remarks = st.text_area("Remarks")

    submitted = st.form_submit_button("Add crew member")

    if submitted:
        role_value = role_other if role_choice == "Other" else role_choice
        try:
            new_id = crew_service.add_crew({
                "name": name,
                "role": role_value,
                "operator_staff_id": operator_staff_id or None,
                "base": base or None,
                "nationality": nationality or None,
                "date_of_birth": date_of_birth,
                "phone": phone or None,
                "email": email or None,
                "license_no": license_no or None,
                "license_expiry": license_expiry,
                "medical_expiry": medical_expiry,
                "ir_expiry": ir_expiry,
                "sim_expiry": sim_expiry,
                "route_check_expiry": route_check_expiry,
                "sep_expiry": sep_expiry,
                "crm_expiry": crm_expiry,
                "dg_expiry": dg_expiry,
                "remarks": remarks or None,
            }, app_user=app_user)
            st.success(f"Added {name} as {new_id}")
            st.rerun()
        except ValueError as e:
            st.error(str(e))


# ================= EDIT / DEACTIVATE EXISTING =================
st.subheader("Edit or deactivate existing crew")

if crew_df.empty:
    st.info("No crew records yet.")
else:
    selected_id = st.selectbox("Select crew member", crew_df["crew_id"])
    selected = crew_service.get_crew(selected_id)

    if selected is not None:
        # Deliberately NO key= on any widget in this form.
        #
        # These use value=selected[...] and rely on Streamlit deriving
        # the widget id from its parameters — which INCLUDES value — so
        # selecting a different crew member changes the id and the form
        # correctly re-renders with that person's data. Verified
        # directly (2026-08-20) by driving the real page with two crew
        # members and switching between them.
        #
        # Adding an explicit stable key would BREAK that: a keyed widget
        # ignores value= once it exists, so the form would keep showing
        # the previously selected crew member while writing to the newly
        # selected crew_id. That is the opposite of the fix applied to
        # pages/7_Schedule_Templates.py, where the keys are stable
        # across submissions and had to carry a generation counter. Same
        # underlying mechanism, opposite correct answer — the difference
        # is whether the intended reset is driven by value= or by a key.
        with st.form("edit_crew_form"):
            st.caption(
                f"Editing {selected['name']} ({selected_id}) — "
                f"role {selected['role']}. Renewing a document? Update its "
                f"expiry date below and save."
            )

            col1, col2, col3 = st.columns(3)
            with col1:
                new_phone = st.text_input("Phone", value=selected["phone"] or "")
                new_email = st.text_input("Email", value=selected["email"] or "")
                new_base = st.text_input("Base", value=selected["base"] or "")
                new_date_of_birth = st.date_input(
                    "Date of Birth", value=selected["date_of_birth"])

            with col2:
                new_license_expiry = _expiry_input("License Expiry", selected, "license_expiry")
                new_medical_expiry = _expiry_input("Medical Expiry", selected, "medical_expiry")
                new_ir_expiry = _expiry_input("IR (Instrument Rating) Expiry", selected, "ir_expiry")
                new_sim_expiry = _expiry_input("SIM Expiry", selected, "sim_expiry")

            with col3:
                new_route_check_expiry = _expiry_input("Route Check Expiry", selected, "route_check_expiry")
                new_sep_expiry = _expiry_input("SEP Expiry", selected, "sep_expiry")
                new_crm_expiry = _expiry_input("CRM Expiry", selected, "crm_expiry")
                new_dg_expiry = _expiry_input("DG Expiry", selected, "dg_expiry")

            new_remarks = st.text_area("Remarks", value=selected["remarks"] or "")

            col_update, col_deactivate = st.columns(2)
            with col_update:
                update_submitted = st.form_submit_button("Save changes")
            with col_deactivate:
                deactivate_submitted = st.form_submit_button("Deactivate this crew member")

            if update_submitted:
                crew_service.update_crew(selected_id, {
                    "phone": new_phone or None,
                    "email": new_email or None,
                    "base": new_base or None,
                    "remarks": new_remarks or None,
                    "date_of_birth": new_date_of_birth,
                    "license_expiry": new_license_expiry,
                    "medical_expiry": new_medical_expiry,
                    "ir_expiry": new_ir_expiry,
                    "sim_expiry": new_sim_expiry,
                    "route_check_expiry": new_route_check_expiry,
                    "sep_expiry": new_sep_expiry,
                    "crm_expiry": new_crm_expiry,
                    "dg_expiry": new_dg_expiry,
                }, app_user=app_user)
                st.success(f"Updated {selected_id}")
                st.rerun()

            if deactivate_submitted:
                crew_service.deactivate_crew(
                    selected_id, reason="Deactivated via Crew Data page", app_user=app_user)
                st.success(f"Deactivated {selected_id}")
                st.rerun()
