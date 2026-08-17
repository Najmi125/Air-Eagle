"""
tests/test_auth_service.py

hash_password/verify_password are pure logic, no DB needed.
authenticate() and require_login() need the real users table
(migrated_db) — authenticate() reads it directly, and require_login()
is exercised here via AppTest.from_string(), a minimal inline script
rather than a separate fixture file, since the whole gate is one
function call plus a stop.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from streamlit.testing.v1 import AppTest

from services.auth_service import authenticate, hash_password, verify_password


# ------------------------------------------------------------------
# hash_password / verify_password — pure logic
# ------------------------------------------------------------------

def test_hash_password_round_trip_verifies_true():
    password_hash, salt = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", password_hash, salt) is True


def test_verify_password_rejects_wrong_password():
    password_hash, salt = hash_password("correct horse battery staple")
    assert verify_password("wrong password", password_hash, salt) is False


def test_hash_password_uses_a_fresh_salt_every_call():
    """Two hashes of the SAME password must differ — a fixed or
    reused salt would make identical passwords produce identical
    hashes, which is exactly what a per-user random salt exists to
    prevent."""
    hash_a, salt_a = hash_password("same password")
    hash_b, salt_b = hash_password("same password")
    assert salt_a != salt_b
    assert hash_a != hash_b


# ------------------------------------------------------------------
# authenticate() — real users table
# ------------------------------------------------------------------

def _seed_user(migrated_db, username, password):
    password_hash, salt = hash_password(password)
    with migrated_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (username, password_hash, salt) "
            "VALUES (:username, :password_hash, :salt)"
        ), {"username": username, "password_hash": password_hash, "salt": salt})


def test_authenticate_correct_credentials_returns_true(migrated_db, monkeypatch):
    import services.auth_service as auth_mod
    monkeypatch.setattr(auth_mod, "get_engine", lambda: migrated_db)
    _seed_user(migrated_db, "occ1", "s3cret-pw")

    assert authenticate("occ1", "s3cret-pw") is True


def test_authenticate_wrong_password_returns_false(migrated_db, monkeypatch):
    import services.auth_service as auth_mod
    monkeypatch.setattr(auth_mod, "get_engine", lambda: migrated_db)
    _seed_user(migrated_db, "occ1", "s3cret-pw")

    assert authenticate("occ1", "wrong-pw") is False


def test_authenticate_unknown_username_returns_false(migrated_db, monkeypatch):
    import services.auth_service as auth_mod
    monkeypatch.setattr(auth_mod, "get_engine", lambda: migrated_db)

    assert authenticate("nobody", "anything") is False


# ------------------------------------------------------------------
# require_login() — the Streamlit-facing gate, via a minimal inline page
# ------------------------------------------------------------------

_GATE_SCRIPT = """
import streamlit as st
from services import auth_service

app_user = auth_service.require_login()
st.write(f"Logged in as {app_user}")
"""


def test_require_login_shows_form_and_stops_when_not_authenticated():
    at = AppTest.from_string(_GATE_SCRIPT)
    at.run()

    assert not at.exception
    assert len(at.text_input) == 2  # username, password
    assert len(at.button) == 1      # "Sign in"
    # Nothing past require_login() ran.
    assert not any("Logged in as" in m.value for m in at.markdown)


def test_require_login_already_authenticated_session_skips_the_form():
    at = AppTest.from_string(_GATE_SCRIPT)
    at.session_state["app_user"] = "occ1"
    at.run()

    assert not at.exception
    assert len(at.text_input) == 0
    assert any("Logged in as occ1" in m.value for m in at.markdown)


def test_require_login_wrong_password_shows_error_and_stays_logged_out(migrated_db, monkeypatch):
    import services.auth_service as auth_mod
    monkeypatch.setattr(auth_mod, "get_engine", lambda: migrated_db)
    _seed_user(migrated_db, "occ1", "s3cret-pw")

    at = AppTest.from_string(_GATE_SCRIPT)
    at.run()
    at.text_input[0].input("occ1")
    at.text_input[1].input("wrong-pw")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert any("Incorrect username or password" in e.value for e in at.error)
    assert "app_user" not in at.session_state


def test_require_login_correct_password_sets_session_and_renders_page(migrated_db, monkeypatch):
    import services.auth_service as auth_mod
    monkeypatch.setattr(auth_mod, "get_engine", lambda: migrated_db)
    _seed_user(migrated_db, "occ1", "s3cret-pw")

    at = AppTest.from_string(_GATE_SCRIPT)
    at.run()
    at.text_input[0].input("occ1")
    at.text_input[1].input("s3cret-pw")
    at.button[0].click()
    at = at.run()

    assert not at.exception
    assert at.session_state["app_user"] == "occ1"
    assert any("Logged in as occ1" in m.value for m in at.markdown)
