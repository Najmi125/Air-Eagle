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

from services import auth_service
from services import rotation_template_service as rts

st.set_page_config(page_title="Schedule Templates", page_icon="📋", layout="wide")
app_user = auth_service.require_login()
st.title("Schedule Templates")

WEEKDAY_OPTIONS = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5), ("Sat", 6), ("Sun", 7)]
WEEKDAY_LABELS = {n: label for label, n in WEEKDAY_OPTIONS}
LEG_ROW_COUNT = 5
LEG_COLUMN_WIDTHS = [1.3, 1, 1, 1, 1, 0.9, 1.4]

# Bumped after every successful create/version save, and threaded into
# every leg widget key below. A Streamlit widget's value= is only
# honored the FIRST time a given key renders, so a fixed key means the
# NEXT template's form silently opens carrying the PREVIOUS one's
# values wherever the controller doesn't overwrite every field.
#
# That is not hypothetical: on 2026-08-19 an operator created EPE-786-787
# (2 legs) and then EPE-802-804-805 (3 legs) without reloading, and the
# second template saved leg 2 with the first's route and times
# (LHE->KHI 22:00-23:45 domestic instead of LHE->DWC 04:30-08:00
# international). Recovering from it required manually disabling the
# database immutability triggers — see migrations/019.
#
# Same fix, same reason, as st.session_state.assistant_generation on
# pages/5_Assistant.py. Both forms share this one counter: they are
# never on screen for the same submission, and one counter means there
# is no second place to forget to bump.
if "template_form_generation" not in st.session_state:
    st.session_state.template_form_generation = 0


def _parse_hhmm(raw: str):
    """'0905'/'09:05' -> dt.time(9, 5). Returns (time, None) or
    (None, message).

    Text rather than st.time_input, at the operator's request
    (2026-08-19): a dropdown is slow for four times per rotation, and
    controllers already write times as 0905. The empty string is a real
    value here and NOT an error — it is what makes an untouched leg row
    distinguishable from a filled one, which st.time_input could never
    express because it always yields a value (00:00 by default). That
    distinction is what fixes the silently-skipped leg below.
    """
    text_value = (raw or "").strip().replace(":", "")
    if not text_value:
        return None, None
    if not (text_value.isdigit() and len(text_value) == 4):
        return None, f"{raw.strip()!r} is not a valid time — use 24-hour HHMM, e.g. 0905"
    hours, minutes = int(text_value[:2]), int(text_value[2:])
    if hours > 23 or minutes > 59:
        return None, f"{raw.strip()!r} is not a valid 24-hour time — hours 00-23, minutes 00-59"
    return dt.time(hours, minutes), None


