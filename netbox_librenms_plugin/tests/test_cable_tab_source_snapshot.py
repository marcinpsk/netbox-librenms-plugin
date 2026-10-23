"""Refresh and read cable sources through the real web adapter and loopback LibreNMS."""

import pytest
from django.contrib import messages
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, map_device_to_librenms

HOST_ID = 5401
OOB_ID = 5402
NEIGHBOUR_ID = 5403
SERVER_KEY = "default"


def _port(port_id=1, name="eth1", **fields):
    return {"port_id": port_id, "ifName": name, "ifDescr": name, **fields}


def _link(local_port_id=1, **fields):
    return {
        "id": local_port_id,
        "local_port_id": local_port_id,
        "local_port": "advertised-local",
        "remote_hostname": "peer.example.test",
        "remote_port": "eth2",
        "protocol": "lldp",
        **fields,
    }


def _serve(server, *, device_id=HOST_ID, links=None, ports=None):
    server.register(
        f"/api/v0/devices/{device_id}/links", {"status": "ok", "links": [_link()] if links is None else links}
    )
    server.register(
        f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": [_port()] if ports is None else ports}
    )


def _device(name, **mapping):
    return map_device_to_librenms(make_device(name), HOST_ID, server_key=SERVER_KEY, **mapping)


def _refresh(client, device):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )


def _fragment(client, device):
    return client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={
                "object_type": "device",
                "pk": device.pk,
                "tab": "cables",
            },
        ),
        {"server_key": SERVER_KEY},
    )


def _rows(response):
    table = response.context["cable_sync"]["table"]
    return [row.record for row in table.rows] if table is not None else []


def _messages(response, level):
    return [str(message) for message in get_messages(response.wsgi_request) if message.level == level]


@pytest.mark.django_db
def test_failed_host_ports_block_host_rows_and_remove_the_previous_snapshot(client, live_librenms):
    device = _device("cable-snapshot-host-ports")
    client.force_login(make_superuser("cable-snapshot-host-ports-user"))
    _serve(live_librenms.server)
    assert len(_rows(_refresh(client, device))) == 1
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "error"}, status=500)

    response = _refresh(client, device)

    assert response.status_code == 200
    assert _rows(response) == []
    assert source_cache.read(device, Resource.CABLES, SERVER_KEY) is None
    assert f"devices/{HOST_ID}/ports" in "; ".join(_messages(response, messages.ERROR))
    assert not SyncCacheConsistency(device).status(SERVER_KEY)[SyncTab.CABLES.value]["snapshot_available"]


@pytest.mark.django_db
def test_refresh_stores_raw_facts_and_reads_current_name_options(client, live_librenms):
    device = _device("cable-snapshot-facts")
    client.force_login(make_superuser("cable-snapshot-facts-user"))
    ports = [_port(ifDescr="Ethernet 1")]
    _serve(live_librenms.server, ports=ports)

    response = _refresh(client, device)

    assert _rows(response)[0]["local_port"] == "eth1"
    snapshot = source_cache.read(device, Resource.CABLES, SERVER_KEY)
    assert snapshot is not None
    assert snapshot.value(Resource.CABLES, "links") == [_link()]
    assert snapshot.value(Resource.CABLES, "ports") == ports
    assert response.context["cable_sync"]["last_fetched"] == snapshot.finished_at
    from netbox_librenms_plugin.views.base.cables_view import read_cable_assessment

    assert (
        read_cable_assessment(response.wsgi_request, device, SERVER_KEY, "ifDescr")["links"][0]["local_port"]
        == "Ethernet 1"
    )
    requests_before = list(live_librenms.server.requests)
    assert len(_rows(_fragment(client, device))) == 1
    assert live_librenms.server.requests == requests_before


@pytest.mark.django_db
def test_failed_oob_ports_block_oob_rows_and_keep_host_rows(client, live_librenms):
    device = _device("cable-snapshot-oob", oob={"id": OOB_ID, "type": "idrac"})
    client.force_login(make_superuser("cable-snapshot-oob-user"))
    _serve(live_librenms.server)
    _serve(live_librenms.server, device_id=OOB_ID, links=[_link(9)], ports=[_port(9, "oob9")])
    live_librenms.server.register(f"/api/v0/devices/{OOB_ID}/ports", {"status": "error"}, status=500)

    response = _refresh(client, device)

    assert [row["local_port_id"] for row in _rows(response)] == [1]
    assert f"devices/{OOB_ID}/ports" in "; ".join(_messages(response, messages.WARNING))
    assert f"devices/{OOB_ID}/ports" in _fragment(client, device).content.decode()


