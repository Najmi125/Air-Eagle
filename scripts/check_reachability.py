"""
scripts/check_reachability.py

Flags any .py file under the WATCHED_DIRS (core/, services/, db/,
configs/) that is never imported by app.py, anything in pages/, or any
other file in those same directories.

This exists because the old repo had 17 files in utils/ — leave
checking, location tracking, the actuals engine, config loading,
candidate search, override guarding — that were fully built and
never connected to the running app. That was only discovered by a
one-off manual trace during a cleanup audit. This script makes that
trace a five-second, repeatable check instead of something that
only happens when someone thinks to do it by hand.

Usage:
    python scripts/check_reachability.py

Run this before ending any session that added a new file under
core/, services/, or db/. If it flags something, that's not
necessarily wrong — a file can legitimately be built ahead of being
wired in — but it should be a deliberate, visible state, not a
silent one. Note it in HANDOVER.md under "Open stubs" if so.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# configs added 2026-08-21. It holds only __init__.py today, so this is
# a no-op right now — but it is NOT dead space: README.md lists it in
# the project structure and services/reporting.py names a planned
# configs/airlines/AEAGLE/ layout for multi-tenant airline config.
#
# An unwatched directory in the repo is a blind spot: anything dropped
# into it would never be flagged as orphaned, which is exactly the
# failure this script exists to prevent. Watching it now means the
# guard is already in place on the day it gets populated, rather than
# being remembered then.
WATCHED_DIRS = ["core", "services", "db", "configs"]
# Root-level entry points ONLY, checked individually below in
# app_reachable_source_files() — anything under pages/ is already
# handled generically there via included_top, so it doesn't need
# per-file listing here (the old version of this constant globbed
# pages/*.py into ENTRY_POINTS too, as absolute paths, inconsistent
# with "app.py"'s relative form — dead weight: nothing ever actually
# read ENTRY_POINTS, so the inconsistency went unnoticed. Fixed
# 2026-08-10 alongside the missing_entry_points() guard below, in the
# same pass that finally wired this constant into real use).
ENTRY_POINTS = ["app.py", "home.py"]

# "from X import a, b, c" and "import X.Y.Z" need different handling.
# IMPORT_FROM_RE's second group captures the whole import clause,
# including a parenthesized multi-line form (`(?:[^)]*)` matches
# newlines too, since it's a negated character class, not "." — no
# DOTALL needed) as well as the plain single-line form.
IMPORT_FROM_RE = re.compile(
    r"^\s*from\s+([\w\.]+)\s+import\s+(\((?:[^)]*)\)|[^\n]+)", re.MULTILINE)
IMPORT_PLAIN_RE = re.compile(r"^\s*import\s+([\w\.]+)", re.MULTILINE)


def _names_from_import_clause(clause: str) -> list[str]:
    """
    '(\n    ANO012CoreValidator, CrewMember,\n)' or
    'crew_service, flight_service as fs' -> ['ANO012CoreValidator',
    'CrewMember'] / ['crew_service', 'flight_service']. Strips
    parens, 'as' aliases, and '*' (contributes no usable name).
    """
    clause = clause.split("#", 1)[0].replace("(", " ").replace(")", " ")
    names = []
    for part in clause.split(","):
        name = part.strip().split(" as ")[0].strip()
        if name and name != "*" and re.fullmatch(r"\w+", name):
            names.append(name)
    return names


def module_name_for(path: Path) -> str:
    """core/legality/pcaa_ano012_core.py -> core.legality.pcaa_ano012_core"""
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def all_watched_files():
    files = []
    for d in WATCHED_DIRS:
        dpath = ROOT / d
        if not dpath.exists():
            continue
        for f in dpath.rglob("*.py"):
            if f.name == "__init__.py":
                continue
            files.append(f)
    return files


def all_source_files():
    """Every .py file in the repo except venv/.git, for scanning import statements."""
    skip_dirs = {".git", "venv", ".venv", "__pycache__", "node_modules"}
    files = []
    for f in ROOT.rglob("*.py"):
        if any(part in skip_dirs for part in f.parts):
            continue
        files.append(f)
    return files


def app_reachable_source_files():
    """
    Files whose imports actually count as "this is wired into the app":
    ENTRY_POINTS, pages/, and cross-references within core/services/db
    themselves. Deliberately EXCLUDES tests/ and scripts/ — a file
    that's only ever imported by its own test is not connected to the
    running app, and counting test imports as reachability would mask
    exactly the gap this checker exists to catch.
    """
    included_top = {"pages"}
    files = []
    for f in all_source_files():
        rel = f.relative_to(ROOT)
        top = rel.parts[0] if len(rel.parts) > 1 else None
        if str(rel) in ENTRY_POINTS:
            files.append(f)
        elif top in included_top:
            files.append(f)
        elif top in WATCHED_DIRS:
            files.append(f)
    return files


def missing_entry_points(entry_points: list[str]) -> list[str]:
    """Declared entry_points that don't exist on disk. Given
    explicitly rather than reading the ENTRY_POINTS global directly —
    same "no implicit dependency, easy to unit test" principle as
    find_orphaned() below.

    This checker cannot safely report anything while blind to a
    declared entry point: app_reachable_source_files() only counts a
    file's imports if str(rel) matches something in ENTRY_POINTS
    exactly — a stale or typo'd entry silently drops that file from
    the scan entirely, and the checker can still print a clean "all
    reachable" pass, unaware it never looked at that entry point's own
    imports at all. Confirmed as a real, reproduced failure mode
    (2026-08-10): copying this branch, renaming app.py -> Home.py
    without updating ENTRY_POINTS, and running this checker produced
    exactly that — a clean, exit-0 pass with a declared entry point
    that didn't exist on disk."""
    return [ep for ep in entry_points if not (ROOT / ep).is_file()]


