"""The coordinator composes resource acquisitions; the web transport refreshes and reads them."""

import pytest

from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row
from netbox_librenms_plugin.tests.view_test_helpers import make_request

SERVER_KEY = "default"


def _serve_ip_and_vlans(server, device_id):
    server.register(f"/api/v0/devices/{device_id}/ip", {"status": "ok", "addresses": [ip_address_row("10.0.0.1", 7)]})
    server.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": [{"port_id": 7, "ifName": "e1"}]})
    server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [{"device_id": device_id, "ip": ""}]})
    server.register("/api/v0/resources/vlans", {"status": "ok", "vlans": [{"device_id": device_id, "vlan_vlan": 5}]})


def _paths(server):
    return [request["path"] for request in server.requests]


@pytest.mark.django_db
def test_one_call_acquires_several_resources_and_makes_each_request_once(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device("coord-two", librenms_cf={SERVER_KEY: {"id": 501}})
    _serve_ip_and_vlans(live_librenms.server, 501)

    # An iterator is consumed once, by the validation and the acquisition alike.
    snapshot = acquire(device, server_key=SERVER_KEY, resources=iter([Resource.IP_ADDRESSES, Resource.VLANS]))

    assert set(snapshot.resources) == {Resource.IP_ADDRESSES, Resource.VLANS}
    assert snapshot.value(Resource.VLANS, "vlans") == [{"device_id": 501, "vlan_vlan": 5}]
    assert snapshot.value(Resource.IP_ADDRESSES, "addresses") == [ip_address_row("10.0.0.1", 7)]
    paths = _paths(live_librenms.server)
    assert sorted(paths) == sorted(set(paths))


@pytest.mark.django_db
def test_a_virtual_chassis_member_acquires_its_own_ip_addresses(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import ObjectRef
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    _vc, (master, member) = make_virtual_chassis_members("coord-vc")
    seed_mapping(master, SERVER_KEY, own=601)
    seed_mapping(member, SERVER_KEY, own=602)
    _serve_ip_and_vlans(live_librenms.server, 602)

    snapshot = acquire(member, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES])

    # IP addresses belong to the subject, not to the VC sync owner.
    assert snapshot.resources[Resource.IP_ADDRESSES].owner == ObjectRef.of(member)
    assert "/api/v0/devices/602/ip" in _paths(live_librenms.server)
    assert "/api/v0/devices/601/ip" not in _paths(live_librenms.server)


@pytest.mark.django_db
def test_acquisition_changes_no_netbox_row(live_librenms):
    from core.models import ObjectChange
    from dcim.models import Device

    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device("coord-nowrite", librenms_cf={SERVER_KEY: {"id": 503}})
    _serve_ip_and_vlans(live_librenms.server, 503)
    before = (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count())

    acquire(device, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES, Resource.VLANS])

    assert (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count()) == before


@pytest.mark.django_db
def test_a_payload_of_another_plugin_version_reads_as_no_snapshot(live_librenms, monkeypatch):
    from django.core.cache import cache

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device("coord-version", librenms_cf={SERVER_KEY: {"id": 504}})
    _serve_ip_and_vlans(live_librenms.server, 504)
    assert source_cache.refresh(make_request("post"), device, Resource.VLANS, SERVER_KEY).error is None
    key = source_cache.snapshot_key(device, Resource.VLANS, SERVER_KEY)
    payload = cache.get(key)

    cache.set(key, {**payload, "plugin_version": "0.0.0"}, timeout=60)

    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None


