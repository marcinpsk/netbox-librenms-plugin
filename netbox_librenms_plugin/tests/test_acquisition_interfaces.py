"""Interface acquisition runs without a request and records what LibreNMS answered, over the local HTTP stub."""

import pytest

SERVER_KEY = "default"
HOST_ID = 4301
OOB_ID = 4302
PORTS_PATH = f"/api/v0/devices/{HOST_ID}/ports"
STACK_PATH = f"/api/v0/devices/{HOST_ID}/port_stack"
DEVICE_PATH = f"/api/v0/devices/{HOST_ID}"
OOB_PORTS_PATH = f"/api/v0/devices/{OOB_ID}/ports"

HOST_PORTS = [
    {"port_id": 1, "ifName": "eth1", "ifType": "ethernetCsmacd", "vlans": [{"vlan": 10, "untagged": 1}]},
    {"port_id": 100, "ifName": "Po1", "ifType": "ieee8023adLag"},
]
OOB_PORTS = [{"port_id": 91, "ifName": "idrac0"}]


def _device(name, **mapping):
    from netbox_librenms_plugin.tests.conftest import make_device, map_device_to_librenms

    return map_device_to_librenms(make_device(name), HOST_ID, server_key=SERVER_KEY, **mapping)


def _serve(server, *, ports=HOST_PORTS, mappings=(), os_name="linux"):
    server.register(PORTS_PATH, {"status": "ok", "ports": list(ports)})
    server.register(STACK_PATH, {"status": "ok", "mappings": list(mappings)})
    server.register(DEVICE_PATH, {"status": "ok", "devices": [{"device_id": HOST_ID, "os": os_name}]})


def _acquire(subject):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource

    return acquire(subject, server_key=SERVER_KEY, resources=[Resource.INTERFACES])


def _blocked(snapshot):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.resources import blocked_scopes

    return blocked_scopes(snapshot, Resource.INTERFACES)


def _ready(snapshot):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.resources import is_ready

    return is_ready(snapshot, Resource.INTERFACES)


def _paths(server):
    return [request["path"] for request in server.requests]


@pytest.mark.django_db
def test_the_snapshot_holds_the_raw_ports_pairs_and_device_record(live_librenms):
    from core.models import ObjectChange
    from dcim.models import Device
    from django.core.cache import cache

    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import ObjectRef
    from netbox_librenms_plugin.tests.source_snapshot_helpers import port_pair

    device = _device("acq-iface-obtained")
    _serve(live_librenms.server, mappings=[port_pair(1, 100)])
    keys_before = set(cache.iter_keys("*"))
    rows_before = (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count())

    snapshot = _acquire(device)

    assert snapshot.value(Resource.INTERFACES, "ports") == HOST_PORTS
    assert snapshot.value(Resource.INTERFACES, "port_stack") == [port_pair(1, 100)]
    assert snapshot.value(Resource.INTERFACES, "device")["os"] == "linux"
    # Source facts only: nothing derived is added to the stored rows.
    assert "untagged_vlan" not in snapshot.value(Resource.INTERFACES, "ports")[0]
    assert snapshot.resources[Resource.INTERFACES].owner == ObjectRef.of(device)
    assert snapshot.resources[Resource.INTERFACES].oob is None
    assert _blocked(snapshot) == {}
    (ports_request,) = [request for request in live_librenms.server.requests if request["path"] == PORTS_PATH]
    assert ports_request["query"]["with"] == ["vlans"]
    assert sorted(_paths(live_librenms.server)) == sorted([PORTS_PATH, STACK_PATH, DEVICE_PATH])
    # Acquisition writes neither the cache nor NetBox.
    assert set(cache.iter_keys("*")) == keys_before
    assert (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count()) == rows_before