def find_all_imports():
    """
    Every module path this codebase's own import statements could
    plausibly make "reachable", scanned from app_reachable_source_files().

    "from X import a, b, c" adds BOTH:
      - X itself (covers `from core.legality.pcaa_ano012_core import
        CrewMember` — CrewMember is a class defined INSIDE that
        module, not a submodule; X is already the exact watched
        module path, and nothing else could prove it reachable)
      - X.a, X.b, X.c (covers `from services import crew_service` —
        crew_service IS itself services/crew_service.py, a distinct
        watched file that importing the bare "services" package does
        NOT, on its own, prove reachable)

    Confirmed real bug this replaces (2026-08-01): the previous
    version only ever captured the "X" part of "from X import
    a, b, c" — discarding a/b/c entirely — and services/assignment_
    service.py's is_imported check in main() used to treat that bare
    "X" as a PREFIX match for every file under X (`mod.startswith(imp
    + ".")`). Every page does `from services import crew_service,
    ...`, so every single file under services/ — including a brand
    new, genuinely never-imported one — silently read as "reachable"
    the moment ANY sibling in that package was named anywhere. core/
    never hit this only because every existing core/ import already
    happens to be fully-qualified (`from core.duty_builder import
    X`), not because the logic was actually correct. Caught by
    planting a deliberately unreferenced module under services/ and
    confirming it went unflagged; see
    tests/test_check_reachability.py for the regression test.
    """
    imported_modules = set()
    for f in app_reachable_source_files():
        try:
            # Explicit UTF-8: Path.read_text() with no encoding
            # defaults to the OS locale codec, which on Windows is
            # cp1252 ("charmap") — every page in pages/ contains
            # UTF-8 characters (em dashes, emoji in st.set_page_config)
            # that cp1252 can't decode. That silently raised
            # UnicodeDecodeError, caught below, and skipped ALL FOUR
            # page files' imports entirely — not just the one file
            # tripping the fallback logic. This wasn't visible before
            # because the (now-removed) over-broad prefix match in
            # main() accidentally covered for it: assignment_service.py
            # imports `from services import crew_service, ...` itself,
            # so under the old rule that self-referential bare
            # "services" token alone was enough to mark
            # assignment_service.py falsely "reachable," regardless of
            # whether pages/ was ever actually scanned. Two bugs
            # canceling out — fixing only the prefix-match bug made
            # this one visible as a wave of new false positives.
            text = f.read_text(encoding="utf-8")
        except Exception as e:
            # A file this checker can't read is a checker malfunction,
            # not a normal condition — silently skipping it (the
            # previous behavior) is exactly how the cp1252-vs-UTF-8
            # bug above went unnoticed. Loud on purpose: every skip
            # here means whatever that file imports may now read as
            # falsely orphaned.
            print(f"WARNING: could not read {f.relative_to(ROOT)} ({e}) — "
                  f"its imports were NOT scanned, results may be incomplete.")
            continue
        for base, clause in IMPORT_FROM_RE.findall(text):
            imported_modules.add(base)
            for name in _names_from_import_clause(clause):
                imported_modules.add(f"{base}.{name}")
        for match in IMPORT_PLAIN_RE.findall(text):
            imported_modules.add(match)
    return imported_modules


def find_orphaned(watched: list[Path], imported: set[str]) -> list[Path]:
    """
    Pure comparison, no filesystem/ROOT access — takes the two things
    main() would otherwise compute inline, so this decision can be
    unit-tested directly against synthetic (watched, imported) pairs
    without needing a real or fake repo on disk.
    """
    orphaned = []
    for f in watched:
        mod = module_name_for(f)
        # Exact match only, both directions — deliberately NO
        # "mod.startswith(imp + '.')" prefix check. That direction
        # used to let a bare parent-package capture (see
        # find_all_imports()'s docstring for exactly how) silently
        # mark every descendant "reachable" regardless of whether it
        # was actually named anywhere. "imp.startswith(mod + '.')"
        # (the other direction) is kept: a more specific import can
        # still prove a less-specific watched path reachable, e.g. if
        # __init__.py files were ever added to WATCHED_DIRS in future.
        is_imported = any(
            imp == mod or imp.startswith(mod + ".")
            for imp in imported
        )
        if not is_imported:
            orphaned.append(f)
    return orphaned


def main():
    missing = missing_entry_points(ENTRY_POINTS)
    if missing:
        print(f"{len(missing)} declared ENTRY_POINTS file(s) do not exist on disk:\n")
        for ep in missing:
            print(f"  {ep}")
        print(
            "\nThis checker cannot safely report anything while blind to a "
            "declared entry point — update ENTRY_POINTS in "
            "scripts/check_reachability.py (or restore the missing file) "
            "before trusting this checker's result."
        )
        return 1

    watched = all_watched_files()
    imported = find_all_imports()
    orphaned = find_orphaned(watched, imported)

    if not orphaned:
        # Built from WATCHED_DIRS rather than hardcoded, so the
        # message cannot drift from what was actually checked —
        # it already had, listing three dirs after configs was added.
        watched = ", ".join(f"{d}/" for d in WATCHED_DIRS)
        print(f"All files under {watched} are reachable from somewhere.")
        return 0

    print(f"{len(orphaned)} file(s) not imported from anywhere in the repo:\n")
    for f in orphaned:
        print(f"  {f.relative_to(ROOT)}")
    print(
        "\nThis isn't automatically wrong — a file can be built ahead of "
        "being wired in. But it should be a deliberate, visible state. "
        "Note it in HANDOVER.md under 'Open stubs' if so."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
