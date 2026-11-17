"""
A write of an existing row must leave the row in the user's change scope, as NetBox's edit views do.

Each test gives a user a real constrained change grant, and makes a write that moves the row out of
the constraint. The write must roll back with its change record and its NetBox event, and the user
must get a refusal, not a server error.
"""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from core.models import ObjectChange
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from utilities.exceptions import PermissionsViolation

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_device_with_module_bays,
    make_interface,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
from netbox_librenms_plugin.transactions import update_existing_row

SERVER_KEY = "default"
MANAGED = {"name__startswith": "managed-"}


def _changes(obj):
    """Return the change records of *obj*."""
    return list(
        ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk
        )
    )


def _errors(response):
    """Return the ERROR messages of *response*; NetBox shows that level with the "danger" tag."""
    return [str(m) for m in messages.get_messages(response.wsgi_request) if m.level == messages.ERROR]


@contextmanager
def _netbox_request(user):
    """Set NetBox's request and event queue as its request processor does, and yield the queue."""
    from netbox.context import current_request, events_queue

    request = make_request("post", user=user)
    request.id = uuid4()
    request_token = current_request.set(request)
    queue_token = events_queue.set({})
    try:
        yield events_queue.get()
    finally:
        current_request.reset(request_token)
        events_queue.reset(queue_token)


def _site_writer(tag):
    """Return a user who may change only the sites named ``managed-*``, and one such site."""
    from dcim.models import Site

    user = grant(make_user_with_perms(f"{tag}-user", [], plugin_write=False), "change", Site, constraints=MANAGED)
    return user, Site.objects.create(name=f"managed-{tag}", slug=f"managed-{tag}")


# ---------------------------------------------------------------------------
# update_existing_row
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_write_that_leaves_the_callers_scope_rolls_back_with_its_change_record_and_event():
    from dcim.models import Site

    user, site = _site_writer("rb-helper")

    def rename(row):
        row.name = "other-rb-helper"

    with _netbox_request(user) as queue:
        with pytest.raises(PermissionsViolation):
            update_existing_row(Site.objects.restrict(user, "change").filter(pk=site.pk), rename)
        assert queue == {}

    assert Site.objects.get(pk=site.pk).name == "managed-rb-helper"
    assert _changes(site) == []


@pytest.mark.django_db
def test_a_row_that_needs_no_write_is_not_read_back():
    from dcim.models import Site

    user, site = _site_writer("rb-no-write")
    scope = Site.objects.restrict(user, "change").filter(pk=site.pk)

    with CaptureQueriesContext(connection) as queries:
        row = update_existing_row(scope, lambda row: False)

    assert row.pk == site.pk
    # The read-back is an EXISTS query on the scope.
    assert [query["sql"] for query in queries if query["sql"].startswith('SELECT 1 AS "a"')] == []


# ---------------------------------------------------------------------------
# The device field sync views
# ---------------------------------------------------------------------------


def _device_writer(tag, constraints, *extra):
    """Return a user who may view every device and change the devices that *constraints* select."""
    from dcim.models import Device

    user = make_user_with_perms(f"{tag}-user", [("view", Device), *extra])
    return grant(user, "change", Device, constraints=constraints)


def _post_field_sync(client, name, device):
    url = reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": device.pk})
    return client.post(url, {"server_key": SERVER_KEY})


@pytest.mark.django_db
def test_a_name_sync_out_of_the_change_scope_is_rolled_back(client, live_librenms, flushed_events):
    device = make_device("managed-rb-name", librenms_cf={SERVER_KEY: 7301})
    live_librenms.server.device_info_response(device_id=7301, hostname="other-rb-name")
    client.force_login(_device_writer("rb-name", MANAGED))

    response = _post_field_sync(client, "update_device_name", device)

    assert response.status_code == 302
    device.refresh_from_db()
    assert device.name == "managed-rb-name"
    assert _changes(device) == []
    assert flushed_events == []
    assert len(_errors(response)) == 1


@pytest.mark.django_db
def test_a_serial_sync_out_of_the_change_scope_is_rolled_back(client, live_librenms):
    device = make_device("rb-serial", librenms_cf={SERVER_KEY: 7302})
    live_librenms.server.device_info_response(device_id=7302, hostname=device.name, serial="RB-SERIAL")
    client.force_login(_device_writer("rb-serial", {"serial": ""}))

    response = _post_field_sync(client, "update_device_serial", device)

    assert response.status_code == 302
    device.refresh_from_db()
    assert device.serial == ""
    assert _changes(device) == []
    assert len(_errors(response)) == 1


