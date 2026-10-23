"""Cable acquisition keeps each mapping and neighbour failure separate."""

import pytest

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes, is_ready
from netbox_librenms_plugin.acquisition.snapshot import IncompleteSource, Outcome
from netbox_librenms_plugin.server_mappings import MappingRole

HOST_ID = 4701
OOB_ID = 4702
NEIGHBOUR_ID = 4703
LINKS = [{"local_port_id": 1, "remote_device_id": NEIGHBOUR_ID, "remote_port_id": 8}]
PORTS = [{"port_id": 1, "ifName": "eth1"}]
SENSORS_PATH = "/api/v0/resources/sensors"


def _device(name, *, own=HOST_ID, oob=None):
    from netbox_librenms_plugin.tests.conftest import make_device, map_device_to_librenms

    return map_device_to_librenms(make_device(name), own, server_key="default", oob=oob)


def _serve(server, device_id=HOST_ID, *, links=()):
    server.register(f"/api/v0/devices/{device_id}/links", {"status": "ok", "links": list(links)})
    server.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": PORTS})


def _acquire(device, **options):
    from netbox_librenms_plugin.acquisition.coordinator import acquire

    return acquire(device, server_key="default", resources=[Resource.CABLES], **options)


@pytest.mark.django_db
def test_cables_acquire_source_rows_and_neighbours_without_serial_by_default(live_librenms):
    _serve(live_librenms.server, links=LINKS)
    _serve(live_librenms.server, NEIGHBOUR_ID)

    snapshot = _acquire(_device("acq-cable-source"))

    assert snapshot.value(Resource.CABLES, "links") == LINKS
    assert snapshot.value(Resource.CABLES, "ports") == PORTS
    assert snapshot.value(Resource.CABLES, f"neighbour_ports:{NEIGHBOUR_ID}") == PORTS
    assert snapshot.record(Resource.CABLES, "sensors").outcome is Outcome.NOT_REQUESTED
    assert SENSORS_PATH not in [r["path"] for r in live_librenms.server.requests]
    assert set(blocked_scopes(snapshot, Resource.CABLES)) == {"serial"}


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["malformed", "http", "disconnect"])
def test_a_failed_host_ports_response_blocks_host_rows(live_librenms, failure):
    _serve(live_librenms.server, links=LINKS)
    _serve(live_librenms.server, NEIGHBOUR_ID)
    path = f"/api/v0/devices/{HOST_ID}/ports"
    if failure == "disconnect":
        live_librenms.server.register_disconnect(path)
    else:
        live_librenms.server.register(path, {"status": "ok"}, status=500 if failure == "http" else 200)

    snapshot = _acquire(_device(f"acq-cable-{failure}"))

    assert "host" in blocked_scopes(snapshot, Resource.CABLES)
    assert not is_ready(snapshot, Resource.CABLES)
    with pytest.raises(IncompleteSource):
        snapshot.value(Resource.CABLES, "ports")


@pytest.mark.django_db
@pytest.mark.parametrize("links_status", [200, 404])
def test_genuine_empty_links_are_obtained(live_librenms, links_status):
    _serve(live_librenms.server)
    body = {"status": "ok", "links": []}
    if links_status == 404:
        body = {"status": "error", "message": "Device does not have any links"}
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/links", body, status=links_status)

    snapshot = _acquire(_device("acq-cable-empty"))

    assert snapshot.value(Resource.CABLES, "links") == []
    assert is_ready(snapshot, Resource.CABLES)


@pytest.mark.django_db
def test_failed_links_never_request_neighbour_ports(live_librenms):
    _serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/links", {"status": "error", "links": LINKS}, status=500)

    snapshot = _acquire(_device("acq-cable-no-neighbour"))

    assert not is_ready(snapshot, Resource.CABLES)
    assert f"/api/v0/devices/{NEIGHBOUR_ID}/ports" not in [r["path"] for r in live_librenms.server.requests]


