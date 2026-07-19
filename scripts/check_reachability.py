"""
scripts/check_reachability.py

Flags any .py file under core/, services/, db/ that is never
imported by app.py, anything in pages/, or any other file in those
same directories.

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
WATCHED_DIRS = ["core", "services", "db"]
ENTRY_POINTS = ["app.py"] + [str(p) for p in (ROOT / "pages").glob("*.py")] if (ROOT / "pages").exists() else ["app.py"]

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([\w\.]+)", re.MULTILINE)


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


def find_all_imports():
    imported_modules = set()
    for f in all_source_files():
        try:
            text = f.read_text()
        except Exception:
            continue
        for match in IMPORT_RE.findall(text):
            imported_modules.add(match)
    return imported_modules


def main():
    watched = all_watched_files()
    imported = find_all_imports()

    orphaned = []
    for f in watched:
        mod = module_name_for(f)
        # A file is "reachable" if its dotted module path (or any prefix
        # of it, e.g. "core.legality" importing "core.legality.pcaa_...")
        # appears in any import statement anywhere else in the repo.
        is_imported = any(
            imp == mod or imp.startswith(mod + ".") or mod.startswith(imp + ".")
            for imp in imported
        )
        if not is_imported:
            orphaned.append(f)

    if not orphaned:
        print("All files under core/, services/, db/ are reachable from somewhere.")
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
