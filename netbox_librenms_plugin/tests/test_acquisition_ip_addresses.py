"""IP address acquisition runs without a request and records what LibreNMS answered, over the local HTTP stub."""

import pytest

SERVER_KEY = "default"
DEVICE_ID = 4301
ADDRESSES_PATH = f"/api/v0/devices/{DEVICE_ID}/ip"
PORTS_PATH = f"/api/v0/devices/{DEVICE_ID}/ports"
DEVICE_PATH = f"/api/v0/devices/{DEVICE_ID}"
VRFS_PATH = "/api/v0/routing/vrf"


def _address(address, port_id=7001, prefix_length=24):
    return {"ipv4_address": address, "ipv4_prefixlen": prefix_length, "port_id": port_id}


def _port(port_id=7001, name="eth0", **extra):
    return {"port_id": port_id, "ifName": name, "ifDescr": name, **extra}


def _serve(server, *, addresses=None, ports=None, device=None):
    """Serve one device's IP rows, ports and device record; a None part is left unregistered."""
    if addresses is not None:
        server.register(ADDRESSES_PATH, {"status": "ok", "addresses": addresses})
    if ports is not None:
        server.register(PORTS_PATH, {"status": "ok", "ports": ports})
    if device is not None:
        server.register(DEVICE_PATH, {"status": "ok", "devices": [{"device_id": DEVICE_ID, **device}]})


def _device(name):
    from netbox_librenms_plugin.tests.conftest import make_device

    return make_device(name, librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})


def _acquire(subject):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource

    return acquire(subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES])


def _record(snapshot, part):
    from netbox_librenms_plugin.acquisition.registry import Resource

    return snapshot.record(Resource.IP_ADDRESSES, part)


def _ready(snapshot):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.resources import is_ready

    return is_ready(snapshot, Resource.IP_ADDRESSES)


def _paths(server):
    return [request["path"] for request in server.requests]


@pytest.mark.django_db
def test_the_snapshot_holds_the_rows_the_ports_and_the_device_record(live_librenms):
    from django.core.cache import cache
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import Outcome

    device = _device("acq-ip-obtained")
    rows = [_address("198.18.60.1"), _address("198.18.61.1", port_id="7002")]
    _serve(live_librenms.server, addresses=rows, ports=[_port(), _port(7002, "eth1")], device={"ip": " 198.18.60.1 "})
    keys_before = set(cache.iter_keys("*"))

    with CaptureQueriesContext(connection) as queries:
        snapshot = _acquire(device)

    assert snapshot.value(Resource.IP_ADDRESSES, "addresses") == rows
    assert snapshot.value(Resource.IP_ADDRESSES, "ports") == [_port(), _port(7002, "eth1")]
    # Acquisition keeps the source fact; the reader strips it.
    assert snapshot.value(Resource.IP_ADDRESSES, "device")["ip"] == " 198.18.60.1 "
    assert _record(snapshot, "vrfs").outcome is Outcome.NOT_APPLICABLE
    assert _ready(snapshot)
    assert VRFS_PATH not in _paths(live_librenms.server)
    assert set(cache.iter_keys("*")) == keys_before
    assert not [query for query in queries.captured_queries if not query["sql"].lstrip().upper().startswith("SELECT")]


