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
from netbox_librenms_plugin.tests.interface_sync_post_helpers import sync_port
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    submit_ip_selection,
)
from netbox_librenms_plugin.transactions import outside_change_scope, update_existing_row

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


def _seed_ip_rows(device, rows, *, mgmt_ip=None):
    """Store the IP snapshot of *rows* ``(address, port_id, interface name)``; *mgmt_ip* is the device record's IP."""
    seed_ip_snapshot(
        device,
        [ip_address_row(address, port_id) for address, port_id, _ in rows],
        [sync_port(port_id, name) for _, port_id, name in rows],
        device={} if mgmt_ip is None else {"ip": mgmt_ip},
        server_key=SERVER_KEY,
    )


def _assert_read_back_refusal(response, row):
    """Assert that the one error of *response* is the read-back refusal of *row*, so the write ran."""
    (error,) = _errors(response)
    assert str(outside_change_scope(row)) in error


def _ip_writer(tag, *, device_change=None, ip_change=None):
    """Return a user who may sync IP addresses, with the given change scopes of devices and addresses."""
    from dcim.models import Device, Interface
    from ipam.models import VRF, IPAddress

    user = make_user_with_perms(
        f"{tag}-user", [("view", Device), ("view", Interface), ("view", VRF), ("view", IPAddress), ("add", IPAddress)]
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
    _seed_ip_rows(device, [("198.18.73.11", 9311, interface.name)], mgmt_ip="198.18.73.11")
    row_id = "198.18.73.11/24"
    client.force_login(_ip_writer("rb-primary", device_change={"primary_ip4__isnull": True}))

    response = submit_ip_selection(client, device, [row_id], server_key=SERVER_KEY, **{"set-primary-ip-toggle": "true"})

    assert response.status_code == 302
    device.refresh_from_db()
    assert device.primary_ip4_id is None
    assert not IPAddress.objects.filter(address=row_id).exists()
    assert _changes(device) == []
    assert flushed_events == []
    _assert_read_back_refusal(response, device)


@pytest.mark.django_db
def test_a_confirmed_ip_change_out_of_the_ip_change_scope_is_rolled_back(client, live_librenms):
    from ipam.models import VRF, IPAddress

    blue = VRF.objects.create(name="rb-blue")
    device = make_device("rb-confirmed-ip", librenms_cf={SERVER_KEY: {"id": 7312}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    seed_mapping(target, SERVER_KEY, own=9312)
    current = make_interface(device, "Ethernet2", iface_type="1000base-t")
    address = IPAddress.objects.create(address="198.18.73.12/24", vrf=blue, assigned_object=current)
    _seed_ip_rows(device, [("198.18.73.12", 9312, target.name)])
    row_id = "198.18.73.12/24"
    client.force_login(_ip_writer("rb-confirmed-ip", ip_change={"assigned_object_id": current.pk}))
    conflict = submit_ip_selection(
        client,
        device,
        [row_id],
        server_key=SERVER_KEY,
        vrfs={row_id: str(blue.pk)},
        headers={"HTTP_HX_REQUEST": "true"},
    ).context["conflicts"][0]

    response = client.post(
        _ip_sync_url(device),
        {"server_key": SERVER_KEY, "force_conflict": row_id, "conflict_intent": conflict["intent"]},
    )

    assert response.status_code == 302
    address.refresh_from_db()
    assert address.assigned_object == current
    assert _changes(address) == []
    _assert_read_back_refusal(response, address)


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
    from ipam.models import IPAddress

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_virtual_chassis_members
    from netbox_librenms_plugin.tests.interface_sync_post_helpers import bound_interface

    configure_default_librenms_server(settings)
    # Member 3 has the same name on another port: the name match is ambiguous, so the create-missing
    # writer finds member 2 from the port name and writes its existing interface.
    _chassis, (device, member, sibling) = make_virtual_chassis_members("rb-ip-interface", count=3)
    seed_mapping(device, SERVER_KEY, own=7331)
    bound_interface(sibling, "eth2/1", 7900)
    interface = make_interface(member, "eth2/1", iface_type="1000base-t")
    seed_ip_snapshot(
        device,
        [ip_address_row("198.18.73.31", 9331)],
        [sync_port(9331, "eth2/1", alias="uplink")],
        server_key=SERVER_KEY,
    )
    user = make_user_with_perms(
        "rb-ip-interface-user",
        [
            ("view", Device),
            ("view", Interface),
            ("add", Interface),
            ("view", IPAddress),
            ("add", IPAddress),
            ("change", IPAddress),
        ],
    )
    client.force_login(grant(user, "change", Interface, constraints={"description": ""}))

    response = submit_ip_selection(
        client, device, ["198.18.73.31/24"], server_key=SERVER_KEY, **{"create-missing-interfaces-toggle": "on"}
    )

    assert response.status_code == 302
    interface.refresh_from_db()
    assert interface.description == ""
    assert not IPAddress.objects.filter(address="198.18.73.31/24").exists()
    assert _changes(interface) == []
    _assert_read_back_refusal(response, interface)


# ---------------------------------------------------------------------------
# _save_device: the import conflict actions
# ---------------------------------------------------------------------------


def _serve_import_device(server, device_id, hostname, serial):
    """Serve one LibreNMS device for the import validation."""
    from netbox_librenms_plugin.tests.import_server_helpers import librenms_device

    payload = {**librenms_device(device_id, hostname), "serial": serial}
    server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [payload]})
    for suffix in ("", "/all"):
        server.register(f"/api/v0/inventory/{device_id}{suffix}", {"status": "ok", "inventory": []})


def _post_conflict_action(client, device_id, device, action):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_conflict_action", kwargs={"device_id": device_id}),
        {
            "action": action,
            "existing_device_id": device.pk,
            "existing_device_type": "device",
            "server_key": SERVER_KEY,
            "use-sysname-toggle": "on",
            "strip-domain-toggle": "off",
        },
        HTTP_HX_REQUEST="true",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["sync_name", "link"])
