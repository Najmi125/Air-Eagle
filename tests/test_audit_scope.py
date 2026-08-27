"""Who is allowed to switch off an audit row, enforced statically.

`assign_pair_to_duty()` and `assign_crew_to_duty()` take
`audit_trials`, and passing False suppresses the REJECTED /
HELD_FOR_REVIEW audit row. That exists for exactly one caller: the
roster generator's internal candidate search, whose trials are options
considered rather than decisions made.

The obvious hazard is the one that has to be designed against rather
than hoped about — a page passing it would let a REAL rejection, a
decision by a real controller, go unrecorded. Three things stop that,
and this file is the second:

  1. The default is True. Silence is never what a caller gets by
     saying nothing.
  2. THIS FILE. No file outside the generator may pass it at all,
     and the generator may pass only the literal False. Checked by
     parsing the source, so it holds for a page that does not exist
     yet and cannot be satisfied by a copy-pasted `audit_trials=x`.
  3. It gates only trial outcomes. ASSIGNMENT_CREATED is written
     unconditionally, so no assignment can be created unaudited
     whatever anyone passes — verified below by reading the source of
     the write path rather than by trusting the review that wrote it.

Static, so it needs no database and runs everywhere — which matters,
because the incident that prompted this was invisible in exactly the
environments where the DB-dependent tests skip.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOLE_PERMITTED_CALLER = "services/roster_generator_service.py"
FLAG = "audit_trials"


def _python_files():
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(("venv", ".venv", "build", "tests/")):
            continue
        yield rel, path


def _calls_passing_flag(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == FLAG:
                    yield node, kw


def test_only_the_generator_may_suppress_a_trial_audit_row():
    """No page, service or script other than the generator passes the
    flag. A controller rejecting a pair through Roster or Control Room
    is a real decision and must stay audited — ADHOC_PAIR_REJECTED and
    the manual paths are not the generator's speculative search and
    have no business borrowing its exemption."""
    offenders = []
    for rel, path in _python_files():
        if rel == SOLE_PERMITTED_CALLER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, _ in _calls_passing_flag(tree):
            offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        f"{FLAG}= is passed outside {SOLE_PERMITTED_CALLER}: {offenders}. "
        f"Suppressing an audit row is for the generator's internal candidate "
        f"search ONLY — a rejection made by a person is a decision and stays "
        f"on the record. If a new caller genuinely needs this, that is an "
        f"operator decision about the regulatory record, not a test to edit."
    )


def test_the_generator_passes_only_a_literal_false():
    """Pinned to the literal, so it cannot become a variable that a
    refactor later threads through from somewhere else — which is how
    a flag stops meaning what its name says."""
    path = REPO / SOLE_PERMITTED_CALLER
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node, kw in _calls_passing_flag(tree):
        assert isinstance(kw.value, ast.Constant) and kw.value.value is False, (
            f"{SOLE_PERMITTED_CALLER}:{node.lineno} passes {FLAG}="
            f"{ast.unparse(kw.value)}, not the literal False"
        )
        found.append(node.lineno)

    # Both speculative loops: the fresh-pair search and the
    # one-seat-already-real fill. If this drops to one, a loop has
    # started writing a row per discarded option again.
    assert len(found) == 2, f"expected both candidate loops to pass it, got {found}"


def test_creating_an_assignment_is_audited_unconditionally():
    """The blast radius, checked rather than asserted in a comment.

    Even if the flag were somehow set on a real assignment, an
    assignment that IS created must still leave a row. So
    ASSIGNMENT_CREATED must not sit inside any `if` that mentions the
    flag — checked by walking the ancestor chain in the AST, which is
    what makes this survive a reindent or a refactor that a substring
    search would miss."""
    path = REPO / "services/assignment_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    created = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(kw.arg == "action_type"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "ASSIGNMENT_CREATED"
                for kw in node.keywords)
    ]
    assert created, "ASSIGNMENT_CREATED is not written at all — that is worse"

    for node in created:
        walker = node
        while walker in parents:
            walker = parents[walker]
            if isinstance(walker, ast.If) and FLAG in ast.unparse(walker.test):
                pytest.fail(
                    f"ASSIGNMENT_CREATED at line {node.lineno} is inside an "
                    f"`if` testing {FLAG} — a created assignment could then go "
                    f"unaudited, which is the one thing this parameter must "
                    f"never be able to do"
                )


def test_the_flag_defaults_to_auditing():
    """Default True on both functions. The direction matters more than
    the value: a caller written next year that knows nothing about this
    parameter gets the full audit trail, and only an explicit opt-out
    turns anything off."""
    path = REPO / "services/assignment_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        args = node.args
        defaults = dict(zip([a.arg for a in args.args][-len(args.defaults):],
                            args.defaults)) if args.defaults else {}
        if FLAG in defaults:
            value = defaults[FLAG]
            assert isinstance(value, ast.Constant) and value.value is True, (
                f"{node.name}() defaults {FLAG} to {ast.unparse(value)} — it "
                f"must default to True so silence never means unaudited"
            )
            checked.append(node.name)

    assert sorted(checked) == ["assign_crew_to_duty", "assign_pair_to_duty"], checked