@pytest.mark.django_db
def test_a_refresh_of_an_unmapped_subject_fails_and_drops_the_older_snapshot(live_librenms):
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

    from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping

    device = make_device("coord-unmapped", librenms_cf={SERVER_KEY: {"id": 505}})
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[])
    seed_stored_mapping(device, {SERVER_KEY: None}, save=True)

    result = source_cache.refresh(make_request("post"), device, Resource.VLANS, SERVER_KEY)

    assert result == source_cache.Refresh(None, "Device not found in LibreNMS.")
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_a_blocked_vlan_refresh_reports_the_failed_source_and_keeps_no_snapshot(client, live_librenms):
    from django.urls import reverse

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

    device = make_device("coord-vlan-blocked", librenms_cf={SERVER_KEY: {"id": 505}})
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[{"device_id": 505, "vlan_vlan": 9}])
    live_librenms.server.register("/api/v0/resources/vlans", {"status": "error", "message": "boom"}, status=500)
    client.force_login(make_superuser("coord-vlan-blocked-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_vlan_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "Failed to fetch VLANs: resources/vlans was not obtained (boom)" in response.content.decode()
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None


@pytest.mark.django_db
def test_resource_tabs_share_status_fragment_mutation_and_cleanup(client, settings, django_capture_on_commit_callbacks):
    from django.urls import reverse

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.sync_cache import (
        SyncCacheConsistency,
        SyncTab,
    )
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
    from netbox_librenms_plugin.tests.source_snapshot_helpers import (
        link_row,
        port_row,
        seed_cables_snapshot,
        seed_interfaces_snapshot,
        seed_modules_snapshot,
        seed_source_snapshot,
    )

    configure_default_librenms_server(settings)
    device = make_device("coord-mixed", librenms_cf={SERVER_KEY: {"id": 506}})
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[])
    interfaces = seed_interfaces_snapshot(device, [{"port_id": 1, "ifName": "eth1"}], server_key=SERVER_KEY)
    seed_cables_snapshot(
        device,
        [link_row(1, "eth2", remote_hostname="peer")],
        [port_row(1, "eth1", device_id=506)],
        server_key=SERVER_KEY,
    )
    seed_modules_snapshot(device, [], server_key=SERVER_KEY)
    coordinator = SyncCacheConsistency(device)

    status = coordinator.status(SERVER_KEY)
    assert all(status[tab]["snapshot_available"] for tab in ("vlans", "interfaces", "cables", "modules"))
    assert not status["ipaddresses"]["snapshot_available"]

    client.force_login(make_superuser("coord-mixed-user"))
    fragment = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.INTERFACES.value},
        ),
        {"server_key": SERVER_KEY},
    )
    assert fragment.status_code == 200
    assert [row.record["port_id"] for row in fragment.context["interface_sync"]["table"].rows] == [1]

    cable_fragment = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.CABLES.value},
        ),
        {"server_key": SERVER_KEY},
    )
    assert cable_fragment.status_code == 200
    assert [row.record["local_port_id"] for row in cable_fragment.context["cable_sync"]["table"].rows] == [1]

    # A sync of the interfaces tab keeps its own snapshot and clears the others.
    with django_capture_on_commit_callbacks(execute=True):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, SERVER_KEY)
    assert transition.completed
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) == interfaces
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None
    assert source_cache.read(device, Resource.CABLES, SERVER_KEY) is None
    assert source_cache.read(device, Resource.MODULES, SERVER_KEY) is None

    coordinator.invalidate_every_tab()

    status = coordinator.status(SERVER_KEY)
    assert not any(status[tab]["snapshot_available"] for tab in status)


@pytest.mark.django_db
def test_a_mapped_interface_is_not_a_device_acquisition_subject(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    subject = make_interface(make_device("unsupported-source-subject"), "eth0")
    seed_mapping(subject, SERVER_KEY, own=42)

    with pytest.raises(ValueError):
        acquire(subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES])

    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_a_resource_name_string_is_rejected_before_any_request(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    subject = make_device("unsupported-resource-type", librenms_cf={SERVER_KEY: {"id": 42}})

    with pytest.raises(ValueError):
        acquire(subject, server_key=SERVER_KEY, resources=[Resource.MODULES.value])

    assert live_librenms.server.requests == []


def _forbidden_inventory_imports(source):
    from netbox_librenms_plugin.tests.test_acquisition_imports import _imported_modules

    imported = set(_imported_modules(source))
    forbidden = {
        "netbox_librenms_plugin.import_utils.virtual_chassis",
        "netbox_librenms_plugin.librenms_api",
        "django.core.cache",
    }
    return {bad for bad in forbidden if any(name == bad or name.startswith(f"{bad}.") for name in imported)}


@pytest.mark.parametrize("module", ["acquisition/resources.py", "assessment/modules.py", "inventory_identity.py"])
def test_inventory_helpers_import_no_discovery_or_transport_module(module):
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / module
    assert not _forbidden_inventory_imports(path.read_text())


@pytest.mark.parametrize(
    "source,forbidden",
    [
        ("import django.core.cache", "django.core.cache"),
        ("from django.core import cache", "django.core.cache"),
        ("from netbox_librenms_plugin import librenms_api", "netbox_librenms_plugin.librenms_api"),
        (
            "from netbox_librenms_plugin.import_utils import virtual_chassis as discovery",
            "netbox_librenms_plugin.import_utils.virtual_chassis",
        ),
        ("from django.core.cache import cache", "django.core.cache"),
        ("from django.utils import timezone", None),
        ("import netbox_librenms_plugin.inventory_identity as identity", None),
    ],
)
def test_inventory_import_guard_checks_import_and_from_aliases(source, forbidden):
    assert _forbidden_inventory_imports(source) == ({forbidden} if forbidden else set())
