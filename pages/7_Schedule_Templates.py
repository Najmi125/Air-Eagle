"""
pages/7_Schedule_Templates.py

Phase 7's second and last UI — presentation only over services/
rotation_template_service.py. Three workflows in order: view/create
templates, expand a window into drafts, review (approve/reject)
drafts. Once this exists, a controller can go from "no schedule
exists" to "published roster" (pages/6_Roster_Generation.py) entirely
through the UI.

Bulk review deliberately uses one st.checkbox per row, not
st.data_editor — confirmed directly that st.data_editor has no
AppTest accessor at all (no way to read an edited selection back in a
test), so a checkbox grid is the only design here that's actually
testable, matching every other page in this repo.
"""
import datetime as dt

import pandas as pd
import streamlit as st

from services import rotation_template_service as rts

st.set_page_config(page_title="Schedule Templates", page_icon="📋", layout="wide")
st.title("Schedule Templates")

WEEKDAY_OPTIONS = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5), ("Sat", 6), ("Sun", 7)]
WEEKDAY_LABELS = {n: label for label, n in WEEKDAY_OPTIONS}
LEG_ROW_COUNT = 5
LEG_COLUMN_WIDTHS = [1.3, 1, 1, 1, 1, 0.9, 1.4]


def _render_leg_rows(key_prefix: str, defaults: list | None = None) -> list[dict]:
    """Fixed 5 blank rows, not dynamic add/remove — real rotations are
    2-3 legs, so 5 covers every real case with margin, and a form with
    dynamic add/remove needs session_state + reruns inside what should
    be one atomic submit for no real benefit at this leg count. A row
    is "filled" if any of flight_no/origin/destination has content;
    filled/blank detection and validation happen in
    _collect_and_validate_legs(), not here.

    defaults: optional list of leg dicts (same shape get_template_legs()
    returns) to pre-fill, oldest leg_order first — used by the "create
    new version" section so an unchanged route doesn't need retyping.
    key_prefix must vary with whatever the defaults depend on (e.g. the
    selected rotation_code) — a Streamlit widget's value=/default=/index=
    is only honored the FIRST time a given key renders; reusing a fixed
    key with different defaults would silently keep showing stale
    values, the same widget-key staleness gotcha found and fixed on
    pages/5_Assistant.py and pages/6_Roster_Generation.py.
    """
    defaults = defaults or []
    rows = []
    for i in range(LEG_ROW_COUNT):
        d = defaults[i] if i < len(defaults) else {}
        st.markdown(f"**Leg {i + 1}**")
        cols = st.columns(LEG_COLUMN_WIDTHS)
        flight_no = cols[0].text_input("Flight no.", value=d.get("flight_no", "") or "", key=f"{key_prefix}_flightno_{i}")
        origin = cols[1].text_input("Origin", value=d.get("origin", "") or "", key=f"{key_prefix}_origin_{i}")
        destination = cols[2].text_input("Destination", value=d.get("destination", "") or "", key=f"{key_prefix}_dest_{i}")
        dep_time = cols[3].time_input("Dep time", value=d.get("dep_time") or dt.time(0, 0), key=f"{key_prefix}_dep_{i}")
        arr_time = cols[4].time_input("Arr time", value=d.get("arr_time") or dt.time(0, 0), key=f"{key_prefix}_arr_{i}")
        day_offset = cols[5].number_input("Day offset", min_value=0, value=int(d.get("day_offset", 0)), step=1, key=f"{key_prefix}_offset_{i}")
        domestic_index = 0 if d.get("domestic", True) else 1
        domestic = cols[6].radio("Domestic/Intl", ["Domestic", "International"], index=domestic_index,
                                  key=f"{key_prefix}_domestic_{i}", horizontal=True)
        rows.append({
            "flight_no": flight_no.strip(), "origin": origin.strip(),
            "destination": destination.strip(), "dep_time": dep_time, "arr_time": arr_time,
            "day_offset": int(day_offset), "domestic": domestic == "Domestic",
        })
    return rows


def _collect_and_validate_legs(rows: list[dict]):
    """rows from _render_leg_rows(). Returns (legs, error) — legs is
    None on error, otherwise a validated list with sequential leg_order
    assigned in row order (gaps in which of the 5 rows were used don't
    matter). Goes one step past rotation_template_service._validate_legs()
    (which only enforces flight_no) by also checking arr_time is after
    dep_time here, at creation time — create_template()/create_new_
    version() don't check this themselves, only expand_template() does,
    later, at expand time. Without this, a controller could create a
    broken template and only discover it when expansion fails, possibly
    days later (confirmed directly: a template with dep 20:00/arr 19:00
    is accepted at creation and only fails at expand_and_persist() with
    "arr_time 19:00 is not after dep_time 20:00")."""
    legs = []
    for i, row in enumerate(rows, start=1):
        filled = bool(row["flight_no"] or row["origin"] or row["destination"])
        if not filled:
            continue
        if not (row["flight_no"] and row["origin"] and row["destination"]):
            return None, f"Leg {i}: flight number, origin, and destination are all required."
        if row["arr_time"] <= row["dep_time"]:
            return None, f"Leg {i}: arrival time must be after departure time."
        legs.append(row)
    if not legs:
        return None, "At least one leg is required."
    for order, leg in enumerate(legs, start=1):
        leg["leg_order"] = order
    return legs, None


