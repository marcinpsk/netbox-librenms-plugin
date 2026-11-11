"""
The package writes in ``atomic_with_events()`` or ``row_savepoint()``, not in a plain ``transaction.atomic``.

NetBox queues an event when a row saves, and a rollback does not undo it, so a plain block that rolls
back still sends the events of its rolled-back writes. Only the runner and the helper in
``transactions.py`` open a plain block. The scan skips ``migrations/`` and ``tests/``.
Limits: it finds ``transaction.atomic``, and ``atomic`` through any import alias, not through ``getattr``, an
assignment or a re-export.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
# The runner's attempt and the helper itself own the only plain blocks.
EXEMPT = {("transactions.py", "_run_attempt"), ("transactions.py", "atomic_with_events")}


def _aliases(tree):
    """Return the names that mean the ``django.db.transaction`` module, and the names that mean its ``atomic``."""
    # The usual module name counts without its import, so a missing import does not hide a block.
    modules, functions = {"transaction", "django.db.transaction"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "django.db":
            modules |= {alias.asname or alias.name for alias in node.names if alias.name == "transaction"}
        elif isinstance(node, ast.ImportFrom) and node.module == "django.db.transaction":
            functions |= {alias.asname or alias.name for alias in node.names if alias.name == "atomic"}
        elif isinstance(node, ast.Import):
            modules |= {alias.asname for alias in node.names if alias.name == "django.db.transaction" and alias.asname}
    return modules, functions


def plain_atomic_uses(source):
    """Return the enclosing callable of each plain ``atomic`` in *source*: a call, a decorator or another reference."""
    tree = ast.parse(source)
    modules, functions = _aliases(tree)
    uses = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # A decorator belongs to the callable that it decorates.
                visit(child, f"{scope}.{child.name}" if scope else child.name)
                continue
            if (
                isinstance(child, ast.Attribute) and child.attr == "atomic" and ast.unparse(child.value) in modules
            ) or (isinstance(child, ast.Name) and child.id in functions):
                uses.append(scope or "<module>")
            visit(child, scope)

    visit(tree, "")
    return uses


def test_no_package_module_opens_a_plain_atomic_block():
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(PACKAGE).as_posix()
        if name.startswith(("migrations/", "tests/")):
            continue
        found |= {(name, scope) for scope in plain_atomic_uses(path.read_text())}
    assert found - EXEMPT == set()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from django.db import transaction\ndef f():\n    with transaction.atomic():\n        pass\n", ["f"]),
        ("from django.db import transaction\n@transaction.atomic\ndef f():\n    pass\n", ["f"]),
        (
            "from django.db import transaction\nclass V:\n    def post(self):\n"
            "        with transaction.atomic(durable=True):\n            pass\n",
            ["V.post"],
        ),
        ("from django.db.transaction import atomic\ndef f():\n    with atomic():\n        pass\n", ["f"]),
        ("def f():\n    with transaction.atomic():\n        pass\n", ["f"]),
        ("from django.db import transaction as tx\n@tx.atomic()\ndef f():\n    pass\n", ["f"]),
        ("import django.db.transaction\ndef f():\n    with django.db.transaction.atomic():\n        pass\n", ["f"]),
        ("def f():\n    with atomic_with_events():\n        pass\n", []),
        ("@row_savepoint()\ndef f():\n    pass\n", []),
        ("from django.db import transaction\ndef f():\n    transaction.on_commit(g)\n", []),
        ("def f(migration):\n    return migration.atomic\n", []),
        ("# transaction.atomic()\nx = 1\n", []),
    ],
    ids=[
        "context-manager",
        "decorator",
        "method-with-arguments",
        "imported-function",
        "module-name-without-import",
        "module-alias",
        "dotted-module",
        "isolated-events",
        "row-savepoint",
        "other-transaction-function",
        "other-atomic-attribute",
        "comment",
    ],
)
def test_the_scan_finds_each_plain_atomic_form(source, expected):
    assert plain_atomic_uses(source) == expected