@pytest.mark.django_db
def test_a_platform_sync_out_of_the_change_scope_is_rolled_back(client, live_librenms):
    from dcim.models import Platform

    device = make_device("rb-platform", librenms_cf={SERVER_KEY: 7303})
    platform = Platform.objects.create(name="rb-platform-os", slug="rb-platform-os")
    live_librenms.server.device_info_response(device_id=7303, hostname=device.name, os=platform.name)
    client.force_login(_device_writer("rb-platform", {"platform__isnull": True}, ("view", Platform)))

    response = _post_field_sync(client, "update_device_platform", device)

    assert response.status_code == 302
    device.refresh_from_db()
    assert device.platform_id is None
    assert _changes(device) == []
    assert len(_errors(response)) == 1


# ---------------------------------------------------------------------------
# The IP address sync
# ---------------------------------------------------------------------------


def _ip_sync_url(device):
    return reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses", kwargs={"object_type": "device", "pk": device.pk}
    )


def _seed_ip_rows(device, rows):
    """Write the IP snapshot that the sync view reads."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key

    cache.set(
        sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY),
        {
            "ip_addresses": rows,
            "mgmt_ip": "",
            "ports_by_id": {
                row["port_id"]: {"port_id": row["port_id"], "ifName": row["interface_name"]} for row in rows
            },
            "interface_name_field": "ifName",
        },
        timeout=300,
    )


def _ip_row(address, port_id, interface_name):
    return {
        "ip_address": address,
        "prefix_length": 24,
        "ip_with_mask": f"{address}/24",
        "port_id": port_id,
        "interface_name": interface_name,
    }


def _ip_writer(tag, *, device_change=None, ip_change=None):
    """Return a user who may sync IP addresses, with the given change scopes of devices and addresses."""
    from dcim.models import Device, Interface
    from ipam.models import VRF, IPAddress

    user = make_user_with_perms(
        f"{tag}-user", [("view", Device), ("view", Interface), ("view", VRF), ("add", IPAddress)]
    )
    user = grant(user, "change", Device, constraints=device_change)
    return grant(user, "change", IPAddress, constraints=ip_change)


# Transactional: the IP sync runs in its own outermost transaction, and keeps its events only on the commit.
@transactional_db_with_all_apps()
def test_a_primary_ip_out_of_the_device_change_scope_is_rolled_back(client, live_librenms, flushed_events):
    from ipam.models import IPAddress

    device = make_device("rb-primary", librenms_cf={SERVER_KEY: {"id": 7311}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    seed_mapping(interface, SERVER_KEY, own=9311)
    live_librenms.server.register(
        "/api/v0/devices/7311", {"status": "ok", "devices": [{"device_id": 7311, "ip": "198.18.73.11"}]}
    )
    row = _ip_row("198.18.73.11", 9311, interface.name)
    _seed_ip_rows(device, [row])
    client.force_login(_ip_writer("rb-primary", device_change={"primary_ip4__isnull": True}))

    response = client.post(
        _ip_sync_url(device),
        {
            "server_key": SERVER_KEY,
            "select": [row["ip_with_mask"]],
            f"vrf_{row['ip_with_mask']}": "",
            "set-primary-ip-toggle": "true",
        },
    )

    assert response.status_code == 302
    device.refresh_from_db()
    assert device.primary_ip4_id is None
    assert not IPAddress.objects.filter(address=row["ip_with_mask"]).exists()
    assert _changes(device) == []
    assert flushed_events == []
    assert len(_errors(response)) == 1


@pytest.mark.django_db
def test_a_confirmed_ip_change_out_of_the_ip_change_scope_is_rolled_back(client, live_librenms):
    from ipam.models import VRF, IPAddress

    blue = VRF.objects.create(name="rb-blue")
    device = make_device("rb-confirmed-ip", librenms_cf={SERVER_KEY: {"id": 7312}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    seed_mapping(target, SERVER_KEY, own=9312)
    current = make_interface(device, "Ethernet2", iface_type="1000base-t")
    address = IPAddress.objects.create(address="198.18.73.12/24", vrf=blue, assigned_object=current)
    row = _ip_row("198.18.73.12", 9312, target.name)
    _seed_ip_rows(device, [row])
    client.force_login(_ip_writer("rb-confirmed-ip", ip_change={"assigned_object_id": current.pk}))
    form = {"server_key": SERVER_KEY, "select": row["ip_with_mask"], f"vrf_{row['ip_with_mask']}": str(blue.pk)}
    conflict = client.post(_ip_sync_url(device), form, HTTP_HX_REQUEST="true").context["conflicts"][0]

    response = client.post(
        _ip_sync_url(device),
        {"server_key": SERVER_KEY, "force_conflict": row["ip_with_mask"], "conflict_intent": conflict["intent"]},
    )

    assert response.status_code == 302
    address.refresh_from_db()
    assert address.assigned_object == current
    assert _changes(address) == []
    assert len(_errors(response)) == 1


# ---------------------------------------------------------------------------
# The module move and the device type mapping
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_module_move_out_of_the_module_change_scope_is_rolled_back(client, settings):
    from dcim.models import Device, Module, ModuleBay

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_module_bay,
        make_module_type,
    )

    configure_default_librenms_server(settings)
    page = make_device_with_module_bays("rb-move-page", ["Slot 1"])
    source = make_device("rb-move-source")
    source_bay = make_module_bay(source, "Source Bay")
    moving = Module.objects.create(
        device=source, module_bay=source_bay, module_type=make_module_type("RB-MOVING"), serial="rb-moving"
    )
    user = make_user_with_perms("rb-move-user", [("view", Device), ("view", ModuleBay), ("delete", Module)])
    client.force_login(grant(user, "change", Module, constraints={"device__name": source.name}))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:move_module", kwargs={"pk": page.pk}),
        {
            "server_key": SERVER_KEY,
            "selected_device_id": "",
            "conflict_module_id": str(moving.pk),
            "target_bay_id": str(ModuleBay.objects.get(device=page, name="Slot 1").pk),
        },
    )

    assert response.status_code == 302
    moving.refresh_from_db()
    assert (moving.device_id, moving.module_bay_id) == (source.pk, source_bay.pk)
    assert _changes(moving) == []
    assert len(_errors(response)) == 1


@pytest.mark.django_db
def test_a_device_type_mapping_out_of_the_mapping_change_scope_is_rolled_back(client, live_librenms):
    from dcim.models import DeviceType

    from netbox_librenms_plugin.models import DeviceTypeMapping

    device = make_device("rb-mapping")
    current, other = (
        device.device_type,
        DeviceType.objects.create(manufacturer=device.device_type.manufacturer, model="RB Other", slug="rb-other"),
    )
    mapping = DeviceTypeMapping.objects.create(librenms_hardware="rb-hw", netbox_device_type=current)
    live_librenms.server.device_info_response(device_id=7321, hostname="rb-mapping-host", hardware="RB-HW")
    user = make_user_with_perms("rb-mapping-user", [("view", DeviceType)])
    client.force_login(grant(user, "change", DeviceTypeMapping, constraints={"netbox_device_type": current.pk}))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:add_device_type_mapping", kwargs={"device_id": 7321}),
        {"device_type_id": str(other.pk), "server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )

    assert (response.status_code, response["HX-Reswap"]) == (200, "none")
    mapping.refresh_from_db()
    assert mapping.netbox_device_type_id == current.pk
    assert _changes(mapping) == []


# ---------------------------------------------------------------------------
# write_interface_row: the IP tab writes an existing interface for an address
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_existing_interface_that_the_ip_tab_writes_out_of_the_change_scope_is_rolled_back(client, settings):
    from dcim.models import Device, Interface
    from django.core.cache import cache
    from ipam.models import IPAddress

    from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
    from netbox_librenms_plugin.tests.interface_sync_post_helpers import sync_port

    configure_default_librenms_server(settings)
    device = make_device("rb-ip-interface", librenms_cf={SERVER_KEY: {"id": 7331}})
    interface = make_interface(device, "eth0")
    cache.set(
        sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY),
        {
            # No interface name in the row: the address finds its interface through the port.
            "ip_addresses": [
                {"ip_address": "198.18.73.31", "prefix_length": 24, "ip_with_mask": "198.18.73.31/24", "port_id": 9331}
            ],
            "mgmt_ip": "",
            "ports_by_id": {9331: sync_port(9331, "eth0", alias="uplink")},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    user = make_user_with_perms(
        "rb-ip-interface-user",
        [("view", Device), ("view", Interface), ("add", Interface), ("add", IPAddress), ("change", IPAddress)],
    )
    client.force_login(grant(user, "change", Interface, constraints={"description": ""}))

    response = client.post(
        _ip_sync_url(device),
        {
            "server_key": SERVER_KEY,
            "create-missing-interfaces-toggle": "on",
            "select": ["198.18.73.31/24"],
            "vrf_198.18.73.31/24": "",
        },
    )

    assert response.status_code == 302
    interface.refresh_from_db()
    assert interface.description == ""
    assert not IPAddress.objects.filter(address="198.18.73.31/24").exists()
    assert _changes(interface) == []
    assert len(_errors(response)) == 1
