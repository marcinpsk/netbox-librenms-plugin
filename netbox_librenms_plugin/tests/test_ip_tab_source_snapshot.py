"""
The IP tab refreshes a source snapshot and assesses it at read time, for the user who reads it.

Each test refreshes the real tab from a loopback LibreNMS, then renders or syncs it through real
requests, with real NetBox rows and real permissions.
"""

import pytest
from dcim.models import Device, Interface
from django.contrib import messages
from django.contrib.messages import get_messages
from django.urls import reverse
from ipam.models import IPAddress

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _serve_librenms_ip_rows
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

SERVER_KEY = "default"
# The loopback helper serves the LibreNMS device as ID 42.
LIBRENMS_ID = 42


def _row(address, port_id, interface, **port_fields):
    return {
        "address": address,
        "prefix_length": 24,
        "port_id": port_id,
        "interface": interface,
        "port_fields": port_fields,
    }


def _refresh(client, device):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )


def _fragment(client, device):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.IP_ADDRESSES.value},
    )
    return client.get(url, {"server_key": SERVER_KEY})


def _sync(client, device, row_ids, **extra):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses", kwargs={"object_type": "device", "pk": device.pk}
    )
    return client.post(url, {"server_key": SERVER_KEY, "select": row_ids, **extra})


def _messages(response, level):
    return [str(message) for message in get_messages(response.wsgi_request) if message.level == level]


def _snapshot_available(device):
    status = SyncCacheConsistency(device).status(SERVER_KEY)[SyncTab.IP_ADDRESSES.value]
    return status["snapshot_available"]


def _device(name, *interfaces):
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
    return device, [make_interface(device, interface, iface_type="1000base-t") for interface in interfaces]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("rows", "break_source", "failed_path"),
    [
        (
            [_row("198.18.80.1", 7801, "eth1")],
            lambda server: server.register(f"/api/v0/devices/{LIBRENMS_ID}", {"status": "error"}, status=500),
            f"devices/{LIBRENMS_ID}",
        ),
        (
            [_row("198.18.80.1", 7801, "eth1")],
            lambda server: server.register(f"/api/v0/devices/{LIBRENMS_ID}", {"status": "ok", "devices": []}),
            f"devices/{LIBRENMS_ID}",
        ),
        (
            [_row("198.18.80.1", 7801, "eth1", ifVrf=3)],
            lambda server: server.register("/api/v0/routing/vrf", {"status": "error"}, status=500),
            "routing/vrf",
        ),
    ],
    ids=["device-record-fails", "device-record-malformed", "vrfs-fail-when-a-port-names-one"],
)
def test_a_refresh_without_every_needed_response_fails_and_drops_the_older_snapshot(
    client, live_librenms, rows, break_source, failed_path
):
    device, _interfaces = _device("ip-snap-refresh-fails", "eth1")
    client.force_login(make_superuser("ip-snap-refresh-fails-user"))
    _serve_librenms_ip_rows(live_librenms.server, rows, device_name=device.name)
    live_librenms.server.register("/api/v0/routing/vrf", {"status": "ok", "vrfs": []})
    assert _messages(_refresh(client, device), messages.SUCCESS) == ["IP address data refreshed successfully."]
    assert _snapshot_available(device)

    break_source(live_librenms.server)
    response = _refresh(client, device)

    assert response.status_code == 200
    assert _messages(response, messages.SUCCESS) == []
    (error,) = _messages(response, messages.ERROR)
    assert error.startswith(f"Failed to fetch IP addresses from LibreNMS: {failed_path} was not obtained")
    assert source_cache.read(device, Resource.IP_ADDRESSES, SERVER_KEY) is None
    assert not _snapshot_available(device)
    assert b"No IP address data loaded" in response.content