def test_an_import_conflict_action_out_of_the_change_scope_is_rolled_back(
    client, live_librenms, flushed_events, action
):
    from netbox_librenms_plugin.server_mappings import read_mapping

    device = make_device(f"managed-rb-{action}", serial=f"RB-SN-{action}")
    _serve_import_device(live_librenms.server, 7341, f"other-rb-{action}", f"RB-SN-{action}")
    client.force_login(_device_writer(f"rb-{action}", MANAGED))

    response = _post_conflict_action(client, 7341, device, action)

    device.refresh_from_db()
    assert device.name == f"managed-rb-{action}"
    assert (response.status_code, response["HX-Reswap"]) == (200, "none")
    # The link saves the mapping with the name, so the refusal rolls the mapping back too.
    assert read_mapping(device).own_id(SERVER_KEY) is None
    assert _changes(device) == []
    assert flushed_events == []


@pytest.mark.django_db
def test_a_legacy_id_migration_out_of_the_change_scope_is_rolled_back(client, live_librenms, flushed_events):
    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test

    device = make_device("rb-migrate-legacy", serial="RB-SN-MIGRATE", librenms_cf=7342)
    _serve_import_device(live_librenms.server, 7342, "rb-migrate-legacy", "RB-SN-MIGRATE")
    # The grant names the stored mapping on purpose: the migration writes only the mapping.
    legacy_id = {"custom_field_data__librenms_id": 7342}  # nosemgrep: no-stored-mapping-access-orm
    client.force_login(_device_writer("rb-migrate-legacy", legacy_id))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_conflict_action", kwargs={"device_id": 7342}),
        {
            "action": "migrate_librenms_id",
            "existing_device_id": device.pk,
            "existing_device_type": "device",
            "server_key": SERVER_KEY,
            "force": "on",
        },
        HTTP_HX_REQUEST="true",
    )

    device.refresh_from_db()
    assert stored_mapping_for_test(device) == 7342
    assert (response.status_code, response["HX-Reswap"]) == (200, "none")
    assert _changes(device) == []
    assert flushed_events == []


# ---------------------------------------------------------------------------
# The move-to-winner views of a migrated donor
# ---------------------------------------------------------------------------


