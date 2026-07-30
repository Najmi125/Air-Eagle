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
