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


@lru_cache(maxsize=1)
def get_engine():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set. Copy .env.example to .env and fill it in."
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