@pytest.mark.django_db
def test_an_oob_failure_does_not_block_host_rows(live_librenms):
    _serve(live_librenms.server)
    _serve(live_librenms.server, OOB_ID)
    live_librenms.server.register(f"/api/v0/devices/{OOB_ID}/ports", {"status": "error"}, status=500)

    snapshot = _acquire(_device("acq-cable-oob-fail", oob={"id": OOB_ID, "type": "idrac"}))

    assert set(blocked_scopes(snapshot, Resource.CABLES)) == {"oob", "serial"}
    assert is_ready(snapshot, Resource.CABLES)


@pytest.mark.django_db
def test_oob_rows_stay_ready_when_host_ports_fail(live_librenms):
    _serve(live_librenms.server)
    _serve(live_librenms.server, OOB_ID)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "error"}, status=500)

    snapshot = _acquire(_device("acq-cable-host-fail", oob={"id": OOB_ID, "type": "idrac"}))

    assert set(blocked_scopes(snapshot, Resource.CABLES)) == {"host", "serial"}
    assert is_ready(snapshot, Resource.CABLES)


@pytest.mark.django_db
def test_an_oob_only_mapping_has_no_host_scope(live_librenms):
    _serve(live_librenms.server, OOB_ID)

    snapshot = _acquire(_device("acq-cable-oob-only", own=None, oob={"id": OOB_ID, "type": "idrac"}))

    assert snapshot.record(Resource.CABLES, "links").outcome is Outcome.NOT_APPLICABLE
    assert snapshot.value(Resource.CABLES, "ports", MappingRole.OOB) == PORTS
    assert is_ready(snapshot, Resource.CABLES)
    assert len(live_librenms.server.requests) == 2


@pytest.mark.django_db
def test_a_failed_neighbour_blocks_only_that_neighbour(live_librenms):
    links = LINKS + [{"remote_device_id": NEIGHBOUR_ID + 1, "local_port_id": 2}]
    _serve(live_librenms.server, links=links)
    _serve(live_librenms.server, NEIGHBOUR_ID + 1)
    live_librenms.server.register(f"/api/v0/devices/{NEIGHBOUR_ID}/ports", {"status": "error"}, status=500)

    snapshot = _acquire(_device("acq-cable-neighbour-fail"))

    assert set(blocked_scopes(snapshot, Resource.CABLES)) == {f"host:neighbour:{NEIGHBOUR_ID}", "serial"}
    assert is_ready(snapshot, Resource.CABLES)


@pytest.mark.django_db
def test_the_neighbour_budget_records_omitted_requests_as_failed(live_librenms, monkeypatch):
    from netbox_librenms_plugin.acquisition import resources

    _serve(live_librenms.server, links=LINKS)
    ticks = iter([0.0, 16.0])
    monkeypatch.setattr(resources, "monotonic", lambda: next(ticks))

    snapshot = _acquire(_device("acq-cable-budget"))

    record = snapshot.record(Resource.CABLES, f"neighbour_ports:{NEIGHBOUR_ID}")
    assert record.outcome is Outcome.FAILED
    assert "budget" in record.reason
    assert f"/api/v0/devices/{NEIGHBOUR_ID}/ports" not in [r["path"] for r in live_librenms.server.requests]


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["sensors", "resources"])
def test_serial_requests_keep_only_this_devices_raw_rows(live_librenms, field):
    _serve(live_librenms.server)
    row = {"device_id": HOST_ID, "sensor_type": "acsSerialPortTable", "sensor_id": "bad"}
    other = {"device_id": 999, "sensor_type": "temperature", "sensor_id": 6}
    live_librenms.server.register(SENSORS_PATH, {"status": "ok", field: [row, other]})

    snapshot = _acquire(_device("acq-cable-serial"), include_serial=True)

    assert snapshot.value(Resource.CABLES, "sensors") == [row]
    assert snapshot.record(Resource.CABLES, "sensors").body == {"status": "ok", field: [row]}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body, status",
    [({"status": "ok"}, 200), ({"status": "ok", "sensors": ""}, 200), ({"status": "error"}, 500)],
)
def test_an_invalid_serial_response_blocks_only_serial(live_librenms, body, status):
    _serve(live_librenms.server)
    live_librenms.server.register(SENSORS_PATH, body, status=status)

    snapshot = _acquire(_device("acq-cable-broken-serial"), include_serial=True)

    assert set(blocked_scopes(snapshot, Resource.CABLES)) == {"serial"}
    assert is_ready(snapshot, Resource.CABLES)


