"""
db/db.py

Single owner of the database connection. Every other module gets
its engine from here — nothing else should call create_engine()
directly (that was never actually violated in the old repo, this
one file was fine; it's included here unchanged in spirit, just
relocated to match the new structure and loaded via .env properly).
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(override=True)
# override=True: this project's own .env must always win over any
# environment variable that happens to already be set in the shell
# or system (e.g. a leftover DATABASE_URL from an unrelated earlier
# project). Without override=True, python-dotenv silently keeps
# whatever's already in os.environ and never applies .env at all —
# confirmed as a real, confusing bug: a user's shell had a stale
# Neon DATABASE_URL persistently set from earlier work, and every
# connection silently went there instead of the Supabase URL just
# configured in .env, with no error or warning of any kind.


def _resolve_database_url() -> str | None:
    """.env/environment wins when present (unchanged — every test and
    script already depends on this); st.secrets is consulted only when
    DATABASE_URL is still absent, for Streamlit Community Cloud (2026-08-10)
    — .env is gitignored and doesn't exist in the deployed container.

    streamlit is imported INSIDE this function, not at module level:
    db.py is imported by scripts/run_migrations.py and
    scripts/import_crew_from_xlsx.py specifically outside any Streamlit
    runtime, and it shouldn't have to pull in the Streamlit runtime
    just to run a migration. st.secrets itself raises
    StreamlitSecretNotFoundError with no Streamlit runtime context and
    no secrets.toml file present (confirmed directly) — the broad
    except is what lets this function stay silent and fall through to
    get_engine()'s own RuntimeError in that case, rather than crashing
    with a different, confusing exception from in here.
    """
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return db_url
    try:
        import streamlit as st
        return st.secrets.get("DATABASE_URL")
    except Exception:
        return None


@lru_cache(maxsize=1)
def get_engine():
    db_url = _resolve_database_url()
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set. Copy .env.example to .env and fill it "
            "in, or (on Streamlit Community Cloud) add DATABASE_URL to "
            "the app's secrets."
        )
    # Small pool size — this app runs on a small managed Postgres
    # instance (Neon/Supabase free or low tier), not a large dedicated
    # server. Keep this deliberate, don't bump it without checking the
    # DB plan's connection limit first.
    return create_engine(db_url, pool_size=3, max_overflow=2, pool_pre_ping=True)


def test_connection():
    """Returns True if the DB is reachable, or the exception as a string."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        return str(e)