def _current_version_row(rotation_code: str):
    """The open (effective_until IS NULL) version of rotation_code, or
    None if every version has been closed — shouldn't normally happen
    (create_new_version() always leaves the newest version open), kept
    as a real None-check rather than assumed."""
    versions = rts.get_versions(rotation_code)
    open_versions = versions[versions["effective_until"].isna()]
    if open_versions.empty:
        return None
    return open_versions.iloc[-1]


LEG_DISPLAY_COLUMNS = ["leg_order", "flight_no", "origin", "destination", "dep_time", "arr_time", "day_offset", "domestic"]


# ==================================================================
# 1. View and create templates
# ==================================================================
st.header("1. View and create templates")

st.subheader("Existing rotation templates")
rotation_codes = rts.get_all_rotation_codes()
if not rotation_codes:
    st.info("No rotation templates yet — create one below.")
else:
    for code in rotation_codes:
        with st.expander(code):
            versions = rts.get_versions(code)
            st.dataframe(
                versions[["version", "effective_from", "effective_until", "description"]],
                width="stretch", hide_index=True,
            )
            current = _current_version_row(code)
            if current is not None:
                st.caption(f"Current version: v{int(current['version'])}")
                legs = rts.get_template_legs(int(current["id"]))
                st.dataframe(legs[LEG_DISPLAY_COLUMNS], width="stretch", hide_index=True)
            if st.checkbox("Show all versions' legs", key=f"show_all_legs_{code}"):
                for _, v in versions.iterrows():
                    st.markdown(f"**v{int(v['version'])}**")
                    vlegs = rts.get_template_legs(int(v["id"]))
                    st.dataframe(vlegs[LEG_DISPLAY_COLUMNS], width="stretch", hide_index=True)

st.subheader("Create a new template")
with st.form("create_template_form"):
    ct_rotation_code = st.text_input("Rotation code *")
    ct_description = st.text_input("Description")
    ct_weekday_labels = st.multiselect("Days of week *", [label for label, _ in WEEKDAY_OPTIONS])
    ct_days_of_week = [n for label, n in WEEKDAY_OPTIONS if label in ct_weekday_labels]
    ct_effective_from = st.date_input("Effective from *", value=dt.date.today())
    ct_open_ended = st.checkbox("Open-ended (no end date)", value=True)
    ct_effective_until = None if ct_open_ended else st.date_input("Effective until", value=dt.date.today())
    ct_meal_provided = st.checkbox("Meal provided", value=True)
    ct_snack_provided = st.checkbox("Snack provided", value=True)
    st.markdown("**Legs**")
    ct_leg_rows = _render_leg_rows("ct")

    ct_submitted = st.form_submit_button("Create template")

    if ct_submitted:
        if not ct_rotation_code.strip():
            st.error("Rotation code is required.")
        elif not ct_days_of_week:
            st.error("At least one day of week is required.")
        elif ct_rotation_code.strip() in rts.get_all_rotation_codes():
            st.error(
                f"A template with rotation_code {ct_rotation_code.strip()!r} already "
                f"exists — use 'Create a new version' below instead."
            )
        else:
            ct_legs, ct_leg_error = _collect_and_validate_legs(ct_leg_rows)
            if ct_leg_error:
                st.error(ct_leg_error)
            else:
                try:
                    rts.create_template(
                        rotation_code=ct_rotation_code.strip(), days_of_week=ct_days_of_week,
                        legs=ct_legs, effective_from=ct_effective_from,
                        meal_provided=ct_meal_provided, snack_provided=ct_snack_provided,
                        description=ct_description.strip() or None,
                        effective_until=ct_effective_until,
                    )
                except ValueError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Could not create template: {e}")
                else:
                    st.success(f"Template {ct_rotation_code.strip()} v1 created with {len(ct_legs)} leg(s).")
                    st.rerun()

st.subheader("Create a new version")
if not rotation_codes:
    st.info("No existing templates to version yet.")