@pytest.mark.django_db
def test_serial_can_be_the_only_ready_scope(live_librenms):
    _serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "error"}, status=500)
    live_librenms.server.register(SENSORS_PATH, {"status": "ok", "sensors": []})

    snapshot = _acquire(_device("acq-cable-serial-only"), include_serial=True)

    assert set(blocked_scopes(snapshot, Resource.CABLES)) == {"host"}
    assert is_ready(snapshot, Resource.CABLES)


@pytest.mark.django_db
def test_an_oob_only_owner_with_failed_oob_rows_has_no_ready_scope(live_librenms):
    _serve(live_librenms.server, OOB_ID)
    live_librenms.server.register(f"/api/v0/devices/{OOB_ID}/ports", {"status": "error"}, status=500)

    snapshot = _acquire(_device("acq-cable-oob-only-fails", own=None, oob={"id": OOB_ID, "type": "idrac"}))

    assert not is_ready(snapshot, Resource.CABLES)


@pytest.mark.django_db
def test_a_neighbour_shared_by_host_and_oob_is_requested_once(live_librenms):
    _serve(live_librenms.server, links=LINKS)
    _serve(live_librenms.server, OOB_ID, links=LINKS)
    _serve(live_librenms.server, NEIGHBOUR_ID)

    snapshot = _acquire(_device("acq-cable-shared-neighbour", oob={"id": OOB_ID, "type": "idrac"}))

    assert snapshot.value(Resource.CABLES, f"neighbour_ports:{NEIGHBOUR_ID}", MappingRole.OOB) == PORTS
    paths = [r["path"] for r in live_librenms.server.requests]
    assert paths.count(f"/api/v0/devices/{NEIGHBOUR_ID}/ports") == 1


@pytest.mark.django_db
def test_host_and_oob_neighbours_share_one_budget(live_librenms, monkeypatch):
    from netbox_librenms_plugin.acquisition import resources

    _serve(live_librenms.server, links=LINKS)
    _serve(live_librenms.server, OOB_ID, links=[{"remote_device_id": NEIGHBOUR_ID + 1}])
    _serve(live_librenms.server, NEIGHBOUR_ID)
    ticks = iter([0.0, 0.0, 16.0])
    monkeypatch.setattr(resources, "monotonic", lambda: next(ticks))

    snapshot = _acquire(_device("acq-cable-shared-budget", oob={"id": OOB_ID, "type": "idrac"}))

    assert snapshot.record(Resource.CABLES, f"neighbour_ports:{NEIGHBOUR_ID}").obtained
    omitted = snapshot.record(Resource.CABLES, f"neighbour_ports:{NEIGHBOUR_ID + 1}", MappingRole.OOB)
    assert omitted.outcome is Outcome.FAILED
    assert "budget" in omitted.reason


@pytest.mark.django_db
def test_acquisition_writes_no_netbox_row_or_cache_key(live_librenms):
    from core.models import ObjectChange
    from dcim.models import Device
    from django.core.cache import cache

    device = _device("acq-cable-no-write")
    _serve(live_librenms.server)
    before = (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count(), set(cache.keys("*")))

    _acquire(device)

    assert (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count(), set(cache.keys("*"))) == before


@pytest.mark.django_db
def test_neighbour_budget_cannot_skip_the_oob_mappings_own_ports(live_librenms, monkeypatch):
    from netbox_librenms_plugin.acquisition import resources

    _serve(live_librenms.server, links=[{"remote_device_id": OOB_ID}])
    _serve(live_librenms.server, OOB_ID)
    ticks = iter([0.0, 16.0])
    monkeypatch.setattr(resources, "monotonic", lambda: next(ticks))

    snapshot = _acquire(_device("acq-cable-oob-neighbour", oob={"id": OOB_ID, "type": "idrac"}))

    assert snapshot.value(Resource.CABLES, "ports", MappingRole.OOB) == PORTS
    assert "oob" not in blocked_scopes(snapshot, Resource.CABLES)


