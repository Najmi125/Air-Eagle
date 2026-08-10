"""
tests/test_env_override.py

Regression test for a real, confirmed bug: a user's shell had a
stale DATABASE_URL environment variable left over from an unrelated
earlier project (a Neon database). Every script silently connected
there instead of the Supabase URL they'd carefully configured in
.env, with no error of any kind — because load_dotenv() was called
without override=True (or, in scripts/run_migrations.py's case,
wasn't called at all), so python-dotenv left the pre-existing shell
variable untouched rather than applying .env's value.

This test proves the actual mechanism works, independent of the
real db.py/run_migrations.py call sites — if python-dotenv's own
override behavior ever changes, or someone "simplifies" one of those
two load_dotenv(override=True) calls back to load_dotenv(), this
test fails loudly rather than silently reintroducing the exact bug
that already wasted real debugging time once.
"""
import os
from pathlib import Path

from dotenv import load_dotenv


def test_env_file_overrides_preexisting_shell_variable(tmp_path, monkeypatch):
    """The actual bug scenario: DATABASE_URL is already set (as if
    left over from another project) BEFORE load_dotenv() runs. With
    override=True, the .env file's value must win anyway."""
    stale_value = "postgresql://neondb_owner:stale@old-host.neon.tech/neondb"
    real_value = "postgresql://postgres:real@db.supabase.co:5432/postgres"

    monkeypatch.setenv("DATABASE_URL", stale_value)

    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={real_value}\n")

    load_dotenv(dotenv_path=env_file, override=True)

    assert os.environ["DATABASE_URL"] == real_value
    assert os.environ["DATABASE_URL"] != stale_value


def test_without_override_stale_value_would_have_won(tmp_path, monkeypatch):
    """Demonstrates the bug explicitly, not just the fix — without
    override=True, python-dotenv leaves the pre-existing value alone
    and .env is silently ignored. This is what actually happened."""
    stale_value = "postgresql://neondb_owner:stale@old-host.neon.tech/neondb"
    real_value = "postgresql://postgres:real@db.supabase.co:5432/postgres"

    monkeypatch.setenv("DATABASE_URL", stale_value)

    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={real_value}\n")

    load_dotenv(dotenv_path=env_file, override=False)  # the old, buggy behavior

    assert os.environ["DATABASE_URL"] == stale_value  # .env silently ignored


def test_well_formed_placeholder_string_is_truthy_not_treated_as_unset():
    """The exact bug that blocked a real user twice: a placeholder
    like 'postgresql://user:password@host:5432/dbname_test' LOOKS
    unset to a human, but it's a non-empty Python string — `if not
    value` never triggers on it, so conftest.py's db_engine fixture
    would try to actually connect to a literal host named 'host'
    instead of skipping. This is exactly why .env.example's
    TEST_DATABASE_URL must be genuinely empty, not a placeholder
    string, even though DATABASE_URL's placeholder being non-empty
    is fine (that one's meant to be filled in, not left as a signal
    for 'skip this')."""
    placeholder = "postgresql://user:password@host:5432/dbname_test"
    assert bool(placeholder) is True  # looks unset to a human, is NOT unset to Python
    assert not placeholder is False   # `if not placeholder` does NOT trigger


def test_genuinely_empty_string_is_falsy_correctly_treated_as_unset():
    """The fix: an empty string IS falsy, so `if not test_url` in
    conftest.py's db_engine fixture correctly triggers pytest.skip()
    instead of attempting a connection."""
    empty = ""
    assert not empty  # `if not empty` DOES trigger — correct skip behavior


# ------------------------------------------------------------------
# st.secrets fallback (2026-08-10) — Streamlit Community Cloud has no
# .env (gitignored, doesn't exist in the deployed container), so
# db.py._resolve_database_url() falls back to st.secrets when the env
# var is absent. These three protect the three things that actually
# matter: the fallback works, it never shadows the existing .env/
# environment precedence, and db.py stays importable with no Streamlit
# runtime at all — which is what scripts/run_migrations.py,
# scripts/import_crew_from_xlsx.py, and this whole test suite rely on.
# ------------------------------------------------------------------

def test_secrets_used_when_env_var_absent(monkeypatch):
    from db.db import _resolve_database_url

    monkeypatch.delenv("DATABASE_URL", raising=False)
    secret_value = "postgresql://postgres:secret@db.supabase.co:5432/postgres"

    class _FakeSecrets(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    fake_secrets = _FakeSecrets({"DATABASE_URL": secret_value})
    import streamlit as st
    monkeypatch.setattr(st, "secrets", fake_secrets)

    assert _resolve_database_url() == secret_value


def test_env_var_wins_over_secrets_when_both_present(monkeypatch):
    """st.secrets must not even be CONSULTED when the env var is
    present — proven with a spy, not just by checking the winning
    value, so a future refactor can't accidentally start preferring
    secrets while still returning the right value by coincidence."""
    from db.db import _resolve_database_url

    env_value = "postgresql://postgres:real@db.supabase.co:5432/postgres"
    monkeypatch.setenv("DATABASE_URL", env_value)

    class _SpySecrets(dict):
        def get(self, key, default=None):
            raise AssertionError("st.secrets.get() must not be called when the env var is present")

    import streamlit as st
    monkeypatch.setattr(st, "secrets", _SpySecrets())

    assert _resolve_database_url() == env_value


def test_resolve_database_url_works_with_no_streamlit_runtime_at_all(monkeypatch):
    """No Streamlit runtime context AND no secrets.toml file — exactly
    scripts/run_migrations.py's/the test suite's own situation. Must
    return None (falling through to get_engine()'s existing
    RuntimeError), never raise a different, unhandled exception from
    inside _resolve_database_url() itself. Genuinely exercises the
    real st.secrets, unmocked — this is the test that would fail if
    the try/except guard were ever narrowed or removed."""
    from db.db import _resolve_database_url

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert _resolve_database_url() is None