@pytest.mark.django_db
def test_a_device_with_no_addresses_is_an_obtained_empty_list(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource

    _serve(live_librenms.server, ports=[_port()], device={"ip": ""})
    live_librenms.server.register(
        ADDRESSES_PATH, {"status": "error", "message": "Device acq-ip-empty does not have any IP addresses"}, status=404
    )

    snapshot = _acquire(_device("acq-ip-empty"))

    assert snapshot.value(Resource.IP_ADDRESSES, "addresses") == []
    assert _ready(snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("serve", "reason"),
    [
        (lambda server: server.register(ADDRESSES_PATH, {"status": "ok", "addresses": "nope"}), "malformed"),
        (lambda server: server.register(ADDRESSES_PATH, {"status": "error", "message": "boom"}, status=500), "boom"),
        (
            lambda server: server.register(ADDRESSES_PATH, {"status": "error", "message": "No device"}, status=404),
            "No device",
        ),
        (lambda server: server.register_disconnect(ADDRESSES_PATH), "could not be reached"),
    ],
    ids=["malformed-envelope", "endpoint-failure", "other-404", "disconnect"],
)
def test_an_unusable_address_answer_blocks_the_resource_and_is_never_an_empty_list(live_librenms, serve, reason):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import IncompleteSource, Outcome

    _serve(live_librenms.server, ports=[_port()], device={"ip": ""})
    serve(live_librenms.server)

    snapshot = _acquire(_device(f"acq-ip-{reason.split()[0].lower()}"))

    record = _record(snapshot, "addresses")
    assert record.outcome is Outcome.FAILED
    assert reason in record.reason
    assert not _ready(snapshot)
    with pytest.raises(IncompleteSource):
        snapshot.value(Resource.IP_ADDRESSES, "addresses")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bad_row",
    [
        {"ipv4_address": "198.18.62.1", "ipv4_prefixlen": 24},
        {"ipv4_address": "198.18.62.1", "ipv4_prefixlen": 24, "port_id": True},
        {"ipv4_address": "198.18.62.1", "ipv4_prefixlen": 24, "port_id": None},
        {"ipv4_address": "198.18.62.1", "ipv4_prefixlen": 24, "port_id": {"id": 1}},
        {"ipv4_address": "198.18.62.1", "ipv4_prefixlen": 24, "port_id": 7001.5},
        {"ipv4_address": "not-an-address", "ipv4_prefixlen": 24, "port_id": 7001},
        {"ipv4_address": "198.18.62.1/25", "ipv4_prefixlen": 24, "port_id": 7001},
        {"port_id": 7001, "address": "198.18.62.1/24"},
    ],
    ids=[
        "no-port-id",
        "bool-port-id",
        "null-port-id",
        "dict-port-id",
        "float-port-id",
        "unparseable",
        "prefix-conflict",
        "no-address-fields",
    ],
)
def test_one_unusable_address_row_makes_the_whole_answer_malformed(live_librenms, bad_row):
    from netbox_librenms_plugin.acquisition.snapshot import Outcome

    _serve(live_librenms.server, addresses=[_address("198.18.62.2"), bad_row], ports=[_port()], device={"ip": ""})

    snapshot = _acquire(_device("acq-ip-bad-row"))

    record = _record(snapshot, "addresses")
    assert record.outcome is Outcome.FAILED
    assert "IP address row" in record.reason
    assert not _ready(snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "serve_device",
    [
        lambda server: server.register(DEVICE_PATH, {"status": "error", "message": "boom"}, status=500),
        lambda server: server.register(DEVICE_PATH, {"status": "ok", "devices": []}),
        lambda server: server.register_disconnect(DEVICE_PATH),
    ],
    ids=["endpoint-failure", "malformed", "disconnect"],
)
def test_an_unusable_device_record_blocks_the_resource(live_librenms, serve_device):
    from netbox_librenms_plugin.acquisition.snapshot import Outcome

    _serve(live_librenms.server, addresses=[_address("198.18.63.1")], ports=[_port()])
    serve_device(live_librenms.server)

    snapshot = _acquire(_device("acq-ip-device-fails"))

    assert _record(snapshot, "addresses").obtained
    assert _record(snapshot, "device").outcome is Outcome.FAILED
    assert not _ready(snapshot)


@pytest.mark.django_db
def test_failed_ports_block_the_resource_and_skip_the_vrfs(live_librenms):
    from netbox_librenms_plugin.acquisition.snapshot import Outcome

    _serve(live_librenms.server, addresses=[_address("198.18.64.1")], device={"ip": ""})
    live_librenms.server.register(PORTS_PATH, {"status": "error", "message": "boom"}, status=500)

    snapshot = _acquire(_device("acq-ip-ports-fail"))

    assert _record(snapshot, "ports").outcome is Outcome.FAILED
    assert _record(snapshot, "vrfs").outcome is Outcome.NOT_REQUESTED
    assert VRFS_PATH not in _paths(live_librenms.server)
    assert not _ready(snapshot)