@pytest.mark.django_db
@pytest.mark.parametrize("cables_first", [True, False])
def test_an_omitted_neighbour_is_still_requested_for_another_resources_owner(live_librenms, monkeypatch, cables_first):
    from netbox_librenms_plugin.acquisition import resources
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    _vc, (master, member) = make_virtual_chassis_members("acq-cable-budget-vc")
    seed_mapping(master, "default", own=HOST_ID)
    seed_mapping(member, "default", own=NEIGHBOUR_ID)
    _serve(live_librenms.server, links=LINKS)
    _serve(live_librenms.server, NEIGHBOUR_ID)
    live_librenms.server.register(f"/api/v0/devices/{NEIGHBOUR_ID}/ip", {"status": "ok", "addresses": []})
    live_librenms.server.register(
        f"/api/v0/devices/{NEIGHBOUR_ID}", {"status": "ok", "devices": [{"device_id": NEIGHBOUR_ID}]}
    )
    ticks = iter([0.0, 16.0])
    monkeypatch.setattr(resources, "monotonic", lambda: next(ticks))
    requested = [Resource.CABLES, Resource.IP_ADDRESSES]
    if not cables_first:
        requested.reverse()

    snapshot = acquire(member, server_key="default", resources=requested)

    assert is_ready(snapshot, Resource.IP_ADDRESSES)
    assert snapshot.value(Resource.IP_ADDRESSES, "ports") == PORTS
    assert snapshot.value(Resource.CABLES, f"neighbour_ports:{NEIGHBOUR_ID}") == PORTS
    paths = [request["path"] for request in live_librenms.server.requests]
    assert paths.count(f"/api/v0/devices/{NEIGHBOUR_ID}/ports") == 1


@pytest.mark.django_db
@pytest.mark.parametrize("disconnect", [False, True])
def test_an_attempted_failed_neighbour_is_not_requested_again_for_another_resource(live_librenms, disconnect):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    _vc, (master, member) = make_virtual_chassis_members("acq-cable-failed-vc")
    seed_mapping(master, "default", own=HOST_ID)
    seed_mapping(member, "default", own=NEIGHBOUR_ID)
    _serve(live_librenms.server, links=LINKS)
    path = f"/api/v0/devices/{NEIGHBOUR_ID}/ports"
    if disconnect:
        live_librenms.server.register_disconnect(path)
    else:
        live_librenms.server.register(path, {"status": "error"}, status=500)
    live_librenms.server.register(f"/api/v0/devices/{NEIGHBOUR_ID}/ip", {"status": "ok", "addresses": []})
    live_librenms.server.register(
        f"/api/v0/devices/{NEIGHBOUR_ID}", {"status": "ok", "devices": [{"device_id": NEIGHBOUR_ID}]}
    )

    snapshot = acquire(member, server_key="default", resources=[Resource.CABLES, Resource.IP_ADDRESSES])

    assert not is_ready(snapshot, Resource.IP_ADDRESSES)
    assert snapshot.record(Resource.IP_ADDRESSES, "ports").received_at is not None
    paths = [request["path"] for request in live_librenms.server.requests]
    assert paths.count(f"/api/v0/devices/{NEIGHBOUR_ID}/ports") == 1


@pytest.mark.django_db
def test_an_absent_host_and_invalid_oob_mapping_keep_distinct_response_outcomes(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import INVALID_OOB_REASON
    from netbox_librenms_plugin.acquisition.snapshot import SourceSnapshot

    snapshot = _acquire(_device("acq-cable-absent-invalid", own=None, oob={"id": "bad-id", "type": "idrac"}))

    for part in ("links", "ports"):
        host = snapshot.record(Resource.CABLES, part)
        oob = snapshot.record(Resource.CABLES, part, MappingRole.OOB)
        assert host.outcome is Outcome.NOT_APPLICABLE
        assert oob.outcome is Outcome.FAILED
        assert oob.reason == INVALID_OOB_REASON
        assert host.request.key != oob.request.key
    assert set(blocked_scopes(snapshot, Resource.CABLES)) == {"oob"}
    assert not is_ready(snapshot, Resource.CABLES)
    assert SourceSnapshot.from_payload(snapshot.to_payload()) == snapshot
    assert live_librenms.server.requests == []