@pytest.mark.django_db
def test_failed_neighbour_ports_block_only_that_neighbours_rows(client, live_librenms):
    device = _device("cable-snapshot-neighbours")
    client.force_login(make_superuser("cable-snapshot-neighbours-user"))
    _serve(live_librenms.server, links=[_link(1, remote_device_id=NEIGHBOUR_ID), _link(2)], ports=[_port(), _port(2)])
    live_librenms.server.register(f"/api/v0/devices/{NEIGHBOUR_ID}/ports", {"status": "error"}, status=500)

    response = _refresh(client, device)

    assert [row["local_port_id"] for row in _rows(response)] == [2]
    assert f"devices/{NEIGHBOUR_ID}/ports" in "; ".join(_messages(response, messages.WARNING))
    assert f"devices/{NEIGHBOUR_ID}/ports" in _fragment(client, device).content.decode()


@pytest.mark.django_db
def test_low_privilege_refresh_makes_no_sensor_request_and_removes_old_serial_rows(client, live_librenms):
    from dcim.models import ConsoleServerPort, Device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    device = _device("cable-snapshot-serial-gate")
    ConsoleServerPort.objects.create(device=device, name="ttyS1")
    admin = make_superuser("cable-snapshot-serial-admin")
    low_privilege = make_user_with_perms("cable-snapshot-serial-viewer", [("view", Device)])
    _serve(live_librenms.server, links=[], ports=[])
    live_librenms.server.register(
        "/api/v0/resources/sensors",
        {
            "status": "ok",
            "sensors": [
                {
                    "device_id": HOST_ID,
                    "sensor_id": 101,
                    "sensor_type": "acsSerialPortTable",
                    "sensor_index": "acsSerialPortTableStatus.1",
                    "sensor_descr": "serial-peer Status",
                    "sensor_deleted": 0,
                }
            ],
        },
    )
    client.force_login(admin)
    assert [row["_source"] for row in _rows(_refresh(client, device))] == ["serial"]
    before = len(live_librenms.server.requests)

    client.force_login(low_privilege)
    response = _refresh(client, device)

    assert response.status_code == 200
    assert all(r["path"] != "/api/v0/resources/sensors" for r in live_librenms.server.requests[before:])
    client.force_login(admin)
    assert _rows(_fragment(client, device)) == []


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_expired_picker_requires_refresh_without_another_acquisition(client, live_librenms, method):
    device = _device("cable-snapshot-picker-expired")
    client.force_login(make_superuser("cable-snapshot-picker-expired-user"))
    _serve(live_librenms.server)
    response = getattr(client, method)(
        reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[device.pk]),
        {"server_key": SERVER_KEY, "row_id": "1"},
    )
    assert response.status_code == 409
    assert live_librenms.server.requests == []


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["host", "link", "unlink"])
def test_snapshot_binding_tracks_host_and_oob_mapping_changes(client, live_librenms, change):
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping, seed_stored_mapping_row

    device = _device("cable-snapshot-binding", oob={"id": OOB_ID, "type": "idrac"})
    client.force_login(make_superuser("cable-snapshot-binding-user"))
    _serve(live_librenms.server)
    _serve(live_librenms.server, device_id=OOB_ID, links=[], ports=[])
    _refresh(client, device)
    assert source_cache.read(device, Resource.CABLES, SERVER_KEY) is not None
    mapping = {"id": HOST_ID, "oob": {"id": OOB_ID, "type": "idrac"}}
    if change == "host":
        mapping["id"] = HOST_ID + 10
    elif change == "link":
        mapping["oob"]["id"] = OOB_ID + 10
    else:
        del mapping["oob"]
    seed_stored_mapping(device, {SERVER_KEY: mapping})
    seed_stored_mapping_row(device, {SERVER_KEY: mapping})

    assert source_cache.read(device, Resource.CABLES, SERVER_KEY) is None
    before = list(live_librenms.server.requests)
    assert _fragment(client, device).status_code == 404
    assert live_librenms.server.requests == before
