"""VLAN acquisition runs without a request and records what LibreNMS answered, over the local HTTP stub."""

import pytest

SERVER_KEY = "default"
DEVICE_ID = 4201
VLANS_PATH = "/api/v0/resources/vlans"


def _vlan(vid, device_id=DEVICE_ID):
    return {"vlan_id": vid * 10, "device_id": device_id, "vlan_vlan": vid, "vlan_name": f"v{vid}", "vlan_state": 1}


def _device(name):
    from netbox_librenms_plugin.tests.conftest import make_device

    return make_device(name, librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})


def _acquire(device):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource

    return acquire(device, server_key=SERVER_KEY, resources=[Resource.VLANS])


def _record(snapshot):
    from netbox_librenms_plugin.acquisition.registry import Resource

    return snapshot.record(Resource.VLANS, "vlans")


@pytest.mark.django_db
def test_the_snapshot_keeps_only_this_devices_vlans(live_librenms):
    from django.core.cache import cache

    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.resources import is_ready

    device = _device("acq-vlan-own")
    live_librenms.server.register(VLANS_PATH, {"status": "ok", "vlans": [_vlan(10), _vlan(20, device_id=9999)]})
    keys_before = set(cache.iter_keys("*"))

    snapshot = _acquire(device)

    record = _record(snapshot)
    assert record.obtained and record.http_status == 200
    assert snapshot.value(Resource.VLANS, "vlans") == [_vlan(10)]
    # The instance-wide body keeps this device's rows only.
    assert record.body == {"status": "ok", "vlans": [_vlan(10)]}
    assert is_ready(snapshot, Resource.VLANS)
    assert set(cache.iter_keys("*")) == keys_before


@pytest.mark.django_db
def test_a_device_with_no_vlans_is_an_obtained_empty_list(live_librenms):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.resources import is_ready

    live_librenms.server.register(VLANS_PATH, {"status": "ok", "vlans": [_vlan(20, device_id=9999)]})

    snapshot = _acquire(_device("acq-vlan-empty"))

    assert snapshot.value(Resource.VLANS, "vlans") == []
    assert is_ready(snapshot, Resource.VLANS)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("serve", "reason"),
    [
        (lambda server: server.register(VLANS_PATH, {"status": "ok", "vlans": "not-a-list"}), "malformed"),
        (lambda server: server.register(VLANS_PATH, {"status": "error", "message": "boom"}, status=500), "boom"),
        (lambda server: server.register_disconnect(VLANS_PATH), "could not be reached"),
    ],
    ids=["malformed-envelope", "endpoint-failure", "disconnect"],
)
def test_an_unusable_answer_blocks_the_resource_and_is_never_an_empty_list(live_librenms, serve, reason):
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.resources import is_ready
    from netbox_librenms_plugin.acquisition.snapshot import IncompleteSource, Outcome

    serve(live_librenms.server)

    snapshot = _acquire(_device(f"acq-vlan-{reason.split()[0]}"))

    record = _record(snapshot)
    assert record.outcome is Outcome.FAILED
    assert reason in record.reason
    assert not is_ready(snapshot, Resource.VLANS)
    with pytest.raises(IncompleteSource):
        snapshot.value(Resource.VLANS, "vlans")


@pytest.mark.django_db
def test_acquisition_refuses_a_subject_it_cannot_acquire(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device, make_vm

    device = _device("acq-vlan-refusals")
    for server_key in ("", "  ", "no-such-server"):
        with pytest.raises(ValueError):
            acquire(device, server_key=server_key, resources=[Resource.VLANS])
    with pytest.raises(ValueError, match="virtual machine"):
        acquire(make_vm("acq-vlan-vm"), server_key=SERVER_KEY, resources=[Resource.VLANS])
    # Acquisition reads the mapping; it never discovers one.
    with pytest.raises(SubjectNotMapped):
        acquire(make_device("acq-vlan-unmapped"), server_key=SERVER_KEY, resources=[Resource.VLANS])
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_the_stored_snapshot_round_trips_and_follows_the_mapping(live_librenms):
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    device = _device("acq-vlan-store")
    live_librenms.server.register(VLANS_PATH, {"status": "ok", "vlans": [_vlan(10)]})
    snapshot = _acquire(device)

    assert source_cache.store(device, Resource.VLANS, snapshot)
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) == snapshot

    # The mapping changes without a save, so no write signal clears the snapshot; the binding check does.
    seed_mapping(device, SERVER_KEY, own=DEVICE_ID + 1, save=False)
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None
