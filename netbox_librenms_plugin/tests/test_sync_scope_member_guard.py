"""
Guard: the sync tab views and tables read Virtual Chassis members only through the view scope.

A direct member read lists members the actor may not view, so its results reach a table, a select or
a template. ``sync_view_scope.ChassisScope`` holds every member for attribution and discloses one
only through ``owner(pk)``.

The scan flags ``<x>.virtual_chassis.members``, ``get_virtual_chassis_members()``, a
``Device.objects`` chain filtered on ``virtual_chassis*`` and any ``VirtualChassis.objects`` chain,
unless the chain passes through ``.restrict(<user>, "view")``. It recognizes direct chains, a local
name bound to ``<x>.virtual_chassis`` and a local name bound to ``Device.objects``. It does not
follow calls into other functions, ``getattr`` or generic relations. It covers the views, the tables
and the assessment modules. A locked writer that must read every member to lock or re-check the
chassis is named in ``LOCKED_MEMBER_READS``; it discloses no member.
"""

import ast
from pathlib import Path

from netbox_librenms_plugin.tests.test_sync_view_scope_guard import is_view_restrict, view_restricted_above

PACKAGE = Path(__file__).resolve().parents[1]
SCANNED = ("views/base", "views/object_sync", "views/sync", "tables", "assessment")
# (file, function): writers that lock or re-check every member under the chassis lock.
LOCKED_MEMBER_READS = {
    ("views/sync/interfaces.py", "_lock_selected_device_targets"),
    ("views/sync/interfaces.py", "_lock_relationship_scope"),
    ("views/sync/ip_addresses.py", "_lock_interface_owner_scope"),
}
TEMPLATES = PACKAGE / "templates" / "netbox_librenms_plugin"


def _chassis_aliases(function):
    """Return the local names that a function binds to ``<x>.virtual_chassis``."""
    return {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "virtual_chassis"
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _is_chassis(node, aliases):
    return (isinstance(node, ast.Attribute) and node.attr == "virtual_chassis") or (
        isinstance(node, ast.Name) and node.id in aliases
    )


def _view_restricted(node):
    """Return whether the chain below *node* holds a view restrict."""
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Call):
            if is_view_restrict(node):
                return True
            node = node.func
        else:
            node = node.value
    return False


def _device_manager_chain(node, aliases=frozenset()):
    """Return whether *node* is a chain that starts at ``Device.objects`` or at a local name bound to it."""
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Attribute) and node.attr == "objects":
            return isinstance(node.value, ast.Name) and node.value.id == "Device"
        node = node.func if isinstance(node, ast.Call) else node.value
    return isinstance(node, ast.Name) and node.id in aliases


def _device_aliases(function):
    """Return the local names that a function binds to a ``Device.objects`` chain without a view restrict."""
    return {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and _device_manager_chain(node.value) and not _view_restricted(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _is_chassis_manager(node):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "objects"
        and isinstance(node.value, ast.Name)
        and node.value.id == "VirtualChassis"
    )


def _member_reads(tree):
    """Return ``(function name, line)`` of each direct member read in *tree*."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        aliases = _chassis_aliases(function)
        device_aliases = _device_aliases(function)
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "members"
                and _is_chassis(node.value, aliases)
                and not view_restricted_above(node, parents)
            ):
                found.append((function.name, node.lineno))
            elif _is_chassis_manager(node) and not view_restricted_above(node, parents):
                found.append((function.name, node.lineno))
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name == "get_virtual_chassis_members":
                    found.append((function.name, node.lineno))
                elif (
                    isinstance(func, ast.Attribute)
                    and any((keyword.arg or "").startswith("virtual_chassis") for keyword in node.keywords)
                    and _device_manager_chain(func.value, device_aliases)
                    and not _view_restricted(func.value)
                ):
                    found.append((function.name, node.lineno))
    return sorted(set(found), key=lambda item: item[1])


def _scan(source):
    return _member_reads(ast.parse(source))


def test_no_sync_view_or_table_reads_members_outside_the_view_scope():
    offenders = []
    seen = set()
    for directory in SCANNED:
        for path in sorted((PACKAGE / directory).rglob("*.py")):
            relative = str(path.relative_to(PACKAGE))
            for function, line in _member_reads(ast.parse(path.read_text())):
                if (relative, function) in LOCKED_MEMBER_READS:
                    seen.add((relative, function))
                    continue
                offenders.append(f"{relative}:{line} in {function}")
    assert not set(LOCKED_MEMBER_READS) - seen, f"Stale exceptions: {sorted(set(LOCKED_MEMBER_READS) - seen)}"
    assert not offenders, (
        "Read chassis members through SyncViewScope.chassis (attribution_members for matching, "
        f"visible or owner(pk) for anything shown): {offenders}"
    )


def test_no_sync_template_traverses_the_chassis_members():
    offenders = [
        str(path.relative_to(TEMPLATES))
        for path in TEMPLATES.rglob("*.html")
        if "virtual_chassis.members" in path.read_text()
    ]
    assert not offenders, f"Pass the members a template may show from the view scope: {offenders}"


def test_the_scan_flags_each_direct_member_read():
    for source in (
        "def f(obj):\n    return obj.virtual_chassis.members.all()\n",
        "def f(obj):\n    return obj.virtual_chassis.members.get(vc_position=1)\n",
        "def f(obj):\n    vc = obj.virtual_chassis\n    return list(vc.members.all())\n",
        "def f(obj):\n    return get_virtual_chassis_members(obj)\n",
        "def f(ids):\n    return Device.objects.filter(virtual_chassis_id__in=ids)\n",
        "def f(obj):\n    return Device.objects.filter(virtual_chassis=obj.virtual_chassis).first()\n",
        "def f(user, ids):\n    return Device.objects.restrict(user, 'change').filter(virtual_chassis_id__in=ids)\n",
        "def f(pk):\n    rows = Device.objects\n    return rows.filter(virtual_chassis_id=pk)\n",
        "def f(pk):\n    rows = Device.objects.all()\n    return rows.filter(virtual_chassis=pk).first()\n",
        "def f(pk):\n    return VirtualChassis.objects.get(pk=pk)\n",
        "def f(user, pk):\n    return VirtualChassis.objects.restrict(user, 'change').filter(pk=pk)\n",
        "def f():\n    chassis = VirtualChassis.objects\n    return chassis.all()\n",
        "def f(user, obj):\n    return obj.virtual_chassis.members.restrict(user, 'change').all()\n",
    ):
        assert _scan(source), source


def test_the_scan_passes_the_scoped_reads():
    for source in (
        "def f(scope):\n    return scope.chassis.attribution_members\n",
        "def f(scope):\n    return [m.pk for m in scope.chassis.visible]\n",
        "def f(user, ids):\n    return Device.objects.restrict(user, 'view').filter(virtual_chassis_id__in=ids)\n",
        "def f(maps, port_id):\n    return maps.lag_members.get(port_id)\n",
        "def f(obj):\n    return obj.virtual_chassis_id is not None\n",
        "def f(user, obj):\n    return obj.virtual_chassis.members.restrict(user, 'view').order_by('vc_position')\n",
        "def f(user, obj):\n    vc = obj.virtual_chassis\n    return list(vc.members.restrict(user).all())\n",
        "def f(user, pk):\n    rows = Device.objects.restrict(user, 'view')\n    return rows.filter(virtual_chassis_id=pk)\n",
        "def f(user, pk):\n    return VirtualChassis.objects.restrict(user, 'view').get(pk=pk)\n",
        "def f(pk):\n    rows = Device.objects\n    return rows.filter(pk=pk)\n",
    ):
        assert not _scan(source), source