@pytest.mark.django_db
def test_the_vlan_fields_that_librenms_reports_are_obtained(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource

    ports = [
        {"port_id": 1, "ifVlan": "10", "ifTrunk": "dot1Q", "vlans": [{"vlan": 10, "untagged": 1}, {"vlan": "20"}]},
        {"port_id": 2, "ifVlan": 30, "ifTrunk": None},
        {"port_id": 3, "ifVlan": None, "vlans": []},
        {"port_id": 4, "ifVlan": ""},
    ]
    _serve(live_librenms.server, ports=ports)

    snapshot = _acquire(_device("acq-iface-vlan-shapes"))

    assert snapshot.value(Resource.INTERFACES, "ports") == ports


@pytest.mark.django_db
def test_a_device_with_no_ports_and_no_pairs_is_an_obtained_empty_source(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource

    _serve(live_librenms.server, ports=[])

    snapshot = _acquire(_device("acq-iface-empty"))

    assert snapshot.value(Resource.INTERFACES, "ports") == []
    assert snapshot.value(Resource.INTERFACES, "port_stack") == []
    assert _ready(snapshot)


def _break_ports(serve):
    return lambda server: (_serve(server), serve(server, PORTS_PATH))


def _break_stack(body, status=200):
    return lambda server: (_serve(server), server.register(STACK_PATH, body, status=status))


def _ports_with(**vlan_fields):
    """Serve one host port whose VLAN fields are *vlan_fields*."""
    return _break_ports(lambda s, p: s.register(p, {"status": "ok", "ports": [{"port_id": 1, **vlan_fields}]}))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("serve", "failed_path", "http_status"),
    [
        (_break_ports(lambda s, p: s.register(p, {"status": "ok", "ports": "x"})), PORTS_PATH, 200),
        (
            _break_ports(lambda s, p: s.register(p, {"status": "error", "message": "boom"}, status=500)),
            PORTS_PATH,
            500,
        ),
        (_break_ports(lambda s, p: s.register_disconnect(p)), PORTS_PATH, None),
        (_ports_with(ifVlan="trunk"), PORTS_PATH, 200),
        (_ports_with(ifVlan=True), PORTS_PATH, 200),
        (_ports_with(vlans={"vlan": 10}), PORTS_PATH, 200),
        (_ports_with(vlans=["10"]), PORTS_PATH, 200),
        (_ports_with(vlans=[{"untagged": 1}]), PORTS_PATH, 200),
        (_ports_with(vlans=[{"vlan": "N/A", "untagged": 0}]), PORTS_PATH, 200),
        (_break_stack([{"high_port_id": 1, "low_port_id": 100}]), STACK_PATH, 200),
        (_break_stack({"status": "error", "message": "polling off", "mappings": []}), STACK_PATH, 200),
        (_break_stack({"status": False, "mappings": []}), STACK_PATH, 200),
        (_break_stack({"status": "ok"}), STACK_PATH, 200),
        (_break_stack({"status": "ok", "mappings": None}), STACK_PATH, 200),
        (_break_stack({"status": "ok", "mappings": ["x"]}), STACK_PATH, 200),
        (_break_stack({"status": "ok", "mappings": [{"high_port_id": 1}]}), STACK_PATH, 200),
        (_break_stack({"status": "error", "message": "Device does not exist"}, 404), STACK_PATH, 404),
        (lambda s: (_serve(s), s.register_raw(STACK_PATH, "not-json")), STACK_PATH, 200),
    ],
    ids=[
        "ports-malformed-envelope",
        "ports-http-500",
        "ports-disconnect",
        "ports-ifvlan-not-a-vid",
        "ports-ifvlan-bool",
        "ports-vlans-not-a-list",
        "ports-vlans-entry-not-an-object",
        "ports-vlans-entry-without-vid",
        "ports-vlans-entry-vid-not-a-number",
        "stack-not-an-object",
        "stack-error-status",
        "stack-non-string-status",
        "stack-no-mappings",
        "stack-null-mappings",
        "stack-non-dict-pair",
        "stack-half-pair",
        "stack-404",
        "stack-not-json",
    ],
)
def test_an_unusable_ports_or_port_stack_answer_blocks_the_host_scope(live_librenms, serve, failed_path, http_status):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import IncompleteSource, Outcome

    serve(live_librenms.server)

    snapshot = _acquire(_device("acq-iface-broken"))

    part = "ports" if failed_path == PORTS_PATH else "port_stack"
    record = snapshot.record(Resource.INTERFACES, part)
    assert record.outcome is Outcome.FAILED
    assert record.request.path == failed_path.removeprefix("/api/v0/")
    assert record.http_status == http_status
    (blocker,) = _blocked(snapshot)["host"]
    assert blocker.path == failed_path.removeprefix("/api/v0/")
    assert not _ready(snapshot)
    with pytest.raises(IncompleteSource):
        snapshot.value(Resource.INTERFACES, part)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "device_record",
    [
        {"status": "ok", "devices": [{"device_id": HOST_ID, "os": "  "}]},
        {"status": "ok", "devices": [{"device_id": HOST_ID}]},
        {"status": "ok", "devices": [{"device_id": HOST_ID, "os": 7}]},
        {"status": "ok", "devices": []},
    ],
    ids=["blank-os", "no-os", "non-string-os", "no-device-record"],
)
def test_port_stack_pairs_without_a_device_os_block_the_host_scope(live_librenms, device_record):
    from netbox_librenms_plugin.tests.source_snapshot_helpers import port_pair

    _serve(live_librenms.server, mappings=[port_pair(1, 100)])
    live_librenms.server.register(DEVICE_PATH, device_record)

    snapshot = _acquire(_device("acq-iface-no-os"))

    (blocker,) = _blocked(snapshot)["host"]
    assert blocker.path == f"devices/{HOST_ID}"
    assert not _ready(snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize("device_status", [200, 500])
def test_a_device_without_pairs_needs_no_device_os(live_librenms, device_status):
    _serve(live_librenms.server)
    live_librenms.server.register(
        DEVICE_PATH, {"status": "ok", "devices": [{"device_id": HOST_ID}]}, status=device_status
    )

    snapshot = _acquire(_device("acq-iface-no-pairs"))

    assert _blocked(snapshot) == {}


@pytest.mark.django_db
def test_a_linked_oob_controller_is_acquired_as_its_own_mapping(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import MappingState
    from netbox_librenms_plugin.server_mappings import MappingRole

    device = _device("acq-iface-oob", oob={"id": OOB_ID, "type": "idrac"})
    _serve(live_librenms.server)
    live_librenms.server.register(OOB_PORTS_PATH, {"status": "ok", "ports": OOB_PORTS})

    snapshot = _acquire(device)

    oob = snapshot.resources[Resource.INTERFACES].oob
    assert (oob.role, oob.librenms_id, oob.state) == (MappingRole.OOB, OOB_ID, MappingState.RESOLVED)
    assert snapshot.value(Resource.INTERFACES, "ports", MappingRole.OOB) == OOB_PORTS
    assert _blocked(snapshot) == {}
    (oob_request,) = [request for request in live_librenms.server.requests if request["path"] == OOB_PORTS_PATH]
    assert oob_request["query"]["with"] == ["vlans"]


@pytest.mark.django_db
def test_an_oob_failure_blocks_the_oob_rows_only(live_librenms):
    device = _device("acq-iface-oob-fails", oob={"id": OOB_ID, "type": "idrac"})
    _serve(live_librenms.server)
    live_librenms.server.register(OOB_PORTS_PATH, {"status": "error", "message": "boom"}, status=500)

    snapshot = _acquire(device)

    blocked = _blocked(snapshot)
    assert set(blocked) == {"oob"}
    assert blocked["oob"][0].path == f"devices/{OOB_ID}/ports"
    assert _ready(snapshot)


@pytest.mark.django_db
def test_a_blocked_host_scope_blocks_the_oob_rows_too(live_librenms):
    device = _device("acq-iface-host-fails", oob={"id": OOB_ID, "type": "idrac"})
    _serve(live_librenms.server)
    live_librenms.server.register(PORTS_PATH, {"status": "error"}, status=500)
    live_librenms.server.register(OOB_PORTS_PATH, {"status": "ok", "ports": OOB_PORTS})

    snapshot = _acquire(device)

    assert set(_blocked(snapshot)) == {"host", "oob"}
    assert not _ready(snapshot)


@pytest.mark.django_db
def test_a_corrupt_oob_id_is_an_invalid_mapping_that_makes_no_request(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import MappingState, Outcome
    from netbox_librenms_plugin.server_mappings import MappingRole

    device = _device("acq-iface-oob-corrupt", oob={"id": "not-an-id", "type": "idrac"})
    _serve(live_librenms.server)

    snapshot = _acquire(device)

    oob = snapshot.resources[Resource.INTERFACES].oob
    assert (oob.librenms_id, oob.state) == (None, MappingState.INVALID)
    record = snapshot.record(Resource.INTERFACES, "ports", MappingRole.OOB)
    assert record.outcome is Outcome.FAILED
    assert record.http_status is None
    assert record.request.path == "devices/None/ports"
    assert set(_blocked(snapshot)) == {"oob"}
    assert _blocked(snapshot)["oob"][0].path == "oob"
    assert sorted(_paths(live_librenms.server)) == sorted([PORTS_PATH, STACK_PATH, DEVICE_PATH])


@pytest.mark.django_db
def test_a_change_of_the_oob_link_alone_makes_the_snapshot_unreadable(live_librenms):
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping

    device = _device("acq-iface-oob-binding", oob={"id": "not-an-id", "type": "idrac"})
    _serve(live_librenms.server)
    live_librenms.server.register(OOB_PORTS_PATH, {"status": "ok", "ports": OOB_PORTS})
    snapshot = _acquire(device)
    assert source_cache.store(device, Resource.INTERFACES, snapshot)

    # A host-only snapshot stays readable while the corrupt link stays as it was.
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) == snapshot

    # The mapping changes without a save, so no write signal clears the snapshot; the binding check does.
    # The host id is the same; only the OOB link becomes valid.
    seed_stored_mapping(device, {SERVER_KEY: {"id": HOST_ID, "oob": {"id": OOB_ID, "type": "idrac"}}})
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) is None

    snapshot = _acquire(device)
    assert source_cache.store(device, Resource.INTERFACES, snapshot)
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) == snapshot

    # An unlink is a change of the link too.
    seed_stored_mapping(device, {SERVER_KEY: {"id": HOST_ID}})
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) is None


