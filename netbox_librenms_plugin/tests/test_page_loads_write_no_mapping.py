"""
A page load never writes a mapping.

The walk requests every GET route of a sync page, a sync tab and a status list, for an unlinked device
and VM that the LibreNMS fake matches by primary IP, and asserts that no domain row changed: no
mapping and no change record. A new GET route in those view modules joins the walk with no test edit.
"""

import pytest
from django.urls import get_resolver, reverse
from django.urls.resolvers import URLPattern, URLResolver
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    make_cluster,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import domain_state

SERVER_KEY = "default"
# The view modules whose GET routes render a sync page, a sync tab, a status list or a tab action's preview.
_WALKED_MODULES = (
    "netbox_librenms_plugin.views.base.",
    "netbox_librenms_plugin.views.object_sync.",
    "netbox_librenms_plugin.views.sync.",
    "netbox_librenms_plugin.views.status_check",
)
_ROUTE_ARGUMENTS = {"pk", "object_type", "tab"}
_MATCHES = {"device": ("192.0.2.10", 5501), "virtualmachine": ("192.0.2.20", 5502)}


def _walked_routes():
    """Return ``(route name, view class, route arguments)`` for each named GET route of the walked view modules."""
    routes = {}

    def arguments_of(pattern):
        return frozenset(pattern.regex.groupindex) | frozenset(getattr(pattern, "converters", ()))

    def walk(patterns, namespace, inherited):
        for entry in patterns:
            if isinstance(entry, URLResolver):
                namespace_of_entry = ":".join(filter(None, (namespace, entry.namespace)))
                walk(entry.url_patterns, namespace_of_entry, inherited | arguments_of(entry.pattern))
                continue
            view = getattr(entry.callback, "view_class", None)
            if isinstance(entry, URLPattern) and entry.name and view is not None and hasattr(view, "get"):
                if view.__module__.startswith(_WALKED_MODULES):
                    name = f"{namespace}:{entry.name}" if namespace else entry.name
                    routes[name] = (view, inherited | arguments_of(entry.pattern))

    walk(get_resolver().url_patterns, "", frozenset())
    return [(name, view, arguments) for name, (view, arguments) in sorted(routes.items())]


def _cases():
    cases = []
    for name, view, arguments in _walked_routes():
        if "object_type" in arguments:
            kinds = ("device", "virtualmachine")
        else:
            kinds = ("virtualmachine",) if getattr(view, "model", None) is VirtualMachine else ("device",)
        tabs = [tab.value for tab in SyncTab] if "tab" in arguments else [None]
        for kind in kinds:
            for tab in tabs:
                for scoped in (False, True):
                    suffix = f"-{tab}" if tab else ""
                    case_id = f"{name}-{kind}{suffix}-{'server_key' if scoped else 'default'}"
                    cases.append(pytest.param(name, view, arguments, kind, tab, scoped, id=case_id))
    return cases


@pytest.fixture
def unlinked(settings, librenms_server):
    """An unlinked device and VM, each with a primary IP that the LibreNMS fake matches."""
    from virtualization.models import VMInterface

    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    device = make_device("walk-device")
    device.primary_ip4 = make_ip("192.0.2.10/24", assigned_object=make_interface(device, "eth0"))
    device.save()
    vm = make_vm("walk-vm", make_cluster("walk-cluster"))
    vm.primary_ip4 = make_ip("192.0.2.20/24", assigned_object=VMInterface.objects.create(virtual_machine=vm, name="e0"))
    vm.save()
    for kind, (address, device_id) in _MATCHES.items():
        record = {"device_id": device_id, "hostname": f"walk-{kind}", "sysName": f"walk-{kind}"}
        librenms_server.register(f"/api/v0/devices/{address}", {"status": "ok", "devices": [record]})
        librenms_server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [record]})
    return {"device": device, "virtualmachine": vm}


def _query(view, subject, kind, scoped):
    """Return the query of one request; a status list shows nothing until a filter selects the subject."""
    query = {"server_key": SERVER_KEY} if scoped else {}
    if view.__module__ == "netbox_librenms_plugin.views.status_check":
        query.update({"site": subject.site_id} if kind == "device" else {"cluster": subject.cluster_id})
    return query


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "view", "arguments", "kind", "tab", "scoped"), _cases())
def test_a_get_writes_no_mapping_and_no_change_record(client, unlinked, name, view, arguments, kind, tab, scoped):
    assert arguments <= _ROUTE_ARGUMENTS, "Supply the arguments of the new GET route"
    subject = unlinked[kind]
    kwargs = {"pk": subject.pk, "object_type": kind, "tab": tab}
    url = reverse(name, kwargs={key: value for key, value in kwargs.items() if key in arguments})
    client.force_login(make_superuser("walk-reader"))
    before = domain_state()

    # A page without ?server_key resolves the installation default, which an unlinked object can be matched on.
    response = client.get(url, _query(view, subject, kind, scoped))

    assert response.status_code < 500
    assert domain_state() == before


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["device", "virtualmachine"])
def test_the_sync_page_proposes_the_match_that_it_does_not_store(client, unlinked, librenms_server, kind):
    subject = unlinked[kind]
    page = "device_librenms_sync" if kind == "device" else "vm_librenms_sync"
    client.force_login(make_superuser("walk-proposal-reader"))
    before = domain_state()

    response = client.get(reverse(f"plugins:netbox_librenms_plugin:{page}", args=[subject.pk]))

    assert response.status_code == 200
    assert domain_state() == before
    assert response.context["link"]["state"] == "match"
    assert response.context["link"]["librenms_id"] == _MATCHES[kind][1]
    assert response.context["link"]["token"]


def test_the_walk_covers_the_sync_pages_and_the_status_lists():
    names = {name for name, _view, _arguments in _walked_routes()}

    assert {
        "dcim:device_librenms_sync",
        "virtualization:virtualmachine_librenms_sync",
        "plugins:netbox_librenms_plugin:device_librenms_sync",
        "plugins:netbox_librenms_plugin:vm_librenms_sync",
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        "plugins:netbox_librenms_plugin:sync_cache_status",
        "plugins:netbox_librenms_plugin:sync_tab_filter",
        "plugins:netbox_librenms_plugin:device_status_list",
        "plugins:netbox_librenms_plugin:vm_status_list",
    } <= names
