"""
The sync tabs read IPAddress, VRF and interface rows only through the actor's view scope.

The scan fails on an ``IPAddress``/``VRF`` manager chain in the sync tab modules, an
``Interface``/``VMInterface`` manager chain in the IP tab modules, and an ``<x>.interfaces.<m>``
read of a subject's interfaces, when the chain does not pass through ``.restrict(<user>, "view")``.
A ``"change"`` restrict does not count for a read. The scope module ``sync_view_scope.py`` is not
scanned, and each other exception names one function, the first method of the chain, and its reason.

Limits: the scan reads direct chains and the simple local aliases they start (``qs =
IPAddress.objects`` is flagged where it is bound). It does not follow calls between functions,
``getattr``, a manager reached through another related object, or ``restricted_queryset()``.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent
SCANNED = ("assessment", "views/base", "views/object_sync", "views/sync", "tables")
MODELS = {"IPAddress", "VRF"}
# The IP tab modules also read interfaces only through the view scope.
IP_TAB_MODULES = {"views/base/ip_addresses_view.py", "views/sync/ip_addresses.py", "assessment/ip_addresses.py"}
IP_TAB_MODELS = {"Interface", "VMInterface"}
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
    (
        "views/sync/migrate.py",
        "_addresses_on",
        "filter",
    ): "every address that an interface move re-homes; the move reads each one back with saved_in_scope",
    (
        "views/sync/ip_addresses.py",
        "SyncIPAddressesView._build_interface_creation_state",
        "filter",
    ): "the locked binding catalog of the interface create: a port bound on a hidden interface must block the create",
    (
        "views/object_sync/devices.py",
        "DeviceInterfaceTableView.get_interfaces",
        "interfaces.all",
    ): "the attribution catalog of the interface tab; a row shows an interface only through the view-scoped ids",
    (
        "views/object_sync/vms.py",
        "VMInterfaceTableView.get_interfaces",
        "interfaces.all",
    ): "the attribution catalog of the interface tab; a row shows an interface only through the view-scoped ids",
    (
        "views/base/modules_view.py",
        "BaseModuleTableView._count_adoptable_template_interfaces",
        "interfaces.filter",
    ): "a short cut before the adoption count; the count shows a number, never an interface",
}


def is_view_restrict(call):
    """Return whether *call* is ``.restrict(user)`` or ``.restrict(user, "view")``."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "restrict"):
        return False
    action = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "action":
            action = keyword.value
    return action is None or (isinstance(action, ast.Constant) and action.value == "view")


def view_restricted_above(node, parents):
    """Return whether the chain that continues from *node* passes through a view restrict."""
    current = node
    while True:
        parent = parents.get(current)
        if isinstance(parent, ast.Attribute) and parent.value is current:
            current = parent
        elif isinstance(parent, ast.Call) and parent.func is current:
            if is_view_restrict(parent):
                return True
            current = parent
        else:
            return False


def _is_subject_interfaces(node, parents):
    """Return whether *node* is ``<x>.interfaces`` read as a related manager (``<x>.interfaces.<m>``)."""
    parent = parents.get(node)
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "interfaces"
        and isinstance(node.value, (ast.Name, ast.Attribute))
        and isinstance(parent, ast.Attribute)
        and parent.value is node
    )


def _unscoped_chains(tree, models=MODELS):
    """Return ``(function, first method, line)`` of each *models* manager or subject interface read without a view restrict."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if _is_subject_interfaces(node, parents):
            prefix = "interfaces."
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in MANAGERS
            and isinstance(node.value, ast.Name)
            and node.value.id in models
        ):
            prefix = ""
        else:
            continue
        parent = parents.get(node)
        method = parent.attr if isinstance(parent, ast.Attribute) and parent.value is node else None
        if not view_restricted_above(node, parents):
            found.append((_function_name(node, parents), method and prefix + method, node.lineno))
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


def _scan(source, models=MODELS):
    return [line for _function, _method, line in _unscoped_chains(ast.parse(source), models)]


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
        "obj.interfaces.first()",
        "self.object.interfaces.filter(name='eth0').exists()",
        "device.interfaces.restrict(user, 'change').all()",
    ],
    ids=[
        "filter",
        "all",
        "change-restrict",
        "change-keyword",
        "alias",
        "default-manager",
        "create",
        "subject-interfaces",
        "attribute-subject-interfaces",
        "subject-interfaces-change-restrict",
    ],
)
def test_an_unscoped_chain_is_found(source):
    assert _scan(source) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "Interface.objects.filter(device=obj)",
        "VMInterface.objects.get(pk=1)",
        "rows = Interface.objects\nrows.filter(device=obj)",
        "Interface.objects.restrict(user, 'change').filter(device=obj)",
    ],
    ids=["interface", "vm-interface", "alias", "change-restrict"],
)
def test_an_unscoped_interface_chain_is_found_in_the_ip_tab(source):
    assert _scan(source, MODELS | IP_TAB_MODELS) == [1]
    assert _scan(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "IPAddress.objects.restrict(user, 'view').filter(pk=1)",
        "VRF.objects.restrict(user).filter(pk=1)",
        "IPAddress.objects.restrict(user, action='view').values_list('pk', flat=True)",
        "qs = IPAddress.objects.restrict(request.user, 'view')\nqs.filter(pk=1)",
        "self.restricted_queryset(VRF).get(pk=1)",
        "Device.objects.filter(pk=1)",
        "obj.interfaces.restrict(user, 'view').first()",
        "scope.interfaces()",
        "Interface.objects.restrict(user, 'view').filter(device=obj)",
        "port.get('interfaces')",
    ],
    ids=[
        "view",
        "default-action",
        "view-keyword",
        "alias-of-scoped",
        "restricted-queryset",
        "other-model",
        "subject-interfaces-view",
        "scope-interfaces",
        "interface-view",
        "dict-key",
    ],
)
def test_a_view_scoped_chain_passes(source):
    assert _scan(source, MODELS | IP_TAB_MODELS) == []


def test_the_sync_tab_modules_read_addresses_vrfs_and_interfaces_through_the_view_scope():
    files = [path for folder in SCANNED for path in sorted((PACKAGE / folder).rglob("*.py"))]
    assert files, "the scan found no module"
    assert IP_TAB_MODULES <= {path.relative_to(PACKAGE).as_posix() for path in files}
    found = []
    allowed_seen = set()
    for path in files:
        relative = path.relative_to(PACKAGE).as_posix()
        models = MODELS | IP_TAB_MODELS if relative in IP_TAB_MODULES else MODELS
        for function, method, line in _unscoped_chains(ast.parse(path.read_text(encoding="utf-8")), models):
            if (relative, function, method) in ALLOWED:
                allowed_seen.add((relative, function, method))
            else:
                found.append(f"{relative}:{line} in {function}")

    assert found == [], "Read through SyncViewScope or .restrict(user, 'view'):\n" + "\n".join(found)
    # A stale exception would hide a new chain in that function, so each one must still be needed.
    assert allowed_seen == set(ALLOWED)