else:
    cv_code = st.selectbox("Rotation code", rotation_codes, key="cv_code")
    cv_current = _current_version_row(cv_code)
    if cv_current is None:
        st.warning(f"{cv_code} has no open version — cannot create a new version.")
    else:
        cv_effective_from = st.date_input("New version effective from *", value=dt.date.today(), key="cv_effective_from")
        cv_day_before = cv_effective_from - dt.timedelta(days=1)
        # THE least-obvious behaviour on this page: create_new_version()
        # closes the CURRENT version's effective_until to the day before
        # the new version starts. Shown live, before the controller ever
        # confirms — this date picker is deliberately outside any form so
        # the preview below updates on every change.
        st.info(f"This will end version {int(cv_current['version'])} (currently open-ended) on {cv_day_before}.")

        cv_description = st.text_input("Description", key="cv_description")
        cv_default_days = [WEEKDAY_LABELS[d] for d in cv_current["days_of_week"] if d in WEEKDAY_LABELS]
        cv_weekday_labels = st.multiselect(
            "Days of week *", [label for label, _ in WEEKDAY_OPTIONS],
            default=cv_default_days, key=f"cv_days_{cv_code}",
        )
        cv_days_of_week = [n for label, n in WEEKDAY_OPTIONS if label in cv_weekday_labels]
        cv_open_ended = st.checkbox("Open-ended (no end date)", value=True, key=f"cv_open_ended_{cv_code}")
        cv_effective_until = None if cv_open_ended else st.date_input(
            "Effective until", value=dt.date.today(), key=f"cv_effective_until_{cv_code}")
        cv_meal_provided = st.checkbox("Meal provided", value=bool(cv_current["meal_provided"]), key=f"cv_meal_{cv_code}")
        cv_snack_provided = st.checkbox("Snack provided", value=bool(cv_current["snack_provided"]), key=f"cv_snack_{cv_code}")

        st.markdown("**Legs**")
        cv_default_legs = rts.get_template_legs(int(cv_current["id"])).to_dict("records")
        # key_prefix includes cv_code so switching rotation codes always
        # gets fresh widgets honoring THAT code's own current legs as
        # defaults (the staleness gotcha described in _render_leg_rows'
        # own docstring). Returning to a code already visited this
        # session shows whatever was last typed for it, not a fresh
        # reset back to the template's defaults — a deliberate, minor,
        # non-destructive quirk (preserving in-progress edits), not
        # fixed further.
        cv_leg_rows = _render_leg_rows(f"cv_{cv_code}", defaults=cv_default_legs)

        if st.button("Create new version"):
            cv_legs, cv_leg_error = _collect_and_validate_legs(cv_leg_rows)
            if not cv_days_of_week:
                st.error("At least one day of week is required.")
            elif cv_day_before < cv_current["effective_from"]:
                st.error(
                    f"effective_from {cv_effective_from} leaves no room to close "
                    f"version {int(cv_current['version'])} (its own effective_from "
                    f"is {cv_current['effective_from']})."
                )
            elif cv_leg_error:
                st.error(cv_leg_error)
            else:
                try:
                    rts.create_new_version(
                        rotation_code=cv_code, days_of_week=cv_days_of_week, legs=cv_legs,
                        effective_from=cv_effective_from, meal_provided=cv_meal_provided,
                        snack_provided=cv_snack_provided, description=cv_description.strip() or None,
                        effective_until=cv_effective_until,
                    )
                except ValueError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Could not create new version: {e}")
                else:
                    st.success(
                        f"New version created for {cv_code} — version "
                        f"{int(cv_current['version'])} now ends {cv_day_before}."
                    )
                    st.rerun()

st.divider()

# ==================================================================
# 2. Expand a window into drafts
# ==================================================================
st.header("2. Expand a window into drafts")

rotation_codes = rts.get_all_rotation_codes()  # re-fetch: workflow 1 above may have just added one
if not rotation_codes:
    st.info("No rotation templates yet — create one above before expanding.")
else:
    expand_choice = st.selectbox("Rotation code", ["All rotation codes"] + rotation_codes, key="expand_code")
    exp_col1, exp_col2 = st.columns(2)
    expand_from = exp_col1.date_input("From", value=dt.date.today(), key="expand_from")
    expand_to = exp_col2.date_input("To", value=dt.date.today() + dt.timedelta(days=27), key="expand_to")

    st.caption(
        "Expanding only fills gaps — a date that already has a draft or "
        "approved instance is skipped, never re-created or altered. Safe to run again."
    )

    if expand_from > expand_to:
        st.error("'From' must not be after 'To'.")
    elif st.button("Expand window"):
        codes_to_expand = rotation_codes if expand_choice == "All rotation codes" else [expand_choice]
        with st.spinner("Expanding..."):
            breakdown = {code: len(rts.expand_and_persist(code, expand_from, expand_to)) for code in codes_to_expand}
        total = sum(breakdown.values())
        if total:
            st.success(f"{total} new draft instance(s) created.")
            if len(codes_to_expand) > 1:
                st.dataframe(
                    pd.DataFrame([{"Rotation": k, "Created": v} for k, v in breakdown.items()]),
                    width="stretch", hide_index=True,
                )
        else:
            st.info("No new instances — every date in this window already has one.")

