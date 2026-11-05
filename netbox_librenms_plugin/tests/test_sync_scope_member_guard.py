"""
Guard: the sync tab views and tables read Virtual Chassis members only through the view scope.

A direct member read lists members the actor may not view, so its results reach a table, a select or
a template. ``sync_view_scope.ChassisScope`` holds every member for attribution and discloses one
only through ``owner(pk)``.

The scan recognizes direct chains and a local name bound to ``<x>.virtual_chassis``. It does not
follow calls into other functions, ``getattr`` or generic relations. It covers the views, the tables
and the assessment modules. A locked writer that must read every member to lock or re-check the
chassis is named in ``LOCKED_MEMBER_READS``; it discloses no member.
"""

import ast
from pathlib import Path

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
    """Return whether a manager chain holds ``.restrict(<user>, "view")``."""
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "restrict"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "view"
            ):
                return True
            node = func
        else:
            node = node.value
    return False


def _device_manager_chain(node):
    """Return whether *node* is a chain that starts at ``Device.objects``."""
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Attribute) and node.attr == "objects":
            return isinstance(node.value, ast.Name) and node.value.id == "Device"
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def _member_reads(tree):
    """Return ``(function name, line)`` of each direct member read in *tree*."""
    found = []
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        aliases = _chassis_aliases(function)
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and node.attr == "members" and _is_chassis(node.value, aliases):
                found.append((function.name, node.lineno))
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name == "get_virtual_chassis_members":
                    found.append((function.name, node.lineno))
                elif (
                    isinstance(func, ast.Attribute)
                    and any((keyword.arg or "").startswith("virtual_chassis") for keyword in node.keywords)
                    and _device_manager_chain(func.value)
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
    ):
        assert _scan(source), source


def test_the_scan_passes_the_scoped_reads():
    for source in (
        "def f(scope):\n    return scope.chassis.attribution_members\n",
        "def f(scope):\n    return [m.pk for m in scope.chassis.visible]\n",
        "def f(user, ids):\n    return Device.objects.restrict(user, 'view').filter(virtual_chassis_id__in=ids)\n",
        "def f(maps, port_id):\n    return maps.lag_members.get(port_id)\n",
        "def f(obj):\n    return obj.virtual_chassis_id is not None\n",
    ):
        assert not _scan(source), source
