"""The coordinator composes resource acquisitions; the web transport refreshes and reads them."""

import pytest

from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row

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
    assert source_cache.refresh(device, Resource.VLANS, SERVER_KEY).error is None
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

    device = make_device("coord-unmapped")
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[])

    result = source_cache.refresh(device, Resource.VLANS, SERVER_KEY)

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
def test_converted_and_unconverted_tabs_share_status_and_cleanup():
    from django.core.cache import cache

    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, sync_snapshot_key
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

    device = make_device("coord-mixed", librenms_cf={SERVER_KEY: {"id": 506}})
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[])
    cache.set(sync_snapshot_key(device, "ports", SERVER_KEY), {"ports": []}, timeout=300)
    coordinator = SyncCacheConsistency(device)

    status = coordinator.status(SERVER_KEY)
    assert status["vlans"]["snapshot_available"] and status["interfaces"]["snapshot_available"]
    assert not status["ipaddresses"]["snapshot_available"]

    coordinator.invalidate_every_tab()

    status = coordinator.status(SERVER_KEY)
    assert not status["vlans"]["snapshot_available"] and not status["interfaces"]["snapshot_available"]
