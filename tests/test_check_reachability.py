"""
tests/test_check_reachability.py

Regression tests for scripts/check_reachability.py's import-tracing
logic, using isolated fake repos built under pytest's tmp_path (never
the real project tree) — so these tests stay correct regardless of
how the real repo's files evolve, and monkeypatch ROOT rather than
depending on this session's actual file layout.

Written after two real, stacked bugs found while adding
services/assistant/query_parser.py (2026-08-01):

1. find_all_imports() only ever captured the "X" part of
   "from X import a, b, c" — discarding a/b/c — and main()'s
   reachability check treated that bare "X" as a PREFIX match for
   EVERY file under X (`mod.startswith(imp + ".")`). Every page does
   `from services import crew_service, ...`, so a brand new file
   under services/ (or any subpackage of it) read as "reachable" the
   moment ANY sibling in that package was named anywhere, regardless
   of whether it was actually imported. Confirmed by planting a
   genuinely unreferenced module and observing it went unflagged.

2. Masked by bug 1 until it was removed: Path.read_text() with no
   explicit encoding defaults to the OS locale codec (cp1252 on
   Windows), which can't decode the UTF-8 characters (em dashes,
   emoji) every page file contains — silently caught by a bare
   except: continue, so pages/ was never actually scanned for imports
   at all. Bug 1's over-broad prefix match had been accidentally
   compensating for this the whole time (assignment_service.py's own
   `from services import crew_service, ...` line was enough to
   "self-prove" it reachable). Fixing only bug 1 turned bug 2 into a
   wave of new false positives before it was caught too.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.check_reachability as reachability


def _make_repo(tmp_path: Path, files: dict) -> Path:
    """files: {relative_path: content}. Creates each file (and its
    parent dirs) under tmp_path."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def _orphaned_module_names(tmp_path, monkeypatch):
    monkeypatch.setattr(reachability, "ROOT", tmp_path)
    watched = reachability.all_watched_files()
    imported = reachability.find_all_imports()
    return {reachability.module_name_for(f) for f in reachability.find_orphaned(watched, imported)}


def test_unreferenced_module_in_a_subpackage_is_flagged(tmp_path, monkeypatch):
    """The actual regression case: a file sitting in a subpackage
    (services/assistant/), never imported by anything, must be
    flagged — this exact scenario went silently unflagged before the
    fix, precisely because it's nested under a package (services/)
    that other, unrelated files import names from."""
    _make_repo(tmp_path, {
        "app.py": "",
        "services/__init__.py": "",
        "services/crew_service.py": "def foo(): pass\n",
        "services/assistant/__init__.py": "",
        "services/assistant/query_parser.py": "def parse(): pass\n",
    })

    orphaned = _orphaned_module_names(tmp_path, monkeypatch)

    assert "services.assistant.query_parser" in orphaned


def test_genuinely_imported_module_in_a_subpackage_is_not_flagged(tmp_path, monkeypatch):
    """The other half of the same fix: a real caller naming a
    subpackage module must make it reachable — the fix must narrow
    false positives, not just make everything noisy."""
    _make_repo(tmp_path, {
        "app.py": "",
        "pages/1_Roster.py": (
            "from services import crew_service\n"
            "from services.assistant import query_parser\n"
        ),
        "services/__init__.py": "",
        "services/crew_service.py": "def foo(): pass\n",
        "services/assistant/__init__.py": "",
        "services/assistant/query_parser.py": "def parse(): pass\n",
    })

    orphaned = _orphaned_module_names(tmp_path, monkeypatch)

    assert "services.assistant.query_parser" not in orphaned
    assert "services.crew_service" not in orphaned