@pytest.mark.django_db
def test_a_virtual_chassis_member_acquires_through_the_vc_sync_owner(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import ObjectRef
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    _vc, (master, member) = make_virtual_chassis_members("acq-iface-vc")
    seed_mapping(master, SERVER_KEY, own=HOST_ID)
    _serve(live_librenms.server)

    snapshot = _acquire(member)

    source = snapshot.resources[Resource.INTERFACES]
    assert (source.owner, source.host.librenms_id) == (ObjectRef.of(master), HOST_ID)
    assert snapshot.subject == ObjectRef.of(member)
    assert PORTS_PATH in _paths(live_librenms.server)


@pytest.mark.django_db
def test_a_virtual_machine_acquires_its_interfaces(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    vm = seed_mapping(make_vm("acq-iface-vm"), SERVER_KEY, own=HOST_ID)
    _serve(live_librenms.server)

    snapshot = _acquire(vm)

    assert snapshot.value(Resource.INTERFACES, "ports") == HOST_PORTS
    assert _blocked(snapshot) == {}


@pytest.mark.django_db
def test_an_oob_only_owner_has_no_interfaces_to_acquire(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped
    from netbox_librenms_plugin.tests.conftest import make_device, map_device_to_librenms

    device = map_device_to_librenms(
        make_device("acq-iface-oob-only"), server_key=SERVER_KEY, oob={"id": OOB_ID, "type": "idrac"}
    )
    live_librenms.server.register(OOB_PORTS_PATH, {"status": "ok", "ports": OOB_PORTS})

    # Interfaces need a host mapping; the OOB controller alone is not one.
    with pytest.raises(SubjectNotMapped):
        _acquire(device)
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_an_oob_link_change_drops_only_the_interfaces_snapshot():
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import (
        seed_interfaces_snapshot,
        seed_ip_snapshot,
        seed_source_snapshot,
    )

    device = _device("acq-iface-oob-scope")
    seed_interfaces_snapshot(device, HOST_PORTS)
    seed_ip_snapshot(device, [])
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[])

    # The mapping changes without a save, so no write signal clears a snapshot; only the bindings decide.
    seed_stored_mapping(device, {SERVER_KEY: {"id": HOST_ID, "oob": {"id": OOB_ID, "type": "idrac"}}})

    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) is None
    assert source_cache.read(device, Resource.IP_ADDRESSES, SERVER_KEY) is not None
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is not None