st.divider()

# ==================================================================
# 3. Review drafts
# ==================================================================
st.header("3. Review drafts")

review_filter_code = st.selectbox("Filter by rotation code", ["All"] + rts.get_all_rotation_codes(), key="review_filter_code")
drafts = rts.get_instances(status="DRAFT")
if review_filter_code != "All":
    drafts = drafts[drafts["rotation_code"] == review_filter_code]

if drafts.empty:
    st.info("No drafts to review.")
else:
    visible_ids = [int(i) for i in drafts["id"].tolist()]

    # Apply any pending select-all/clear BEFORE the checkboxes below are
    # instantiated — st.session_state[key] = value raises once a widget
    # with that key already exists THIS run, so a button click can only
    # take effect on the FOLLOWING run, applied here at the top before
    # anything with these keys renders. Confirmed directly via AppTest.
    pending = st.session_state.get("_review_pending_action")
    if pending:
        value = pending == "select_all"
        for iid in visible_ids:
            st.session_state[f"select_{iid}"] = value
        st.session_state["_review_pending_action"] = None

    sel_col1, sel_col2 = st.columns(2)
    if sel_col1.button("Select all visible"):
        st.session_state["_review_pending_action"] = "select_all"
        st.rerun()
    if sel_col2.button("Clear selection"):
        st.session_state["_review_pending_action"] = "clear"
        st.rerun()

    row_widths = [0.5, 0.7, 1.3, 1, 0.6, 2.5, 1.2, 1.2]
    header_cols = st.columns(row_widths)
    for col, label in zip(header_cols, ["", "ID", "Rotation", "Date", "Ver", "Route (flights)", "Report", "Debrief"]):
        col.markdown(f"**{label}**")

    for _, draft in drafts.iterrows():
        instance_id = int(draft["id"])
        legs = rts.get_instance_legs(instance_id)
        flights = ", ".join(legs["flight_no"].fillna("").tolist())
        route = " -> ".join([legs.iloc[0]["origin"]] + legs["destination"].tolist())
        report = legs.iloc[0]["dep_time_planned"]
        debrief = legs.iloc[-1]["arr_time_planned"]

        cols = st.columns(row_widths)
        cols[0].checkbox("select", key=f"select_{instance_id}", label_visibility="collapsed")
        cols[1].write(instance_id)
        cols[2].write(draft["rotation_code"])
        cols[3].write(draft["rotation_date"])
        cols[4].write(int(draft["version"]))
        cols[5].write(f"{route} ({flights})")
        cols[6].write(report)
        cols[7].write(debrief)

    # Filtered against the currently visible id list, never scanned from
    # all session_state keys — a selection made under one rotation_code
    # filter must not get swept into an action taken after switching to
    # a different filter.
    selected_ids = [iid for iid in visible_ids if st.session_state.get(f"select_{iid}", False)]
    st.caption(f"{len(selected_ids)} of {len(visible_ids)} selected.")

    approve_col, reject_col = st.columns(2)

    with approve_col:
        st.markdown("**Approve**")
        st.caption("Promotes every leg of each approved instance into a real flights row.")
        if st.button("Approve selected", disabled=not selected_ids):
            approved, failed, total_flights = [], [], 0
            for iid in selected_ids:
                try:
                    flight_ids = rts.approve_instance(iid)
                except ValueError as e:
                    failed.append((iid, str(e)))
                else:
                    approved.append(iid)
                    total_flights += len(flight_ids)
            if approved:
                st.success(f"{len(approved)} rotation(s) approved — {total_flights} flight(s) created.")
            for iid, msg in failed:
                st.error(f"Instance {iid}: {msg}")
            st.rerun()

    with reject_col:
        st.markdown("**Reject**")
        reject_reason = st.text_input("Reason for rejection *", key="review_reject_reason")
        if st.button("Reject selected", disabled=not (selected_ids and reject_reason.strip())):
            rejected, failed = [], []
            for iid in selected_ids:
                try:
                    rts.reject_instance(iid, reason=reject_reason.strip())
                except ValueError as e:
                    failed.append((iid, str(e)))
                else:
                    rejected.append(iid)
            if rejected:
                st.success(f"{len(rejected)} rotation(s) rejected.")
            for iid, msg in failed:
                st.error(f"Instance {iid}: {msg}")
            st.rerun()