@pytest.mark.django_db
def test_a_device_with_no_addresses_stores_a_valid_snapshot_and_renders_an_empty_table(client, live_librenms):
    device, _interfaces = _device("ip-snap-empty")
    client.force_login(make_superuser("ip-snap-empty-user"))
    _serve_librenms_ip_rows(live_librenms.server, [], device_name=device.name)
    live_librenms.server.register(
        f"/api/v0/devices/{LIBRENMS_ID}/ip",
        {"status": "error", "message": f"Device {device.name} does not have any IP addresses"},
        status=404,
    )

    refresh = _refresh(client, device)
    fragment = _fragment(client, device)

    assert _messages(refresh, messages.SUCCESS) == ["IP address data refreshed successfully."]
    assert _snapshot_available(device)
    for response in (refresh, fragment):
        assert response.status_code == 200
        html = response.content.decode()
        assert 'id="librenms-ipaddress-table"' in html
        assert "No IP address data loaded" not in html


@pytest.mark.django_db
def test_the_render_and_the_sync_read_the_snapshot_only(client, live_librenms):
    """The sync sets the Primary IP from the device record of the snapshot, not from a later read."""
    device, (eth1,) = _device("ip-snap-no-live-read", "eth1")
    client.force_login(make_superuser("ip-snap-no-live-read-user"))
    _serve_librenms_ip_rows(
        live_librenms.server, [_row("198.18.81.1", 7811, "eth1")], device_name=device.name, management_ip="198.18.81.1"
    )
    assert _refresh(client, device).status_code == 200
    # LibreNMS now reports another management address; only a new refresh may pick it up.
    live_librenms.server.register(
        f"/api/v0/devices/{LIBRENMS_ID}", {"status": "ok", "devices": [{"device_id": LIBRENMS_ID, "ip": "198.18.81.9"}]}
    )
    requests_before = len(live_librenms.server.requests)

    fragment = _fragment(client, device)
    sync = _sync(client, device, ["198.18.81.1/24"], **{"set-primary-ip-toggle": "on"})

    assert fragment.status_code == 200
    assert 'data-mgmt-rows="[&quot;198.18.81.1/24&quot;]"' in fragment.content.decode()
    assert sync.status_code == 302
    assert _messages(sync, messages.SUCCESS) == [
        "Created IP addresses: 198.18.81.1/24",
        "Set as Primary IP: 198.18.81.1/24",
    ]
    device.refresh_from_db()
    assert device.primary_ip4 == IPAddress.objects.get(address="198.18.81.1/24", assigned_object_id=eth1.pk)
    assert len(live_librenms.server.requests) == requests_before


@pytest.mark.django_db
def test_the_sync_assesses_the_snapshot_for_the_user_who_syncs(client, live_librenms):
    """A refreshes; B may view eth1 only, so B's table and B's sync never resolve a row to eth2."""
    device, (eth1, eth2) = _device("ip-snap-two-users", "eth1", "eth2")
    rows = [_row("198.18.82.1", 7821, "eth1"), _row("198.18.82.2", 7822, "eth2")]
    _serve_librenms_ip_rows(live_librenms.server, rows, device_name=device.name)
    client.force_login(make_superuser("ip-snap-user-a"))
    refresh = _refresh(client, device)
    assert eth2.get_absolute_url() in refresh.content.decode()

    user_b = make_user_with_perms(
        "ip-snap-user-b", [("view", Device), ("add", IPAddress), ("change", IPAddress), ("view", IPAddress)]
    )
    user_b = grant(user_b, "view", Interface, constraints={"name": "eth1"})
    client.force_login(user_b)
    requests_before = len(live_librenms.server.requests)

    fragment = _fragment(client, device).content.decode()
    sync = _sync(client, device, ["198.18.82.1/24", "198.18.82.2/24"])

    assert eth1.get_absolute_url() in fragment
    assert eth2.get_absolute_url() not in fragment
    assert sync.status_code == 302
    assert _messages(sync, messages.SUCCESS) == ["Created IP addresses: 198.18.82.1/24"]
    assert any(
        "Skipped (no matching NetBox interface): 198.18.82.2/24" in text for text in _messages(sync, messages.WARNING)
    )
    assert IPAddress.objects.get(address="198.18.82.1/24").assigned_object == eth1
    assert not IPAddress.objects.filter(address="198.18.82.2/24").exists()
    assert len(live_librenms.server.requests) == requests_before
