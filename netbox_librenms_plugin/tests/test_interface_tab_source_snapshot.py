"""
The interfaces tab refreshes a source snapshot and assesses it at read time, with the name rules stored now.

Each test refreshes the real tab from a loopback LibreNMS, then renders or syncs it through real requests,
with real NetBox rows and real permissions.
"""

import pytest
from dcim.models import Interface
from django.contrib import messages
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes
from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, map_device_to_librenms
from netbox_librenms_plugin.tests.interface_sync_post_helpers import bound_interface, post_interface_sync, sync_port
from netbox_librenms_plugin.tests.source_snapshot_helpers import interface_rows, port_pair

SERVER_KEY = "default"
HOST_ID = 4401
OOB_ID = 4402


def _port(port_id, name, if_type="ethernetCsmacd", **fields):
    return sync_port(port_id, name, if_type=if_type, **fields)


def _serve(server, ports, mappings=(), *, os_name="linux", device_id=HOST_ID):
    server.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": list(ports)})
    server.register(f"/api/v0/devices/{device_id}/port_stack", {"status": "ok", "mappings": list(mappings)})
    server.register(
        f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [{"device_id": device_id, "os": os_name}]}
    )


def _device(name, **mapping):
    return map_device_to_librenms(make_device(name), HOST_ID, server_key=SERVER_KEY, **mapping)


def _refresh(client, device):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_interface_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )


def _fragment(client, device):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.INTERFACES.value},
    )
    return client.get(url, {"server_key": SERVER_KEY})


def _messages(response, level):
    return [str(message) for message in get_messages(response.wsgi_request) if message.level == level]


def _status(device):
    return SyncCacheConsistency(device).status(SERVER_KEY)[SyncTab.INTERFACES.value]


def _refreshed(response):
    """Return whether a refresh answered with a success notice and no error."""
    return _messages(response, messages.ERROR) == [] and len(_messages(response, messages.SUCCESS)) == 1


def _rows(response):
    return {row.record["port_id"]: row.record for row in response.context["interface_sync"]["table"].rows}


@pytest.mark.django_db
def test_a_refresh_stores_source_facts_and_the_tab_assesses_them(client, live_librenms):
    device = _device("iface-snap-refresh")
    client.force_login(make_superuser("iface-snap-refresh-user"))
    ports = [
        _port(1, "eth1", vlans=[{"vlan": 10, "untagged": 1}], ifVlan="10"),
        _port(100, "bond0", "ieee8023adLag"),
    ]
    _serve(live_librenms.server, ports, [port_pair(1, 100)])

    response = _refresh(client, device)

    assert _refreshed(response)
    snapshot = source_cache.read(device, Resource.INTERFACES, SERVER_KEY)
    assert snapshot.value(Resource.INTERFACES, "ports") == ports
    rows = _rows(response)
    assert (rows[1]["untagged_vlan"], rows[1]["mode"], rows[1]["_source"]) == (10, "access", "main")
    # The aggregate has no VLANs of its own, so the LAG fill copies its member's.
    assert rows[100]["untagged_vlan"] == 10
    assert response.context["interface_sync"]["last_fetched"] == snapshot.finished_at
    assert _status(device)["snapshot_available"]
    # A render from the cache shows the same rows.
    assert _rows(_fragment(client, device)).keys() == rows.keys()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("break_source", "failed"),
    [
        (
            lambda server: server.register(f"/api/v0/devices/{HOST_ID}/port_stack", {"status": "error"}, status=500),
            f"devices/{HOST_ID}/port_stack",
        ),
        (
            lambda server: server.register(f"/api/v0/devices/{HOST_ID}", {"status": "ok", "devices": [{"os": ""}]}),
            f"devices/{HOST_ID}",
        ),
    ],
    ids=["port-stack-fails", "pairs-without-os"],
)
def test_a_refresh_without_the_host_relationship_data_fails_and_drops_the_older_snapshot(
    client, live_librenms, break_source, failed
):
    device = _device("iface-snap-blocked")
    client.force_login(make_superuser("iface-snap-blocked-user"))
    _serve(live_librenms.server, [_port(1, "eth1"), _port(100, "bond0", "ieee8023adLag")], [port_pair(1, 100)])
    assert _refreshed(_refresh(client, device))

    break_source(live_librenms.server)
    response = _refresh(client, device)

    assert response.status_code == 200
    assert _messages(response, messages.SUCCESS) == []
    (error,) = _messages(response, messages.ERROR)
    assert failed in error
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) is None
    assert not _status(device)["snapshot_available"]
    assert response.context["interface_sync"]["table"] is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "oob",
    [{"id": OOB_ID, "type": "idrac"}, {"id": "not-an-id", "type": "idrac"}],
    ids=["oob-ports-fail", "oob-id-corrupt"],
)
def test_an_oob_failure_keeps_the_host_rows_and_shows_the_oob_notice(client, live_librenms, oob):
    device = _device("iface-snap-oob-blocked", oob=oob)
    client.force_login(make_superuser("iface-snap-oob-blocked-user"))
    _serve(live_librenms.server, [_port(1, "eth1")])
    live_librenms.server.register(f"/api/v0/devices/{OOB_ID}/ports", {"status": "error"}, status=500)

    response = _refresh(client, device)

    assert _refreshed(response)
    assert list(_rows(response)) == [1]
    # Each notice names the source that blocks the OOB rows.
    snapshot = source_cache.read(device, Resource.INTERFACES, SERVER_KEY)
    (blocker,) = blocked_scopes(snapshot, Resource.INTERFACES)["oob"]
    (warning,) = _messages(response, messages.WARNING)
    assert blocker.path in warning and blocker.message in warning
    assert response.context["interface_sync"]["oob_incomplete"]
    # The banner belongs to the snapshot, so a later render from the cache names the source too.
    fragment = _fragment(client, device)
    assert fragment.context["interface_sync"]["oob_incomplete"]
    assert blocker.message in fragment.content.decode()
    assert _status(device)["snapshot_available"]