@pytest.mark.django_db
def test_the_vrfs_are_read_when_a_port_names_one(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource

    vrf = {"vrf_id": 3, "device_id": DEVICE_ID, "vrf_name": "blue", "mplsVpnVrfRouteDistinguisher": "65000:3"}
    other_device_vrf = {**vrf, "vrf_id": 4, "device_id": DEVICE_ID + 1}
    _serve(live_librenms.server, addresses=[_address("198.18.65.1")], ports=[_port(), _port(7009, ifVrf=3)], device={})
    live_librenms.server.register(VRFS_PATH, {"status": "ok", "vrfs": [vrf, other_device_vrf]})

    snapshot = _acquire(_device("acq-ip-vrfs"))

    assert snapshot.value(Resource.IP_ADDRESSES, "vrfs") == [vrf]
    assert _ready(snapshot)
    (vrf_request,) = [request for request in live_librenms.server.requests if request["path"] == VRFS_PATH]
    assert vrf_request["query"] == {"hostname": [str(DEVICE_ID)]}


@pytest.mark.django_db
@pytest.mark.parametrize("if_vrf", [None, 0, "0", "", "not-a-number", True, -3])
def test_a_port_value_that_is_no_vrf_id_reads_no_vrfs(live_librenms, if_vrf):
    from netbox_librenms_plugin.acquisition.snapshot import Outcome

    _serve(live_librenms.server, addresses=[_address("198.18.69.1")], ports=[_port(ifVrf=if_vrf)], device={})

    snapshot = _acquire(_device("acq-ip-no-vrf-id"))

    assert _record(snapshot, "vrfs").outcome is Outcome.NOT_APPLICABLE
    assert VRFS_PATH not in _paths(live_librenms.server)
    assert _ready(snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "serve_vrfs",
    [
        lambda server: server.register(VRFS_PATH, {"status": "error", "message": "boom"}, status=500),
        lambda server: server.register(VRFS_PATH, {"status": "ok", "vrfs": [{"vrf_id": 3, "vrf_name": "blue"}]}),
    ],
    ids=["endpoint-failure", "rows-without-a-device"],
)
def test_unusable_vrfs_block_the_resource_when_a_port_names_one(live_librenms, serve_vrfs):
    from netbox_librenms_plugin.acquisition.snapshot import Outcome

    _serve(live_librenms.server, addresses=[_address("198.18.66.1")], ports=[_port(ifVrf=3)], device={})
    serve_vrfs(live_librenms.server)

    snapshot = _acquire(_device("acq-ip-vrfs-fail"))

    assert _record(snapshot, "vrfs").outcome is Outcome.FAILED
    assert not _ready(snapshot)


@pytest.mark.django_db
def test_a_device_with_no_vrf_table_is_an_obtained_empty_list(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource

    _serve(live_librenms.server, addresses=[_address("198.18.67.1")], ports=[_port(ifVrf=3)], device={})
    live_librenms.server.register(VRFS_PATH, {"status": "error", "message": "VRFs do not exist"}, status=404)

    snapshot = _acquire(_device("acq-ip-no-vrf-table"))

    assert snapshot.value(Resource.IP_ADDRESSES, "vrfs") == []
    assert _ready(snapshot)


@pytest.mark.django_db
def test_a_virtual_machine_is_acquired_from_its_own_mapping(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_vm, map_device_to_librenms

    vm = map_device_to_librenms(make_vm("acq-ip-vm"), DEVICE_ID, server_key=SERVER_KEY)
    _serve(live_librenms.server, addresses=[_address("198.18.68.1")], ports=[_port()], device={"ip": ""})

    snapshot = _acquire(vm)

    assert snapshot.value(Resource.IP_ADDRESSES, "addresses") == [_address("198.18.68.1")]
    assert _ready(snapshot)
