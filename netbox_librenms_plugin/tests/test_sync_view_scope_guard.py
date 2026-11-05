"""
The sync tabs read IPAddress and VRF rows only through the actor's view scope.

The scan fails on an ``IPAddress``/``VRF`` manager chain in the sync tab modules that does not
pass through ``.restrict(<user>, "view")``. A ``"change"`` restrict does not count for a read. The
scope module ``sync_view_scope.py`` is not scanned, and each other exception names one function,
the first manager method of the chain, and its reason.

Limits: the scan reads direct chains and the simple local aliases they start (``qs =
IPAddress.objects`` is flagged where it is bound). It does not follow calls between functions,
``getattr``, a manager reached through a related object, or ``restricted_queryset()``.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent
SCANNED = ("assessment", "views/base", "views/object_sync", "views/sync", "tables")
MODELS = {"IPAddress", "VRF"}
MANAGERS = {"objects", "_default_manager", "_base_manager"}

# One entry per function and first manager method: the reason the unscoped chain is safe there.
ALLOWED = {
    (
        "views/sync/ip_addresses.py",
        "SyncIPAddressesView._classify_ip_change",
        "create",
    ): "creates the address; the writer reads it back with saved_in_scope for add and view",
    (
        "views/sync/ip_addresses.py",
        "_vrf_collision_refusal",
        "filter",
    ): "a VRF create collision needs every VRF; it names a VRF only when the user may view each one",
    (
        "views/sync/migrate.py",
        "_reconcile_donor_device_ip_fks",
        "select_for_update",
    ): "the locked designation move; the designation mechanism increment moves it behind the view scope",
}


def _is_view_restrict(call):
    """Return whether *call* is ``.restrict(user)`` or ``.restrict(user, "view")``."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "restrict"):
        return False
    action = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "action":
            action = keyword.value
    return action is None or (isinstance(action, ast.Constant) and action.value == "view")


def _unscoped_chains(tree):
    """Return ``(function, first method, line)`` of each IPAddress/VRF manager chain without a view restrict."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and node.attr in MANAGERS
            and isinstance(node.value, ast.Name)
            and node.value.id in MODELS
        ):
            continue
        parent = parents.get(node)
        method = parent.attr if isinstance(parent, ast.Attribute) and parent.value is node else None
        current, scoped = node, False
        while True:
            parent = parents.get(current)
            if isinstance(parent, ast.Attribute) and parent.value is current:
                current = parent
            elif isinstance(parent, ast.Call) and parent.func is current:
                scoped = scoped or _is_view_restrict(parent)
                current = parent
            else:
                break
        if not scoped:
            found.append((_function_name(node, parents), method, node.lineno))
    return found


def _function_name(node, parents):
    """Return the dotted name of the class and function that hold *node*, or ``<module>``."""
    names = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def _scan(source):
    return [line for _function, _method, line in _unscoped_chains(ast.parse(source))]


@pytest.mark.parametrize(
    "source",
    [
        "IPAddress.objects.filter(address='192.0.2.1/24')",
        "VRF.objects.all()",
        "IPAddress.objects.restrict(user, 'change').filter(pk=1)",
        "IPAddress.objects.restrict(user, action='change').first()",
        "rows = IPAddress.objects\nrows.filter(pk=1)",
        "IPAddress._default_manager.get(pk=1)",
        "IPAddress.objects.create(address='192.0.2.1/24')",
    ],
    ids=["filter", "all", "change-restrict", "change-keyword", "alias", "default-manager", "create"],
)
def test_an_unscoped_chain_is_found(source):
    assert _scan(source) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "IPAddress.objects.restrict(user, 'view').filter(pk=1)",
        "VRF.objects.restrict(user).filter(pk=1)",
        "IPAddress.objects.restrict(user, action='view').values_list('pk', flat=True)",
        "qs = IPAddress.objects.restrict(request.user, 'view')\nqs.filter(pk=1)",
        "self.restricted_queryset(VRF).get(pk=1)",
        "Device.objects.filter(pk=1)",
    ],
    ids=["view", "default-action", "view-keyword", "alias-of-scoped", "restricted-queryset", "other-model"],
)
def test_a_view_scoped_chain_passes(source):
    assert _scan(source) == []


def test_the_sync_tab_modules_read_ip_addresses_and_vrfs_through_the_view_scope():
    files = [path for folder in SCANNED for path in sorted((PACKAGE / folder).rglob("*.py"))]
    assert files, "the scan found no module"
    found = []
    allowed_seen = set()
    for path in files:
        relative = path.relative_to(PACKAGE).as_posix()
        for function, method, line in _unscoped_chains(ast.parse(path.read_text(encoding="utf-8"))):
            if (relative, function, method) in ALLOWED:
                allowed_seen.add((relative, function, method))
            else:
                found.append(f"{relative}:{line} in {function}")

    assert found == [], "Read IPAddress/VRF through SyncViewScope or .restrict(user, 'view'):\n" + "\n".join(found)
    # A stale exception would hide a new chain in that function, so each one must still be needed.
    assert allowed_seen == set(ALLOWED)
