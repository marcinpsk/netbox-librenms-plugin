"""A write that a rollback undoes sends no NetBox event; a write that commits in the same request still sends its event."""

from contextlib import contextmanager

from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout, second_connection
from netbox_librenms_plugin.tests.mapping_fixtures import mapping_summary, seed_mapping
from netbox_librenms_plugin.tests.test_import_identity_replacement import secondary_server  # noqa: F401
from netbox_librenms_plugin.tests.test_platform_version_tracking_views import (
    librenms_server as platform_server,  # noqa: F401
)
from netbox_librenms_plugin.tests.test_server_mapping_edit import servers  # noqa: F401
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE

pytestmark = transactional_db_with_all_apps()


@contextmanager
def _commit_meets_a_lock_conflict(user):
    """Hold the row of *user*, so the commit checks the change record's user foreign key and meets a real 55P03."""
    from django.contrib.auth import get_user_model

    with second_connection() as other:
        lock_row(other, get_user_model(), user.pk)
        with lock_timeout(200):
            yield


def _texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_a_refused_remote_cable_sends_no_event_of_the_new_interface_or_the_cable(
    librenms_server, settings, flushed_events
):
    """The remote interface is saved, then the cable is saved and refused by the add scope: both roll back."""
    from dcim.models import Cable, Device, Interface

    from netbox_librenms_plugin.tests.test_cable_remote_matching import (
        TestCheckAndCreateTheRemoteEnd,
        _logged_in,
        _remote_create_url,
    )

    server_key, local_device, local_interface, remote_device, row_id = TestCheckAndCreateTheRemoteEnd._scenario(
        None, "events-remote-create", librenms_server, settings
    )
    user = make_user_with_perms(
        "events-remote-create",
        [("view", Device), ("view", Interface), ("change", Interface), ("add", Interface), ("change", Cable)],
    )
    user = grant(user, "add", Cable, constraints={"label__startswith": "permitted-"})
    flushed_events.clear()

    response = _logged_in(user).post(
        _remote_create_url(local_device),
        {"expected_local_id": local_interface.pk, "row_id": row_id, "server_key": server_key},
    )

    assert "Failed to create cable: You may not add this cable." in _texts(response)
    assert not Interface.objects.filter(device=remote_device).exists()
    assert not Cable.objects.exists()
    assert flushed_events == []


def test_a_rebind_whose_commit_meets_a_lock_conflict_sends_no_event(client, settings, flushed_events):
    from netbox_librenms_plugin.tests.test_interface_name_owner_rebind import (
        HOST_PORT,
        STALE_PORT,
        _binding,
        _bound_interface,
        _device,
        _port,
        _rebind,
        _seed,
    )

    configure_default_librenms_server(settings)
    device = _device("events-rebind")
    interface = _bound_interface(device, "eth0", STALE_PORT)
    _seed(device, [_port(HOST_PORT, "eth0")])
    user = make_superuser("events-rebind-user")
    client.force_login(user)
    flushed_events.clear()

    with _commit_meets_a_lock_conflict(user):
        response = _rebind(client, device, HOST_PORT)

    assert TRY_AGAIN_MESSAGE in _texts(response)
    assert _binding(interface) == STALE_PORT
    assert flushed_events == []


def test_an_address_whose_primary_ip_write_is_refused_sends_no_event_and_the_other_address_does(
    client, settings, flushed_events
):
    """The management address is saved, then NetBox refuses the device: only that row rolls back."""
    from dcim.models import Device
    from ipam.models import IPAddress

    from netbox_librenms_plugin.tests.test_ip_sync_primary_ip import _ip_url, _row, _seed, _sync_payload

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("events-ip-primary"), "default", own=4291)
    # A rack face without a rack: a state that NetBox refuses on the next full validation of the device.
    Device.objects.filter(pk=device.pk).update(face="front")
    rows = [_row("198.18.41.10", 9291, "Ethernet1"), _row("198.18.41.11", 9292, "Ethernet2")]
    for row in rows:
        seed_mapping(
            make_interface(device, row["interface_name"], iface_type="1000base-t"), "default", own=row["port_id"]
        )
    _seed(device, rows, management_ip="198.18.41.10")
    client.force_login(make_superuser("events-ip-primary-user"))
    flushed_events.clear()

    client.post(_ip_url(device), _sync_payload(rows))

    assert not IPAddress.objects.filter(address="198.18.41.10/24").exists()
    kept = IPAddress.objects.get(address="198.18.41.11/24")
    assert flushed_events == [("ipaddress", kept.pk)]