def _migrated_pair(tag):
    """Return a donor device that is marked as migrated to a winner device, and the winner."""
    from netbox_librenms_plugin.server_mappings import mark_migrated
    from netbox_librenms_plugin.tests.mapping_fixtures import apply_mapping_change

    donor, winner = make_device(f"{tag}-donor"), make_device(f"{tag}-winner")
    apply_mapping_change(donor, mark_migrated(donor, winner.pk, SERVER_KEY))
    donor.save(update_fields=["custom_field_data"])
    return donor, winner


def _post_move(client, name, *args):
    return client.post(
        reverse(f"plugins:netbox_librenms_plugin:{name}", args=args), {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true"
    )


def _assert_refused(response):
    assert (response.status_code, response["HX-Reswap"]) == (200, "none")
    assert "HX-Refresh" not in response.headers


@pytest.mark.django_db
def test_an_interface_move_out_of_the_interface_change_scope_is_rolled_back(client, flushed_events):
    from dcim.models import Device, Interface

    donor, _winner = _migrated_pair("rb-move-interface")
    interface = make_interface(donor, "Ethernet1")
    user = make_user_with_perms("rb-move-interface-user", [("view", Device), ("change", Device), ("view", Interface)])
    client.force_login(grant(user, "change", Interface, constraints={"device__name": donor.name}))

    response = _post_move(client, "interface_move_to_winner", interface.pk)

    _assert_refused(response)
    interface.refresh_from_db()
    assert interface.device_id == donor.pk
    assert _changes(interface) == []
    assert flushed_events == []


@pytest.mark.django_db
def test_an_ip_move_out_of_the_ip_change_scope_is_rolled_back(client, flushed_events):
    from dcim.models import Device, Interface
    from ipam.models import IPAddress

    from netbox_librenms_plugin.tests.conftest import make_ip

    donor, winner = _migrated_pair("rb-move-ip")
    donor_interface = make_interface(donor, "Ethernet1")
    make_interface(winner, "Ethernet1")
    address = make_ip("198.18.73.51/24", assigned_object=donor_interface)
    user = make_user_with_perms(
        "rb-move-ip-user", [("view", Device), ("change", Device), ("view", Interface), ("view", IPAddress)]
    )
    client.force_login(grant(user, "change", IPAddress, constraints={"assigned_object_id": donor_interface.pk}))

    response = _post_move(client, "ipaddress_move_to_winner", address.pk)

    _assert_refused(response)
    address.refresh_from_db()
    assert address.assigned_object == donor_interface
    assert _changes(address) == []
    assert flushed_events == []


@pytest.mark.django_db
def test_a_device_ip_transfer_out_of_the_device_change_scope_is_rolled_back(client, flushed_events):
    from dcim.models import Device

    from netbox_librenms_plugin.tests.conftest import ip_on

    donor, winner = _migrated_pair("rb-transfer")
    address = ip_on(winner, "198.18.73.61/24", "mgmt0")
    Device.objects.filter(pk=donor.pk).update(primary_ip4=address)
    user = make_user_with_perms("rb-transfer-user", [])
    # The winner may be changed only while it has no primary IPv4.
    scope = [{"name": donor.name}, {"name": winner.name, "primary_ip4__isnull": True}]
    client.force_login(grant(user, "change", Device, constraints=scope))

    response = _post_move(client, "device_transfer_ip", donor.pk, "primary4")

    _assert_refused(response)
    donor.refresh_from_db()
    winner.refresh_from_db()
    assert (donor.primary_ip4_id, winner.primary_ip4_id) == (address.pk, None)
    assert _changes(donor) == _changes(winner) == []
    assert flushed_events == []


@pytest.mark.django_db
def test_an_oob_link_out_of_the_change_scope_rolls_back_the_link_and_its_new_rows(
    client, live_librenms, flushed_events
):
    from dcim.models import Interface
    from ipam.models import IPAddress

    from netbox_librenms_plugin.server_mappings import read_mapping

    device = make_device("rb-oob", serial="RB-OOB")
    live_librenms.server.device_info_response(
        device_id=7351, hostname="rb-oob-idrac", hardware="iDRAC", os="idrac", serial=device.serial, ip="198.18.73.71"
    )
    live_librenms.server.vc_inventory_callable(7351, [], {})
    user = _device_writer(
        "rb-oob",
        {"oob_ip__isnull": True},
        *((action, model) for model in (Interface, IPAddress) for action in ("view", "add", "change")),
    )
    client.force_login(user)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_add_as_oob", args=[7351]),
        {
            "existing_device_id": str(device.pk),
            "server_key": SERVER_KEY,
            "oob_interface_id": "__new__",
            "oob_new_interface_name": "idrac",
        },
        HTTP_HX_REQUEST="true",
    )

    device.refresh_from_db()
    assert device.oob_ip_id is None
    assert not read_mapping(device).has_oob(SERVER_KEY)
    assert (response.status_code, response["HX-Reswap"]) == (200, "none")
    assert not device.interfaces.filter(name="idrac").exists()
    assert not IPAddress.objects.filter(address__net_host="198.18.73.71").exists()
    assert _changes(device) == []
    assert flushed_events == []