def _format_hhmm(value) -> str:
    """A stored dt.time back to the HHMM the controller typed, for
    pre-filling the 'create new version' form."""
    return value.strftime("%H%M") if value else ""


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
        dep_time = cols[3].text_input("Dep (UTC HHMM)", value=_format_hhmm(d.get("dep_time")), key=f"{key_prefix}_dep_{i}")
        arr_time = cols[4].text_input("Arr (UTC HHMM)", value=_format_hhmm(d.get("arr_time")), key=f"{key_prefix}_arr_{i}")
        day_offset = cols[5].number_input("Day offset", min_value=0, value=int(d.get("day_offset", 0)), step=1, key=f"{key_prefix}_offset_{i}")
        domestic_index = 0 if d.get("domestic", True) else 1
        domestic = cols[6].radio("Domestic/Intl", ["Domestic", "International"], index=domestic_index,
                                  key=f"{key_prefix}_domestic_{i}", horizontal=True)
        rows.append({
            "flight_no": flight_no.strip(), "origin": origin.strip(),
            "destination": destination.strip(),
            "dep_time_raw": dep_time, "arr_time_raw": arr_time,
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
    "arr_time 19:00 is not after dep_time 20:00").

    "Filled" counts the TIME fields too, as of 2026-08-19. It used to
    mean flight_no/origin/destination only, so a row with times entered
    but empty text fields read as blank and was skipped in silence —
    the leg simply did not appear in the saved template, with no error
    and nothing to see. That is what made the widget-key bug in the
    same release so hard to spot: leg 3 of a 3-leg rotation had its
    times typed and its text fields left stale-empty, so it vanished
    rather than complaining. A partially-filled row is now always an
    error naming the row. This only became expressible once the time
    fields became text (_parse_hhmm) — st.time_input always yields a
    value, so a row could never be "empty" in the time columns."""
    legs = []
    for i, row in enumerate(rows, start=1):
        dep_raw, arr_raw = row["dep_time_raw"], row["arr_time_raw"]
        filled = bool(row["flight_no"] or row["origin"] or row["destination"]
                      or dep_raw.strip() or arr_raw.strip())
        if not filled:
            continue

        dep_time, dep_error = _parse_hhmm(dep_raw)
        if dep_error:
            return None, f"Leg {i} departure time: {dep_error}"
        arr_time, arr_error = _parse_hhmm(arr_raw)
        if arr_error:
            return None, f"Leg {i} arrival time: {arr_error}"

        missing = [name for name, value in (
            ("flight number", row["flight_no"]), ("origin", row["origin"]),
            ("destination", row["destination"]), ("departure time", dep_time),
            ("arrival time", arr_time)) if not value]
        if missing:
            return None, (
                f"Leg {i} is partially filled — missing {', '.join(missing)}. "
                f"Complete the row, or clear it entirely to leave it unused."
            )

        if arr_time <= dep_time:
            return None, f"Leg {i}: arrival time must be after departure time."

        legs.append({
            "flight_no": row["flight_no"], "origin": row["origin"],
            "destination": row["destination"], "dep_time": dep_time,
            "arr_time": arr_time, "day_offset": row["day_offset"],
            "domestic": row["domestic"],
        })
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

            # Delete, only for a template that has produced nothing —
            # undoing a mistaken creation, not a retirement mechanism.
            # A rotation that has run and should stop is closed with
            # effective_until; one being replaced is superseded by a new
            # version. This covers only the remaining case: a template
            # referenced by nothing at all, where there is no history to
            # protect. See migrations/019 for why the rule lives in the
            # database trigger rather than here.
            #
            # The control is shown DISABLED with the specific reason
            # rather than hidden, so "why can't I remove this?" is
            # answered in place instead of looking like a missing
            # feature.
            if len(versions) == 1:
                deletability = rts.get_template_deletability(int(versions.iloc[0]["id"]))
            else:
                deletability = {
                    "deletable": False,
                    "reason": (
                        f"{code} has {len(versions)} versions. Only a sole-version template "
                        f"can be deleted — removing one version of a chain would leave its "
                        f"predecessor permanently closed, since effective_until can only be "
                        f"closed once. Supersede it with a new version instead."
                    ),
                }
            if not deletability["deletable"]:
                st.caption(f"Cannot delete: {deletability['reason']}")
            if st.button("Delete template", key=f"delete_{code}",
                         disabled=not deletability["deletable"]):
                try:
                    rts.delete_template(int(versions.iloc[0]["id"]), app_user=app_user)
                except Exception as e:
                    st.error(f"Could not delete {code}: {e}")
                else:
                    st.success(f"Template {code} deleted — it had produced no rotations.")
                    st.rerun()

st.subheader("Create a new template")
with st.form("create_template_form"):
    # Generation-keyed like the leg rows, and for the same reason: these
    # fields were previously unkeyed, which does NOT mean stateless —
    # Streamlit auto-keys them, so after a save the form still held the
    # previous template's code, description and weekdays. The reported
    # corruption was in the legs, but "wherever the controller didn't
    # overwrite every field" applies just as much here; a description or
    # a day-of-week silently carried into the next template is the same
    # defect with a quieter symptom.
    #
    # Explicit keys also disambiguate these from the "create new
    # version" form below, which renders widgets with identical labels
    # ("Description", "Days of week *"). Label-based lookup cannot tell
    # them apart once a template exists.
    ct_prefix = f"ct_{st.session_state.template_form_generation}"

    ct_rotation_code = st.text_input("Rotation code *", key=f"{ct_prefix}_rotation_code")
    ct_description = st.text_input("Description", key=f"{ct_prefix}_description")
    ct_weekday_labels = st.multiselect(
        "Days of week *", [label for label, _ in WEEKDAY_OPTIONS], key=f"{ct_prefix}_days")
    ct_days_of_week = [n for label, n in WEEKDAY_OPTIONS if label in ct_weekday_labels]
    ct_effective_from = st.date_input(
        "Effective from *", value=dt.date.today(), key=f"{ct_prefix}_effective_from")
    ct_open_ended = st.checkbox(
        "Open-ended (no end date)", value=True, key=f"{ct_prefix}_open_ended")
    ct_effective_until = None if ct_open_ended else st.date_input(
        "Effective until", value=dt.date.today(), key=f"{ct_prefix}_effective_until")
    ct_meal_provided = st.checkbox("Meal provided", value=True, key=f"{ct_prefix}_meal")
    ct_snack_provided = st.checkbox("Snack provided", value=True, key=f"{ct_prefix}_snack")
    st.markdown("**Legs**")
    ct_leg_rows = _render_leg_rows(ct_prefix)

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
                        app_user=app_user,
                    )
                except ValueError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Could not create template: {e}")
                else:
                    st.success(f"Template {ct_rotation_code.strip()} v1 created with {len(ct_legs)} leg(s).")
                    # Retire this form's widget keys so the next
                    # template starts genuinely blank rather than
                    # inheriting whatever was just saved.
                    st.session_state.template_form_generation += 1
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

        # One prefix for EVERY widget in this section, so they can't
        # drift into being keyed differently from each other. It carries
        # both the rotation_code (switching codes must honor THAT code's
        # own defaults) and the form generation (after a save, every
        # default in here changes, because cv_current is now the version
        # that was just created — meal/snack/days/legs all read from it).
        # Keying on the code alone left these showing the previous
        # submission's values instead of the newly-current version's.
        #
        # The line drawn here: a widget whose DEFAULT comes from the
        # template (days, meal, snack, legs) is regenerated, because
        # that default changes the moment a version is saved. Widgets
        # holding the controller's own transient choice are not —
        # "cv_code" must persist or the section would jump back to the
        # first rotation on every rerun, and "cv_effective_from"
        # defaults to today() rather than to anything template-derived,
        # so it has no stale default to carry.
        cv_prefix = f"cv_{cv_code}_{st.session_state.template_form_generation}"

        cv_description = st.text_input("Description", key=f"{cv_prefix}_description")
        cv_default_days = [WEEKDAY_LABELS[d] for d in cv_current["days_of_week"] if d in WEEKDAY_LABELS]
        cv_weekday_labels = st.multiselect(
            "Days of week *", [label for label, _ in WEEKDAY_OPTIONS],
            default=cv_default_days, key=f"{cv_prefix}_days",
        )
        cv_days_of_week = [n for label, n in WEEKDAY_OPTIONS if label in cv_weekday_labels]
        cv_open_ended = st.checkbox("Open-ended (no end date)", value=True, key=f"{cv_prefix}_open_ended")
        cv_effective_until = None if cv_open_ended else st.date_input(
            "Effective until", value=dt.date.today(), key=f"{cv_prefix}_effective_until")
        cv_meal_provided = st.checkbox("Meal provided", value=bool(cv_current["meal_provided"]), key=f"{cv_prefix}_meal")
        cv_snack_provided = st.checkbox("Snack provided", value=bool(cv_current["snack_provided"]), key=f"{cv_prefix}_snack")

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
        #
        # The generation is here for a DIFFERENT case the cv_code alone
        # never covered (2026-08-19): versioning the SAME code twice in
        # one session. The prefix would be identical both times, so the
        # second form opened carrying the first submission's legs
        # instead of the newly-current version's — the same class of bug
        # as the create form's, just needing two saves of one code to
        # show itself rather than two different templates.
        cv_leg_rows = _render_leg_rows(cv_prefix, defaults=cv_default_legs)

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
                        app_user=app_user,
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
                    # Same reason as the create form: the next render of
                    # this section must honor the NEW current version's
                    # legs as defaults, which stale widget keys would
                    # silently ignore.
                    st.session_state.template_form_generation += 1
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
            breakdown = {
                code: len(rts.expand_and_persist(code, expand_from, expand_to, app_user=app_user))
                for code in codes_to_expand
            }
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
                    flight_ids = rts.approve_instance(iid, app_user=app_user)
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
                    rts.reject_instance(iid, reason=reject_reason.strip(), app_user=app_user)
                except ValueError as e:
                    failed.append((iid, str(e)))
                else:
                    rejected.append(iid)
            if rejected:
                st.success(f"{len(rejected)} rotation(s) rejected.")
            for iid, msg in failed:
                st.error(f"Instance {iid}: {msg}")
            st.rerun()
