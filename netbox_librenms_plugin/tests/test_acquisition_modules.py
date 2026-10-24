"""Modules acquisition records source facts over the local LibreNMS HTTP stub."""

import pytest

from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes, is_ready
from netbox_librenms_plugin.acquisition.snapshot import IncompleteSource, MappingState, ObjectRef, Outcome
from netbox_librenms_plugin.server_mappings import MappingRole
from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis_members, map_device_to_librenms
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

HOST_ID = 5301
OOB_ID = 5302
INVENTORY = [{"entPhysicalIndex": 1, "entPhysicalContainedIn": 0, "entPhysicalClass": "chassis"}]
PORTS = [{"port_id": 7, "ifIndex": 3, "ifName": "eth3"}]
TRANSCEIVERS = [{"entity_physical_index": 9001, "port_id": 7, "model": "optic", "serial": "OPTIC"}]


def device(name, **mapping):
    return map_device_to_librenms(make_device(name), HOST_ID, server_key="default", **mapping)


def serve(server, *, inventory=INVENTORY, ports=PORTS, transceivers=TRANSCEIVERS):
    server.register(f"/api/v0/inventory/{HOST_ID}/all", {"status": "ok", "inventory": inventory})
    server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "ok", "ports": ports})
    server.register(f"/api/v0/devices/{HOST_ID}/transceivers", {"status": "ok", "transceivers": transceivers})


def acquire_modules(subject, **options):
    return acquire(subject, server_key="default", resources=[Resource.MODULES], **options)


@pytest.mark.django_db
def test_modules_acquire_raw_facts_without_writing_netbox_or_cache(live_librenms):
    from core.models import ObjectChange
    from dcim.models import Device
    from django.core.cache import cache

    subject = device("modules-source")
    serve(live_librenms.server)
    before = (Device.objects.get(pk=subject.pk).last_updated, ObjectChange.objects.count(), set(cache.iter_keys("*")))

    snapshot = acquire_modules(subject)

    assert snapshot.value(Resource.MODULES, "inventory") == INVENTORY
    assert snapshot.value(Resource.MODULES, "ports") == PORTS
    assert snapshot.value(Resource.MODULES, "transceivers") == TRANSCEIVERS
    assert snapshot.resources[Resource.MODULES].owner == ObjectRef.of(subject)
    assert blocked_scopes(snapshot, Resource.MODULES) == {}
    assert len(live_librenms.server.requests) == 3
    assert (
        Device.objects.get(pk=subject.pk).last_updated,
        ObjectChange.objects.count(),
        set(cache.iter_keys("*")),
    ) == before


@pytest.mark.django_db
def test_empty_modules_are_an_obtained_source(live_librenms):
    serve(live_librenms.server, inventory=[], ports=[], transceivers=[])
    snapshot = acquire_modules(device("modules-empty"))
    assert snapshot.value(Resource.MODULES, "inventory") == []
    assert is_ready(snapshot, Resource.MODULES)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "part,path,field",
    [
        ("inventory", f"inventory/{HOST_ID}/all", "inventory"),
        ("ports", f"devices/{HOST_ID}/ports", "ports"),
        ("transceivers", f"devices/{HOST_ID}/transceivers", "transceivers"),
    ],
)
@pytest.mark.parametrize("failure", ["malformed", "http-error", "disconnect"])
def test_failed_host_parts_block_modules(live_librenms, part, path, field, failure):
    serve(live_librenms.server)
    if failure == "disconnect":
        live_librenms.server.register_disconnect(f"/api/v0/{path}")
    else:
        live_librenms.server.register(
            f"/api/v0/{path}", {"status": "ok", field: None}, status=500 if failure == "http-error" else 200
        )
    snapshot = acquire_modules(device("modules-failed"))
    assert snapshot.record(Resource.MODULES, part).outcome is Outcome.FAILED
    assert {blocker.path for blocker in blocked_scopes(snapshot, Resource.MODULES)["host"]} == {path}
    assert not is_ready(snapshot, Resource.MODULES)
    with pytest.raises(IncompleteSource):
        snapshot.value(Resource.MODULES, part)


@pytest.mark.django_db
@pytest.mark.parametrize("oob_id", [OOB_ID, "corrupt"])
def test_oob_inventory_is_a_separate_mapping_and_its_failure_leaves_host_ready(live_librenms, oob_id):
    subject = device("modules-oob-failed", oob={"id": oob_id, "type": "idrac"})
    serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/inventory/{OOB_ID}/all", {"status": "error"}, status=500)
    snapshot = acquire_modules(subject)
    oob = snapshot.resources[Resource.MODULES].oob
    assert oob.role is MappingRole.OOB
    assert oob.state is (MappingState.INVALID if oob_id == "corrupt" else MappingState.RESOLVED)
    assert snapshot.record(Resource.MODULES, "inventory", MappingRole.OOB).outcome is Outcome.FAILED
    assert set(blocked_scopes(snapshot, Resource.MODULES)) == {"oob"}
    assert is_ready(snapshot, Resource.MODULES)


