"""
A view writes in ``atomic_with_events()`` or ``row_savepoint()``, not in a plain ``transaction.atomic()``.

NetBox queues an event when a row saves, and a rollback does not undo it, so a plain block that a
conflict aborts still sends the events of its rolled-back writes. The existing plain blocks are
pinned by file and enclosing callable; a new one, or one that moves to another callable, fails, and
the list can only shrink. The corrected transaction boundaries must stay isolated.
Limits: it finds ``transaction.atomic`` and ``atomic_with_events`` by these spellings in ``views/``
only, not through an import alias.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

VIEWS = Path(__file__).resolve().parents[1] / "views"

# The plain blocks that exist now, by (file, enclosing callable): moving one to atomic_with_events() removes its pin.
PINNED = {}

# The whole-transaction boundaries whose events a conflict must drop.
ISOLATED = {
    ("imports/actions.py", "AddAsOOBView.post"),
    ("imports/actions.py", "DeviceConflictActionView.post"),
    ("imports/actions.py", "PromoteToHostView.post"),
    ("sync/modules.py", "UpdateModuleInterfaceView.post"),
}


def _is_plain_atomic(node):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "atomic"
        and isinstance(node.value, ast.Name)
        and node.value.id == "transaction"
    )


def _is_isolated(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "atomic_with_events"


def blocks(source):
    """Return the plain blocks per enclosing callable, and the callables that call ``atomic_with_events()``."""
    plain, isolated = Counter(), set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # A decorator belongs to the callable that it decorates.
                visit(child, f"{scope}.{child.name}" if scope else child.name)
                continue
            if _is_plain_atomic(child):
                plain[scope or "<module>"] += 1
            elif _is_isolated(child):
                isolated.add(scope or "<module>")
            visit(child, scope)

    visit(ast.parse(source), "")
    return plain, isolated


def _view_blocks():
    plain, isolated = {}, set()
    for path in sorted(VIEWS.rglob("*.py")):
        name = path.relative_to(VIEWS).as_posix()
        file_plain, file_isolated = blocks(path.read_text())
        plain.update({(name, scope): count for scope, count in file_plain.items()})
        isolated |= {(name, scope) for scope in file_isolated}
    return plain, isolated


def test_no_view_adds_or_moves_a_plain_atomic_block():
    plain, _isolated = _view_blocks()
    assert plain == PINNED


def test_the_corrected_boundaries_keep_their_events_isolated():
    _plain, isolated = _view_blocks()
    assert ISOLATED <= isolated


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f():\n    with transaction.atomic():\n        pass\n", {"f": 1}),
        ("@transaction.atomic\ndef f():\n    pass\n", {"f": 1}),
        (
            "class V:\n    def post(self):\n        with transaction.atomic(durable=True):\n            pass\n",
            {"V.post": 1},
        ),
        ("def f():\n    with atomic_with_events():\n        pass\n", {}),
        ("# transaction.atomic()\nx = 1\n", {}),
    ],
    ids=["context-manager", "decorator", "method-with-arguments", "isolated-events", "comment"],
)
def test_the_scan_finds_each_plain_atomic_form(source, expected):
    assert blocks(source)[0] == expected


def test_a_balanced_move_of_the_isolated_boundary_into_a_helper_fails():
    """A plain outer block keeps the events that the binder's released savepoint published, so D5-01 comes back."""
    guarded = (
        "class UpdateView:\n    def post(self):\n        with atomic_with_events():\n            bind()\n"
        "@transaction.atomic\ndef bind():\n    pass\n"
    )
    moved = (
        "class UpdateView:\n    def post(self):\n        with transaction.atomic():\n            bind()\n"
        "@row_savepoint()\ndef bind():\n    pass\n"
    )
    (guarded_plain, guarded_isolated), (moved_plain, moved_isolated) = blocks(guarded), blocks(moved)

    # The count of plain blocks stays the same; their callables and the isolated boundary do not.
    assert sum(guarded_plain.values()) == sum(moved_plain.values())
    assert guarded_plain != moved_plain
    assert "UpdateView.post" in guarded_isolated
    assert "UpdateView.post" not in moved_isolated
