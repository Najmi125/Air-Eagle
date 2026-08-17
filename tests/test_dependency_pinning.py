"""
tests/test_dependency_pinning.py

Guards the dependency pins, after the incident that made them exist
(2026-08-18).

requirements.txt carried `>=` ranges. A recycled sandbox reinstalled
from `streamlit>=1.38`, resolved to 1.61.1 rather than the 1.60.0
everything had been verified against, and 1.61.1 changed AppTest's
behaviour. Unchanged `main` went from 527/527 to 468 passed / 2 failed
/ 57 errors. Nothing in the repo had changed. The failures presented as
a regression in the branch under review, and two verification rounds
went into establishing that they weren't — the second of which only
succeeded because the same failures were reproduced on `main`.

Two guards, both DB-free so they run everywhere:

1. The pins stay exact. A single `>=` reintroduced later puts the suite
   back to testing whatever the resolver picked that morning.
2. The INSTALLED versions match the pins. This is the one that would
   have caught the incident at its source: instead of 57 errors with a
   FileNotFoundError signature pointing at the test harness, the suite
   would have said "streamlit 1.61.1 installed, 1.60.0 pinned" on the
   first run in the new sandbox.

If you are deliberately testing an upgrade, change the pin in
requirements.txt and regenerate requirements.lock — do not relax these
tests. A failure here is the signal working, not noise.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importlib.metadata import PackageNotFoundError, version

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
LOCKFILE = ROOT / "requirements.lock"

# "name==1.2.3" -> ("name", "1.2.3"); ignores blanks and # comments.
_PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;#]+)")


def _requirement_lines(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def _pins(path: Path) -> dict:
    pins = {}
    for line in _requirement_lines(path):
        m = _PIN.match(line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def test_requirements_txt_pins_every_dependency_exactly():
    """No `>=`, `~=`, `>`, `<` or bare names — every direct dependency
    must name one exact version."""
    unpinned = [line for line in _requirement_lines(REQUIREMENTS)
                if not _PIN.match(line)]

    assert unpinned == [], (
        "requirements.txt must pin every dependency with '==' — these do "
        "not, so a fresh install can silently resolve to a different "
        "version than the suite was verified against:\n  %s"
        % "\n  ".join(unpinned)
    )


def test_requirements_lock_exists_and_is_fully_pinned():
    assert LOCKFILE.is_file(), (
        "requirements.lock is missing — regenerate it with "
        "`pip freeze > requirements.lock` from an environment where the "
        "full suite passes."
    )
    unpinned = [line for line in _requirement_lines(LOCKFILE)
                if not _PIN.match(line)]
    assert unpinned == [], (
        "requirements.lock has entries that are not exactly pinned:\n  %s"
        % "\n  ".join(unpinned)
    )


def test_every_direct_dependency_appears_in_the_lock():
    """The lock is the full transitive set, so it must be a superset of
    the direct pins — and must agree with them. A direct dependency
    missing here means the lock was generated from an environment that
    did not actually have it installed (exactly how openpyxl, declared
    but never installed locally, kept two test modules uncollectable)."""
    direct, locked = _pins(REQUIREMENTS), _pins(LOCKFILE)

    missing = sorted(set(direct) - set(locked))
    assert missing == [], (
        "These are pinned in requirements.txt but absent from "
        "requirements.lock — regenerate the lock from an environment "
        "with everything installed: %s" % ", ".join(missing)
    )

    disagreements = ["%s: requirements.txt==%s but lock==%s" % (n, direct[n], locked[n])
                     for n in sorted(direct) if direct[n] != locked[n]]
    assert disagreements == [], (
        "requirements.txt and requirements.lock disagree:\n  %s"
        % "\n  ".join(disagreements)
    )


@pytest.mark.parametrize("name,pinned", sorted(_pins(REQUIREMENTS).items()))
def test_installed_version_matches_the_pin(name, pinned):
    """The guard that would have caught the 1.61.1 incident on the first
    run in the new sandbox, instead of as 57 errors that looked like a
    code regression."""
    try:
        installed = version(name)
    except PackageNotFoundError:
        pytest.fail(
            "%s is pinned at %s but is not installed. Run "
            "`pip install -r requirements.lock`." % (name, pinned)
        )

    assert installed == pinned, (
        "%s %s is installed but %s is pinned. The suite is not running "
        "against the verified dependency set — this is exactly the drift "
        "that produced 57 spurious errors on 2026-08-18. Run "
        "`pip install -r requirements.lock`, or change the pin "
        "deliberately if you are testing an upgrade."
        % (name, installed, pinned)
    )