@pytest.mark.django_db
def test_host_failure_blocks_obtained_oob_inventory(live_librenms):
    subject = device("modules-host-failed", oob={"id": OOB_ID, "type": "idrac"})
    serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "error"}, status=500)
    live_librenms.server.register(f"/api/v0/inventory/{OOB_ID}/all", {"status": "ok", "inventory": INVENTORY})
    snapshot = acquire_modules(subject)
    assert snapshot.value(Resource.MODULES, "inventory", MappingRole.OOB) == INVENTORY
    assert set(blocked_scopes(snapshot, Resource.MODULES)) == {"host", "oob"}
    assert not is_ready(snapshot, Resource.MODULES)


@pytest.mark.django_db
def test_modules_need_a_host_mapping(live_librenms):
    subject = map_device_to_librenms(
        make_device("modules-oob-only"), server_key="default", oob={"id": OOB_ID, "type": "idrac"}
    )
    with pytest.raises(SubjectNotMapped):
        acquire_modules(subject)
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_vc_modules_use_the_sync_owner(live_librenms):
    _vc, (master, member) = make_virtual_chassis_members("modules-vc")
    seed_mapping(master, "default", own=HOST_ID)
    serve(live_librenms.server)
    snapshot = acquire_modules(member)
    assert snapshot.subject == ObjectRef.of(member)
    assert snapshot.resources[Resource.MODULES].owner == ObjectRef.of(master)
    assert snapshot.value(Resource.MODULES, "inventory") == INVENTORY


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("entPhysicalIndex", "1"),
        ("entPhysicalIndex", -1),
        ("entPhysicalIndex", True),
        ("entPhysicalContainedIn", "0"),
        ("entPhysicalContainedIn", -1),
    ],
)
def test_inventory_indices_must_support_assessed_oob_offsets(live_librenms, field, value):
    serve(live_librenms.server, inventory=[{**INVENTORY[0], field: value}])
    snapshot = acquire_modules(device("modules-index-contract"))
    assert snapshot.record(Resource.MODULES, "inventory").outcome is Outcome.FAILED
    assert not is_ready(snapshot, Resource.MODULES)


@pytest.mark.django_db
def test_inventory_probes_select_children_after_the_effective_root(live_librenms):
    root = {"entPhysicalIndex": 10, "entPhysicalContainedIn": 0, "entPhysicalClass": "stack"}
    member = {"entPhysicalIndex": 11, "entPhysicalContainedIn": 10, "entPhysicalClass": "chassis"}
    serve(live_librenms.server, inventory=[root, member])
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "ok", "inventory": []})

    snapshot = acquire_modules(device("modules-probes"), inventory_probes=True)

    assert snapshot.value(Resource.MODULES, "inventory_root") == []
    assert snapshot.value(Resource.MODULES, "inventory_children") == []
    requests = [
        request for request in live_librenms.server.requests if request["path"] == f"/api/v0/inventory/{HOST_ID}"
    ]
    assert [request["query"] for request in requests] == [
        {"entPhysicalContainedIn": ["0"]},
        {"entPhysicalContainedIn": ["10"]},
    ]
    assert len([request for request in live_librenms.server.requests if request["path"].endswith("/all")]) == 1


@pytest.mark.django_db
def test_failed_root_inventory_does_not_request_children(live_librenms):
    serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "ok", "inventory": None})

    snapshot = acquire_modules(device("modules-probes-failed"), inventory_probes=True)

    assert snapshot.record(Resource.MODULES, "inventory_root").outcome is Outcome.FAILED
    assert "inventory_children" not in dict(snapshot.resources[Resource.MODULES].host.parts)
    requests = [
        request for request in live_librenms.server.requests if request["path"] == f"/api/v0/inventory/{HOST_ID}"
    ]
    assert len(requests) == 1


@pytest.mark.django_db
def test_a_transceiver_envelope_without_success_status_blocks_host_modules(live_librenms):
    serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/transceivers", {"transceivers": []})
    snapshot = acquire_modules(device("modules-transceiver-status"))
    assert snapshot.record(Resource.MODULES, "transceivers").outcome is Outcome.FAILED
    assert not is_ready(snapshot, Resource.MODULES)