def test_bare_from_import_does_not_mark_unrelated_siblings_reachable(tmp_path, monkeypatch):
    """The regression this whole fix exists for: 'from services
    import crew_service' must prove crew_service specifically
    reachable, not silently vouch for every other file under
    services/ too — including ones with no relationship to it at
    all, not even a shared subpackage."""
    _make_repo(tmp_path, {
        "app.py": "",
        "pages/1_Roster.py": "from services import crew_service\n",
        "services/__init__.py": "",
        "services/crew_service.py": "def foo(): pass\n",
        "services/unrelated_module.py": "def bar(): pass\n",
    })

    orphaned = _orphaned_module_names(tmp_path, monkeypatch)

    assert "services.crew_service" not in orphaned
    assert "services.unrelated_module" in orphaned


def test_symbol_imported_from_an_exact_watched_module_marks_it_reachable(tmp_path, monkeypatch):
    """'from core.legality.pcaa_ano012_core import CrewMember' must
    mark core/legality/pcaa_ano012_core.py itself reachable —
    CrewMember is a class defined INSIDE that module, not a
    submodule, so it's the base path alone (not a constructed
    base+name candidate) that has to match."""
    _make_repo(tmp_path, {
        "app.py": "",
        "pages/1_Roster.py": "from core.legality.pcaa_ano012_core import CrewMember\n",
        "core/__init__.py": "",
        "core/legality/__init__.py": "",
        "core/legality/pcaa_ano012_core.py": "class CrewMember: pass\n",
    })

    orphaned = _orphaned_module_names(tmp_path, monkeypatch)

    assert "core.legality.pcaa_ano012_core" not in orphaned


def test_multiline_parenthesized_from_import_extracts_every_name(tmp_path, monkeypatch):
    """The multi-line parenthesized form used throughout this
    codebase's own service layer must be parsed fully, not just up to
    the first line."""
    _make_repo(tmp_path, {
        "app.py": "",
        "services/__init__.py": "",
        "services/consumer.py": (
            "from services.targets import (\n"
            "    FirstTarget,\n"
            "    SecondTarget,\n"
            ")\n"
        ),
        "services/targets.py": "class FirstTarget: pass\nclass SecondTarget: pass\n",
    })

    orphaned = _orphaned_module_names(tmp_path, monkeypatch)

    assert "services.targets" not in orphaned


def test_non_ascii_content_in_a_watched_file_does_not_block_import_scanning(tmp_path, monkeypatch):
    """The positive case for the encoding fix: a real file containing
    em dashes and other non-ASCII characters (exactly what every page
    in this repo already has, in docstrings/comments/emoji) must
    still be read correctly and have its own import statements
    counted — not silently skipped the way cp1252-by-default did
    before Path.read_text(encoding="utf-8") was made explicit."""
    _make_repo(tmp_path, {
        "app.py": (
            '"""Uses an em dash — and an emoji 🗓️ in a docstring, '
            'exactly like the real pages/ files do."""\n'
            "from services import crew_service\n"
        ),
        "services/__init__.py": "",
        "services/crew_service.py": "def foo(): pass\n",
    })

    orphaned = _orphaned_module_names(tmp_path, monkeypatch)

    assert "services.crew_service" not in orphaned


def test_unreadable_file_produces_a_warning_not_a_silent_skip(tmp_path, monkeypatch, capsys):
    """The second bug this session found: a file this checker can't
    decode must be loud about it, not quietly degrade the guardrail.
    Simulated by monkeypatching Path.read_text to fail for one
    specific file, rather than depending on a real encoding mismatch
    that this environment may or may not reproduce."""
    _make_repo(tmp_path, {
        "app.py": "",
        "pages/1_Roster.py": "from services import crew_service\n",
        "services/__init__.py": "",
        "services/crew_service.py": "def foo(): pass\n",
    })
    monkeypatch.setattr(reachability, "ROOT", tmp_path)

    real_read_text = Path.read_text

    def _boom(self, *a, **kw):
        if self.name == "1_Roster.py":
            raise UnicodeDecodeError("cp1252", b"\xe2\x80\x94", 0, 1, "simulated")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)

    reachability.find_all_imports()
    captured = capsys.readouterr()

    assert "WARNING" in captured.out
    assert "1_Roster.py" in captured.out
