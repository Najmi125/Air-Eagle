"""
tests/test_auth_coverage.py

Structural guards for the two failure modes the auth/attribution work
can't detect by looking at any single file, because in both cases the
broken state looks exactly like the working state:

1. A page that never calls require_login(). Eight files each need one
   call; the failure mode is forgetting one, and an unprotected page
   renders identically to a protected one — nobody finds it until
   somebody navigates straight to it. Same reasoning as
   test_check_reachability.py: enumerate the real tree and assert the
   property, so a NEW page added later can't quietly skip the gate.

2. A call site that drops app_user. This produces a NULL
   audit_log.app_user on a real audit record — precisely the
   deficiency this whole change exists to fix (see
   migrations/018_users.sql), and invisible unless someone thinks to
   query for it months later.

Unlike test_check_reachability.py, which builds fake repos under
tmp_path because it's testing the CHECKER's logic, these scan the real
project tree — the repo's actual state IS the thing under test.

Both static tests parse with ast rather than grepping, because the
threading in this codebase is a mix of keyword (app_user=app_user) and
positional (_write_pair_rows(..., app_user)) passing. A keyword-only
grep reports six false failures on the positional call sites in
assignment_service.py and roster_generator_service.py; a substring grep
for "app_user" reports false passes on any line that merely mentions
it. Only resolving each callee's signature distinguishes the two.

The runtime companion test at the bottom proves the mechanism actually
reaches the database, but needs TEST_DATABASE_URL and skips without it.
The static tests above it need no database and therefore run
everywhere, including environments where the DB-backed suite skips.
"""
import ast
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
from sqlalchemy import text
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent

GATE_FUNCTION = "require_login"


def _page_files():
    """Every file that Streamlit will exec as a page: pages/*.py plus
    home.py. Globbed, never hardcoded — a hardcoded list would pass
    forever the moment someone adds page 8 and forgets to update it,
    which is the exact class of bug this file exists to prevent."""
    return sorted(ROOT.glob("pages/*.py")) + [ROOT / "home.py"]


def _parse(path: Path) -> ast.Module:
    # encoding="utf-8" explicitly: every page here contains em dashes
    # and emoji, and read_text() defaults to the OS locale codec
    # (cp1252 on Windows). test_check_reachability.py documents this
    # exact failure silently disabling a guardrail once already.
    return ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node: ast.Call):
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def _app_user_accepting_functions():
    """{function name: index of app_user among positional params, or
    None if keyword-only}. Module-level defs in services/ only."""
    accepts = {}
    for f in sorted(ROOT.glob("services/**/*.py")):
        for node in _parse(f).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = [a.arg for a in node.args.args]
            kwonly = [a.arg for a in node.args.kwonlyargs]
            if "app_user" in positional:
                accepts[node.name] = positional.index("app_user")
            elif "app_user" in kwonly:
                accepts[node.name] = None
    return accepts


def _supplies_app_user(node: ast.Call, positional_index) -> bool:
    """True if this call passes app_user by keyword OR positionally."""
    if any(k.arg == "app_user" for k in node.keywords):
        return True
    if any(k.arg is None for k in node.keywords):
        return True  # **kwargs forwarding — can't prove a drop
    if positional_index is not None and len(node.args) > positional_index:
        return True
    return False


# ------------------------------------------------------------------
# 1. Every page is gated
# ------------------------------------------------------------------

def test_every_page_calls_require_login():
    """The gate has to be in each page file, not only app.py's router:
    AppTest.from_file() execs a page script directly and bypasses
    st.navigation() entirely, so a router-only check leaves every page
    unprotected under test — and in reality too, since the router is
    the normal navigation path, not an enforcement boundary."""
    ungated = []
    for path in _page_files():
        called = {_call_name(n) for n in ast.walk(_parse(path)) if isinstance(n, ast.Call)}
        if GATE_FUNCTION not in called:
            ungated.append(path.relative_to(ROOT).as_posix())

    assert ungated == [], (
        "These page files never call %s() — they are reachable without "
        "logging in:\n  %s" % (GATE_FUNCTION, "\n  ".join(ungated))
    )


def test_require_login_is_called_at_module_level_in_every_page():
    """A gate nested inside a function or an `if` may never execute.
    It has to be a top-level statement so it runs every time Streamlit
    re-execs the script — which is on every interaction."""
    not_top_level = []
    for path in _page_files():
        top_level_calls = set()
        for stmt in _parse(path).body:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Call) and _call_name(n) == GATE_FUNCTION:
                    # Only counts if the statement itself is the call
                    # or its assignment, not a def/if wrapping one.
                    if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign)):
                        top_level_calls.add(GATE_FUNCTION)
        if not top_level_calls:
            not_top_level.append(path.relative_to(ROOT).as_posix())

    assert not_top_level == [], (
        "%s() is not a module-level statement in:\n  %s"
        % (GATE_FUNCTION, "\n  ".join(not_top_level))
    )


def test_no_page_writes_before_its_gate():
    """Ordering, not just presence: a gate placed after a write would
    still let an unauthenticated request mutate data before st.stop()
    ever ran. require_login() must precede every service call that
    takes app_user."""
    accepts = _app_user_accepting_functions()
    violations = []
    for path in _page_files():
        tree = _parse(path)
        gate_lines = [n.lineno for n in ast.walk(tree)
                      if isinstance(n, ast.Call) and _call_name(n) == GATE_FUNCTION]
        write_lines = [n.lineno for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and _call_name(n) in accepts]
        if not gate_lines or not write_lines:
            continue
        if min(write_lines) < min(gate_lines):
            violations.append("%s (write at line %d, gate at line %d)" % (
                path.relative_to(ROOT).as_posix(), min(write_lines), min(gate_lines)))

    assert violations == [], (
        "Service writes appear before the login gate in:\n  %s" % "\n  ".join(violations)
    )