@pytest.mark.django_db
def test_a_failed_refresh_names_every_source_that_blocks_it(client, live_librenms):
    device = _device("iface-snap-every-blocker")
    client.force_login(make_superuser("iface-snap-every-blocker-user"))
    _serve(live_librenms.server, [_port(1, "eth1")])
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "error"}, status=500)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/port_stack", {"status": "error"}, status=500)

    (error,) = _messages(_refresh(client, device), messages.ERROR)

    assert f"devices/{HOST_ID}/ports" in error
    assert f"devices/{HOST_ID}/port_stack" in error


@pytest.mark.django_db
def test_oob_rows_join_the_host_rows_and_a_shared_mac_is_flagged(client, live_librenms):
    device = _device("iface-snap-oob", oob={"id": OOB_ID, "type": "idrac"})
    client.force_login(make_superuser("iface-snap-oob-user"))
    _serve(live_librenms.server, [_port(1, "eth0", ifPhysAddress="AA-BB-CC-DD-EE-01"), _port(2, "eth1")])
    live_librenms.server.register(
        f"/api/v0/devices/{OOB_ID}/ports",
        {"status": "ok", "ports": [_port(91, "idrac0", ifPhysAddress="aa:bb:cc:dd:ee:01")]},
    )

    response = _refresh(client, device)

    assert _refreshed(response) and _messages(response, messages.WARNING) == []
    assessed = {port["port_id"]: port for port in interface_rows(device)["ports"]}
    assert {port_id: port["_source"] for port_id, port in assessed.items()} == {1: "main", 2: "main", 91: "oob"}
    assert {port_id for port_id, port in assessed.items() if port.get("_dedup_conflict")} == {1, 91}
    assert not response.context["interface_sync"]["oob_incomplete"]


@pytest.mark.django_db
def test_the_sync_writes_the_rows_that_the_tab_assessed(client, live_librenms):
    from ipam.models import VLAN

    device = _device("iface-snap-sync")
    client.force_login(make_superuser("iface-snap-sync-user"))
    vlan = VLAN.objects.create(vid=10, name="iface-snap-sync-vlan")
    _serve(live_librenms.server, [_port(1, "eth1", vlans=[{"vlan": 10, "untagged": 1}], ifVlan="10")])
    _refresh(client, device)

    response = post_interface_sync(client, device, [1], htmx=False, exclude_columns=("mac_address",))

    interface = Interface.objects.get(device=device, name="eth1")
    assert (interface.mode, interface.untagged_vlan_id) == ("access", vlan.pk)
    assert _messages(response, messages.ERROR) == [] and _messages(response, messages.WARNING) == []


@pytest.mark.django_db
def test_a_pattern_edit_reaches_the_table_and_the_sync_without_a_refresh(client, live_librenms):
    device = _device("iface-snap-pattern")
    client.force_login(make_superuser("iface-snap-pattern-user"))
    member = bound_interface(device, "Gi0/1", 1, iface_type="1000base-t")
    aggregate = bound_interface(device, "Po1", 2, iface_type="lag")
    # A Cisco port-channel is not typed ieee8023adLag, so only a name rule makes it a LAG.
    _serve(
        live_librenms.server, [_port(1, "Gi0/1"), _port(2, "Po1", "propVirtual")], [port_pair(1, 2)], os_name="ztestos"
    )
    _refresh(client, device)
    assert _rows(_fragment(client, device))[1].get("librenms_lag_port_id") is None
    assert interface_rows(device)["port_stack_relationships"]["stacked_ports"] == {1: [2], 2: [1]}

    PortStackLagPattern.objects.create(librenms_os="ztestos", lag_name_pattern=r"^Po\d+$")

    # The rows cache keys on the name rules, so the table shows the new relationship at once.
    fragment = _fragment(client, device)
    assert _rows(fragment)[1]["librenms_lag_port_id"] == 2
    assert fragment.context["interface_sync"]["relationship_diagnostics"]["patterns"]["lag"] == [r"^Po\d+$"]
    # The sync reads the same assessment, so it sets the LAG that the table shows.
    post_interface_sync(client, device, [1], htmx=False)
    member.refresh_from_db()
    assert member.lag_id == aggregate.pk


@pytest.mark.django_db
def test_a_cables_refresh_leaves_the_interfaces_snapshot_as_it_was(client, live_librenms):
    device = _device("iface-snap-cables")
    client.force_login(make_superuser("iface-snap-cables-user"))
    _serve(live_librenms.server, [_port(1, "eth1")])
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/links", {"status": "ok", "links": []})
    _refresh(client, device)
    snapshot = source_cache.read(device, Resource.INTERFACES, SERVER_KEY)

    client.post(
        reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )

    # The cables tab fetched its own host ports instead of reading the interfaces snapshot.
    port_requests = [r for r in live_librenms.server.requests if r["path"] == f"/api/v0/devices/{HOST_ID}/ports"]
    assert len(port_requests) == 2
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) == snapshot
    assert _status(device)["snapshot_available"]