@pytest.mark.django_db
def test_a_merge_whose_winner_leaves_the_change_scope_rolls_back_the_donor_too(client, live_librenms, flushed_events):
    from dcim.models import Device

    from netbox_librenms_plugin.server_mappings import read_mapping
    from netbox_librenms_plugin.tests.conftest import ip_on

    winner = make_device("rb-merge-winner", librenms_cf={SERVER_KEY: {"id": 20}})
    donor = make_device("rb-merge-donor", serial="RB-MERGE", librenms_cf={SERVER_KEY: {"id": 10}})
    # The donor's OOB IP sits on a winner interface, so the merge moves it to the winner.
    oob_ip = ip_on(winner, "198.18.73.81/32", "mgmt0")
    Device.objects.filter(pk=donor.pk).update(oob_ip=oob_ip)
    live_librenms.server.device_info_response(
        device_id=99, hostname=winner.name, hardware=winner.device_type.model, os="ios", serial=donor.serial, ip=""
    )
    live_librenms.server.vc_inventory_callable(99, [], {})
    # The winner may be changed only while it has no OOB IP.
    scope = [{"name": donor.name}, {"name": winner.name, "oob_ip__isnull": True}]
    client.force_login(grant(make_user_with_perms("rb-merge-user", []), "change", Device, constraints=scope))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_merge_netbox_devices", kwargs={"device_id": 99}),
        {"server_key": SERVER_KEY, "winner_pk": str(winner.pk)},
        HTTP_HX_REQUEST="true",
    )

    donor.refresh_from_db()
    winner.refresh_from_db()
    assert (donor.oob_ip_id, winner.oob_ip_id) == (oob_ip.pk, None)
    assert read_mapping(donor).migrated_to(SERVER_KEY) is None
    assert (response.status_code, response["HX-Reswap"]) == (200, "none")
    assert _changes(donor) == _changes(winner) == []
    assert flushed_events == []


# ---------------------------------------------------------------------------
# The mark-built-in action of the modules tab
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_refused_built_in_mark_leaves_no_event_for_its_rolled_back_rows():
    from dcim.models import DeviceType

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.test_fixed_platform_built_in_units import (
        SLOT_1,
        ixr_device,
        mark,
        modal_form,
        user_with,
        written,
    )

    device = ixr_device("rb-mark-built-in", part_number="")
    # The part number write succeeds; the new rule is outside the add constraint, so the mark is refused.
    user = grant(user_with("rb-mark-built-in"), "change", DeviceType)
    user = grant(user, "add", InventoryIgnoreRule, constraints={"manufacturer__slug": "another-vendor"})
    fields = modal_form(device, SLOT_1, user=user)
    before = written(device)

    with _netbox_request(user) as queue:
        request = mark(device, fields, user=user)
        assert queue == {}

    assert written(device) == before
    assert _changes(device.device_type) == []
    assert [m.level for m in messages.get_messages(request)] == [messages.ERROR]
