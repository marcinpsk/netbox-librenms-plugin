"""
A view writes in ``atomic_with_events()`` or ``row_savepoint()``, not in a plain ``transaction.atomic()``.

NetBox queues an event when a row saves, and a rollback does not undo it, so a plain block that a
conflict aborts still sends the events of its rolled-back writes. The existing plain blocks are
pinned per file; a pin can only go down.
Limits: it counts ``transaction.atomic`` by that spelling in ``views/`` only, not an import alias.
"""

import ast
from pathlib import Path

import pytest

VIEWS = Path(__file__).resolve().parents[1] / "views"

# The plain blocks that exist now; moving one to atomic_with_events() lowers its pin.
PINNED = {
    "imports/actions.py": 7,
    "sync/cables.py": 5,
    "sync/device_fields.py": 2,
    "sync/interfaces.py": 5,
    "sync/ip_addresses.py": 3,
    "sync/migrate.py": 3,
    "sync/modules.py": 19,
    "sync/vlans.py": 1,
}


def plain_atomic_count(source):
    """Return how often *source* names ``transaction.atomic``, as a call, a context manager or a decorator."""
    return sum(
        isinstance(node, ast.Attribute)
        and node.attr == "atomic"
        and isinstance(node.value, ast.Name)
        and node.value.id == "transaction"
        for node in ast.walk(ast.parse(source))
    )


def test_no_view_adds_a_plain_atomic_block():
    found = {
        path.relative_to(VIEWS).as_posix(): count
        for path in sorted(VIEWS.rglob("*.py"))
        if (count := plain_atomic_count(path.read_text()))
    }
    assert found == PINNED


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("with transaction.atomic():\n    pass\n", 1),
        ("@transaction.atomic\ndef f():\n    pass\n", 1),
        ("with transaction.atomic(durable=True):\n    pass\n", 1),
        ("with atomic_with_events():\n    pass\n", 0),
        ("# transaction.atomic()\nx = 1\n", 0),
    ],
    ids=["context-manager", "decorator", "with-arguments", "isolated-events", "comment"],
)
def test_the_scan_counts_each_plain_atomic_form(source, expected):
    assert plain_atomic_count(source) == expected