# ------------------------------------------------------------------
# 2. Every call site threads app_user
# ------------------------------------------------------------------

def test_every_page_call_site_threads_app_user():
    """Each page calls require_login() and binds `app_user`, so every
    service call from a page that accepts app_user has one in scope and
    must pass it. Missing one writes a NULL-attributed audit row."""
    accepts = _app_user_accepting_functions()
    assert accepts, "Found no app_user-accepting service functions — scanner is broken."

    dropped = []
    for path in _page_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in accepts:
                continue
            if not _supplies_app_user(node, accepts[name]):
                dropped.append("%s:%d calls %s() without app_user" % (
                    path.relative_to(ROOT).as_posix(), node.lineno, name))

    assert dropped == [], (
        "Page call sites that drop app_user (each writes a NULL "
        "audit_log.app_user):\n  %s" % "\n  ".join(dropped)
    )


def test_every_service_internal_call_forwards_app_user():
    """The half that's invisible from the pages: a page can thread
    app_user correctly into assign_pair_to_duty(), and the attribution
    still be lost if that function fails to forward it down to
    log_audit(). Only checks callers that actually have app_user in
    scope — a function without the parameter has nothing to forward."""
    accepts = _app_user_accepting_functions()
    dropped = []
    for f in sorted(ROOT.glob("services/**/*.py")):
        rel = f.relative_to(ROOT).as_posix()
        for fn in ast.walk(_parse(f)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            in_scope = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
            if "app_user" not in in_scope:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name not in accepts:
                    continue
                if not _supplies_app_user(node, accepts[name]):
                    dropped.append("%s:%d  %s() calls %s() without forwarding app_user" % (
                        rel, node.lineno, fn.name, name))

    assert dropped == [], (
        "Service-internal call sites that drop app_user:\n  %s" % "\n  ".join(dropped)
    )


# ------------------------------------------------------------------
# 3. Runtime proof: the real page files actually stop
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "page", [p.relative_to(ROOT).as_posix() for p in _page_files()])
def test_unauthenticated_run_of_a_real_page_shows_the_login_form(page):
    """The static checks above prove require_login() is CALLED; this
    proves calling it actually stops the real page file. Complements
    test_auth_service.py, which exercises the same gate through a
    minimal inline script rather than the genuine pages.

    Needs no database, and that's the point being asserted as much as
    anything: an unauthenticated request must be turned away before the
    page reaches any of its own data access. If a page ever grew a
    module-level query above its gate, this test would start failing
    with a connection error instead of passing — which is the correct
    and loud outcome."""
    at = AppTest.from_file(page)
    at.run()

    assert not at.exception, "%s raised on an unauthenticated run: %s" % (
        page, at.exception)
    # The login form, not the page's own content.
    assert len(at.text_input) == 2, (
        "%s did not render the username/password form when unauthenticated "
        "— it is reachable without logging in" % page)
    assert any("Sign in" in b.label for b in at.button), (
        "%s rendered no Sign in button when unauthenticated" % page)


# ------------------------------------------------------------------
# 4. Runtime proof: a logged-in write leaves no NULL app_user
# ------------------------------------------------------------------

@pytest.fixture
def _patch_engine(_patch_all_service_engines):
    """Thin per-file wrapper — patching logic lives once in
    conftest.py's _patch_all_service_engines."""
    return _patch_all_service_engines


def test_writes_by_a_logged_in_user_never_leave_a_null_app_user(_patch_engine):
    """End-to-end complement to the static scans above: those prove
    app_user is syntactically passed everywhere, this proves it
    actually lands in the column. Exercises the real write paths a
    logged-in operator hits, then asserts the audit trail can answer
    'who' for every row it wrote."""
    import services.crew_service as crew_service
    import services.flight_service as flight_service

    app_user = "occ1"

    crew_id = crew_service.add_crew(
        {"name": "Attribution Test", "role": "CPT", "base": "KHI"}, app_user=app_user)
    crew_service.update_crew(crew_id, {"base": "LHE"}, app_user=app_user)
    crew_service.deactivate_crew(crew_id, reason="test", app_user=app_user)

    # Field names and the required `domestic` mirror
    # test_flight_service.py's _valid_flight(); real datetimes, not
    # strings, same as every other flight test in the suite.
    flight_id = flight_service.add_flight({
        "flight_no": "EA100",
        "origin": "KHI",
        "destination": "LHE",
        "dep_time_planned": dt.datetime(2026, 9, 1, 8, 0),
        "arr_time_planned": dt.datetime(2026, 9, 1, 10, 0),
        "domestic": True,
    }, app_user=app_user)
    flight_service.update_flight(flight_id, {"remarks": "attribution probe"}, app_user=app_user)
    flight_service.cancel_flight(flight_id, reason="test", app_user=app_user)

    rows = pd.read_sql(text("SELECT action_type, app_user FROM audit_log"), _patch_engine)

    # Guard against a vacuous pass: "no NULL rows" is trivially true
    # if nothing was logged at all.
    assert not rows.empty, "No audit rows written — test proves nothing."

    null_rows = rows[rows["app_user"].isna()]
    assert null_rows.empty, (
        "These audit_log rows have a NULL app_user despite being written "
        "by a logged-in user:\n%s" % null_rows.to_string()
    )
    assert set(rows["app_user"]) == {app_user}
