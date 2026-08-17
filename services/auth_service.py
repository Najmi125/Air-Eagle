"""
services/auth_service.py

Authentication for attribution, not access control. See
AuthPlan-Ccode.txt for the full design: three fixed OCC accounts,
full access each, no tiers, no self-registration, no password
reset flow (re-running scripts/seed_users.py IS the reset
mechanism). The point is knowing which account performed a write,
not restricting who can perform it.

Password storage: stdlib hashlib.pbkdf2_hmac, no new dependency —
600,000 iterations, per-user random 16-byte salt, constant-time
compare via secrets.compare_digest. Enough for three accounts; no
need for bcrypt/passlib at this scale.

require_login() is the one function every page calls (right after
its own st.set_page_config()) — the check logic lives here once so
it isn't duplicated 8 times. It has to run at the top of every page
script, not only in app.py's router: AppTest.from_file() execs each
page script directly and bypasses st.navigation() entirely, so a
router-only check would leave every page unprotected under test (and
in reality, since the router is just the normal navigation path, not
an enforcement boundary).
"""
import hashlib
import secrets
from typing import Optional

import streamlit as st
from sqlalchemy import text

from db.db import get_engine

PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> tuple[bytes, bytes]:
    """Returns (password_hash, salt). salt is fresh random bytes on
    every call — never reused across users or calls."""
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return password_hash, salt


def verify_password(password: str, password_hash: bytes, salt: bytes) -> bool:
    """Constant-time compare — a timing difference between 'wrong
    password' and 'wrong username' (or between rejections that differ
    only in how many leading bytes matched) is exactly the kind of
    side channel this must not leak, even though the practical
    exposure is low for three internal accounts."""
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes(salt), PBKDF2_ITERATIONS)
    return secrets.compare_digest(candidate, bytes(password_hash))


def authenticate(username: str, password: str) -> bool:
    """Looks up the row, verifies. False for an unknown username —
    same as a wrong password, not a distinguishable error, so this
    function alone can't be used to enumerate valid usernames."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT password_hash, salt FROM users WHERE username = :username"),
            {"username": username},
        ).fetchone()
    if row is None:
        return False
    password_hash, salt = row
    return verify_password(password, password_hash, salt)


def require_login() -> str:
    """Streamlit-facing gate. Returns the logged-in username.

    If st.session_state["app_user"] is already set, returns it
    immediately — no re-render, no form. Otherwise renders a
    username/password form, authenticates on submit, and on success
    sets st.session_state["app_user"] and st.rerun()s so the calling
    page re-executes from the top with the session now authenticated.

    Calls st.stop() whenever login hasn't succeeded yet, so nothing
    below this call in the calling page ever executes — this is what
    makes "one line near the top of every page" an actual gate rather
    than just a display.
    """
    existing = st.session_state.get("app_user")
    if existing:
        return existing

    st.title("Sign in")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        if authenticate(username, password):
            st.session_state["app_user"] = username
            st.rerun()
        else:
            st.error("Incorrect username or password.")

    st.stop()
