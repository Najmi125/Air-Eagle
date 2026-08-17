"""
scripts/seed_users.py

Creates or resets one OCC account. Run once per account by the
operator, never through the app UI — there is no self-registration
surface to build or secure. Re-running with an existing username
resets that user's password; this doubles as the only password-reset
mechanism, which is deliberate (AuthPlan-Ccode.txt's settled spec
excludes a self-service reset flow).

Password is never accepted as a CLI argument (would land in shell
history, unredacted) and never echoed to the terminal — always
prompted twice via getpass.getpass(), same reasoning as every other
credential-entry point in this repo staying out of argv.

Usage:
    python scripts/seed_users.py <username>
"""
import sys
import argparse
import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from db.db import get_engine
from services.auth_service import hash_password


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    args = parser.parse_args()

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match — nothing was written.")
        sys.exit(1)
    if not password:
        print("Password cannot be empty — nothing was written.")
        sys.exit(1)

    password_hash, salt = hash_password(password)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO users (username, password_hash, salt)
            VALUES (:username, :password_hash, :salt)
            ON CONFLICT (username) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    salt = EXCLUDED.salt
        """), {"username": args.username, "password_hash": password_hash, "salt": salt})

    print(f"User {args.username!r} created/updated.")


if __name__ == "__main__":
    main()