def test_a_vlan_create_whose_commit_meets_a_lock_conflict_sends_no_event(client, settings, flushed_events):
    """The VLAN's group savepoint is released, then the outer commit fails."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.vlan_intent_helpers import seed_vlans, sync, tab_tokens

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("events-vlan"), "default", own=4292)
    seed_vlans(device, [{"vlan_vlan": 2391, "vlan_name": "Rolled back"}])
    user = make_superuser("events-vlan-user")
    client.force_login(user)
    token = tab_tokens(client, device)[2391]
    flushed_events.clear()

    with _commit_meets_a_lock_conflict(user):
        response = sync(client, device, [token])

    assert TRY_AGAIN_MESSAGE in response.content.decode()
    assert not VLAN.objects.filter(vid=2391).exists()
    assert flushed_events == []


def test_a_family_move_that_a_dependent_refuses_sends_no_event_of_the_moved_interface(client, flushed_events):
    """The parent moves first; its child keeps a VLAN of the donor's site, so NetBox refuses the child."""
    from dcim.models import Site
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.test_migrate_views import _mark, _move_interface

    donor = make_device("events-move-donor")
    winner = make_device("events-move-winner")
    winner.site = Site.objects.create(name="Events move winner site", slug="events-move-winner-site")
    winner.save()
    _mark(donor, winner)
    parent = make_interface(donor, "Ethernet1")
    child = make_interface(donor, "Ethernet1.100", iface_type="virtual")
    child.parent = parent
    child.mode = "access"
    child.untagged_vlan = VLAN.objects.create(vid=100, name="Donor site VLAN", site=donor.site, status="active")
    child.save()
    client.force_login(make_superuser("events-move-user"))
    flushed_events.clear()

    response = _move_interface(client, parent)

    assert response.headers["HX-Reswap"] == "none"
    parent.refresh_from_db()
    assert parent.device == donor
    assert flushed_events == []


def test_a_module_type_update_that_leaves_the_change_scope_sends_no_event(client, settings, flushed_events):
    from dcim.models import Device, Interface, Module

    from netbox_librenms_plugin.tests.test_module_interface_types import _vc_member_type_mismatch

    configure_default_librenms_server(settings)
    page_device, member, module, interface = _vc_member_type_mismatch("events-type-scope")
    user = make_user_with_perms("events-type-scope", [("view", Device), ("view", Module)])
    client.force_login(grant(user, "change", Interface, constraints={"type": "1000base-t"}))
    flushed_events.clear()

    client.post(
        reverse("plugins:netbox_librenms_plugin:apply_module_interface_types", kwargs={"pk": page_device.pk}),
        {
            "server_key": "default",
            "selected_device_id": str(member.pk),
            "module_id": str(module.pk),
            "interface_id": [str(interface.pk)],
            f"current_type_{interface.pk}": "1000base-t",
            f"template_type_{interface.pk}": "10gbase-x-sfpp",
        },
    )

    interface.refresh_from_db()
    assert interface.type == "1000base-t"
    assert flushed_events == []


def test_a_mapping_edit_whose_commit_meets_a_lock_conflict_sends_no_event(client, servers, flushed_events):  # noqa: F811
    from netbox_librenms_plugin.tests.test_server_mapping_edit import _editor_forms

    target = make_device("events-edit-mapping")
    user = make_superuser("events-edit-mapping-user")
    client.force_login(user)
    action, fields = _editor_forms(client, target)["secondary"]
    flushed_events.clear()

    with _commit_meets_a_lock_conflict(user):
        response = client.post(action, {**fields, "own_id": "14801"})

    assert TRY_AGAIN_MESSAGE in _texts(response)
    target.refresh_from_db()
    assert mapping_summary(target) == {}
    assert flushed_events == []


