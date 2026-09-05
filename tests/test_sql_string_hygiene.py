"""No typographic characters inside SQL strings.

THE MOJIBAKE CLASS AGAIN, in a new form (2026-09-05). A blanket
" -- " -> " — " comment-style pass over
`services/assignment_service.py` rewrote eleven lines that were SQL
comment markers inside a query string:

    SELECT r.roster_id, ...,
           — operating_position added 2026-08-21, additive: no
           — existing consumer selects columns by position.
           r.operating_position,

SQL comments are `--`. An em dash is not syntax, so every call through
that function failed to parse and 52 tests fell over at once. It reads
correctly to a human and parses as nothing — the same shape as the
2026-08 em dashes inside regex alternations, which compiled fine and
then matched nothing forever.

Both times the character was introduced by a well-meaning pass that
made prose nicer without asking what each occurrence WAS. This test is
the thing that notices, because reading the diff did not.

Uses `ast` rather than a regex over the raw file: a string literal is
something Python can identify exactly, and "is this line inside a
string" is not a question a regex answers reliably.
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCANNED_DIRS = ("services", "core", "db", "pages", "scripts")

# Characters that read as ordinary punctuation and are not SQL syntax.
# The dashes are the ones that have actually bitten; the quotes and the
# non-breaking space are the same trap one autocorrect away.
FORBIDDEN = {
    "—": "em dash (SQL comments are --)",
    "–": "en dash (SQL comments are --)",
    "−": "minus sign (subtraction is -)",
    "‘": "left single quote (SQL quotes are ')",
    "’": "right single quote (SQL quotes are ')",
    "“": "left double quote",
    "”": "right double quote",
    " ": "non-breaking space",
}

# A STATEMENT, not a word. The first version of this test asked
# whether "UPDATE " appeared anywhere in the string and flagged four
# docstrings and a Crew Data caption -- "Update one or more fields on
# an existing flight", "Renewing a document? Update its expiry date".
# Prose about the database is not SQL, and a hygiene test that cries
# wolf on it gets suppressed rather than fixed.
STATEMENT_STARTS = ("SELECT ", "INSERT INTO ", "UPDATE ", "DELETE FROM ",
                    "CREATE TABLE ", "ALTER TABLE ", "CREATE INDEX ",
                    "CREATE UNIQUE ", "WITH ")
CLAUSE_WORDS = (" FROM ", " WHERE ", " VALUES ", " SET ", " JOIN ",
                " GROUP BY ", " ORDER BY ", " RETURNING ")


def _looks_like_sql(value: str) -> bool:
    """A string is SQL when some LINE of it begins a statement AND the
    string carries a clause keyword. Both halves are needed: the first
    alone matches a docstring sentence that happens to start with
    "Update ", and the second alone matches almost any prose."""
    upper = value.upper()
    starts = any(line.strip().startswith(STATEMENT_STARTS)
                 for line in upper.splitlines())
    return starts and any(word in upper for word in CLAUSE_WORDS)


def _docstring_nodes(tree):
    """Every docstring, by identity. A module's prose is not SQL even
    when it quotes a query, and flagging it is how this test would earn
    a blanket ignore."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _sql_literals(path: Path):
    """(line number, text) for every string constant in the file that
    contains SQL."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _looks_like_sql(node.value):
                yield node.lineno, node.value
        # An f-string's literal halves are JoinedStr parts, and the
        # query built by _seat_holders() is one — a scan that only
        # looked at plain Constants would miss exactly the code this
        # test was written for.
        elif isinstance(node, ast.JoinedStr):
            text = "".join(part.value for part in node.values
                           if isinstance(part, ast.Constant)
                           and isinstance(part.value, str))
            if _looks_like_sql(text):
                yield node.lineno, text


def _python_files():
    for directory in SCANNED_DIRS:
        yield from sorted((ROOT / directory).rglob("*.py"))


def _executable(sql: str) -> list:
    """Each line with any `-- comment` tail removed.

    A typographic character INSIDE a SQL comment is inert — Postgres
    stops reading at the `--`. `get_roster_for_flight()`'s query has
    carried "-- role_assigned is NOT — under the pair model" since
    2026-08-21 and is perfectly valid. Flagging it would make this test
    demand that correct code be changed, which is how a hygiene check
    gets a blanket ignore added to it.

    So this checks what the database will actually parse, and the
    em dash that broke everything is caught precisely because it was
    standing WHERE the `--` should have been.
    """
    return [line.split("--", 1)[0] for line in sql.splitlines()]


@pytest.mark.parametrize(
    "path", list(_python_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_typographic_characters_in_sql(path):
    offences = []
    for lineno, sql in _sql_literals(path):
        for line in _executable(sql):
            for character, why in FORBIDDEN.items():
                if character in line:
                    offences.append(
                        f"{path.relative_to(ROOT)}: SQL literal near line "
                        f"{lineno} contains {why!r}: {line.strip()[:80]!r}"
                    )
    assert not offences, "\n".join(offences)


def test_the_scan_actually_finds_sql():
    """A hygiene test that scanned nothing would pass forever. The
    assignment service is the largest SQL surface in the codebase; if
    this stops finding queries there, the detector has broken rather
    than the code having been cleaned up."""
    found = list(_sql_literals(ROOT / "services" / "assignment_service.py"))
    assert len(found) >= 10, f"only {len(found)} SQL literals found"


def test_the_detector_catches_the_real_defect():
    """The exact string that broke 52 tests, run through the detector.
    Without this, a scan that silently stopped matching em dashes would
    look identical to a clean codebase."""
    corrupted = """
        SELECT r.roster_id, r.crew_id,
               — operating_position added 2026-08-21, additive: no
               r.operating_position
        FROM roster r
    """
    assert _looks_like_sql(corrupted)
    assert any(any(c in line for c in FORBIDDEN) for line in _executable(corrupted))

    # And the other direction, which is what keeps the check honest:
    # the SAME sentence behind a correct marker is inert, and must not
    # be flagged. This is the line that actually ships.
    valid = """
        SELECT r.roster_id,
               -- role_assigned is NOT — under the pair model a CPT can
               r.operating_position
        FROM roster r
    """
    assert _looks_like_sql(valid)
    assert not any(any(c in line for c in FORBIDDEN) for line in _executable(valid))