def test_a_serial_sync_whose_commit_meets_a_lock_conflict_sends_no_event(
    client,
    secondary_server,  # noqa: F811
    flushed_events,
):
    from netbox_librenms_plugin.tests.test_import_identity_replacement import _post_action, _serve_secondary

    device = seed_mapping(make_device("events-sync-serial", serial="SN-EVENTS-OLD"), "secondary", own=52901)
    user = make_superuser("events-sync-serial-user")
    client.force_login(user)
    server = _serve_secondary(secondary_server, 52901, device.name, serial="SN-EVENTS-NEW")
    flushed_events.clear()

    with _commit_meets_a_lock_conflict(user):
        response = _post_action(client, 52901, device, "sync_serial", server=server)

    assert TRY_AGAIN_MESSAGE in response.content.decode()
    device.refresh_from_db()
    assert device.serial == "SN-EVENTS-OLD"
    assert flushed_events == []


def test_a_refused_platform_mapping_sends_no_event_and_the_assigned_platform_does(
    client,
    platform_server,  # noqa: F811
    flushed_events,
):
    """The platform and its assignment commit first; then the mapping is saved and refused by the add scope."""
    from dcim.models import Device, Manufacturer, Platform

    from netbox_librenms_plugin.models import PlatformMapping
    from netbox_librenms_plugin.tests.test_platform_version_tracking_views import _linked, _post, _tracking

    _tracking(False)
    device = _linked("events-platform", platform_server, 7491)
    user = make_user_with_perms(
        "events-platform",
        [("view", Device), ("change", Device), ("view", Platform), ("add", Platform), ("view", Manufacturer)],
    )
    client.force_login(grant(user, "add", PlatformMapping, constraints={"librenms_os": "eos"}))
    flushed_events.clear()

    _post(
        client,
        "create_and_assign_platform",
        device,
        {"platform_name": "Juniper Junos", "librenms_os": "junos", "create_mapping": "on"},
    )

    device.refresh_from_db()
    assert device.platform.name == "Juniper Junos"
    assert not PlatformMapping.objects.exists()
    assert sorted(flushed_events) == sorted([("platform", device.platform_id), ("device", device.pk)])


def test_a_chassis_that_the_master_rename_refuses_sends_no_event_and_the_imported_device_does(
    client, live_librenms, flushed_events
):
    """The chassis is saved, then the renamed master is too long for its column: only the chassis rolls back."""
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.tests.test_bulk_import_job_control import (
        _chassis,
        _libre_device,
        _prerequisites,
        _register_stack,
    )

    device_id = 96391
    prerequisites = _prerequisites("events-chassis")
    # The member pattern appends "-M1": 62 characters fit the name column, 65 do not.
    hostname = f"events-chassis-{'x' * 41}-{device_id}"
    assert len(hostname) == 62
    members = [_chassis(100, "SN-EVENTS-A", position=1), _chassis(200, "SN-EVENTS-B", position=2)]
    row = _libre_device(
        device_id,
        hostname,
        hardware=prerequisites["hardware"],
        serial="SN-EVENTS-A",
        location=prerequisites["location"],
    )
    _register_stack(live_librenms, device_id, row, members)
    client.force_login(make_superuser("events-chassis-user"))
    flushed_events.clear()

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "server_key": "default",
            "select": [str(device_id)],
            f"role_{device_id}": str(prerequisites["device_role_id"]),
        },
        HTTP_HX_REQUEST="true",
    )

    assert f"Failed to create VC for device {device_id}" in response.content.decode()
    imported = Device.objects.get(name=hostname, virtual_chassis__isnull=True)
    assert not VirtualChassis.objects.exists()
    assert flushed_events == [("device", imported.pk)]


def test_a_refused_provenance_tag_sends_no_event(client, flushed_events):
    """The settings form saves the new tag, then the add scope refuses it."""
    from extras.models import Tag

    from netbox_librenms_plugin.models import LibreNMSSettings

    settings, _ = LibreNMSSettings.objects.get_or_create()
    settings.cable_sync_tag = "events-missing-tag"
    settings.save(update_fields=["cable_sync_tag"])
    user = make_user_with_perms("events-tag-scope", [("add", Tag)], constraints={"name__startswith": "team-"})
    client.force_login(user)
    flushed_events.clear()

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:settings"),
        {
            "form_type": "cable_sync_settings",
            "cable_sync_tag": "events-outside-scope",
            "cable_sync_tag_color": "ff5722",
            "cable_sync_description": "Managed cable",
        },
    )

    assert "permission to create the cable provenance tag" in response.content.decode()
    assert not Tag.objects.filter(name="events-outside-scope").exists()
    assert flushed_events == []
