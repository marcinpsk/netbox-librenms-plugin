"""Modules acquisition records source facts over the local LibreNMS HTTP stub."""

from netbox_librenms_plugin.acquisition.coordinator import plan_acquisition

import pytest

from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes, is_ready
from netbox_librenms_plugin.acquisition.snapshot import IncompleteSource, MappingState, ObjectRef, Outcome
from netbox_librenms_plugin.server_mappings import MappingRole
from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis_members
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import module_serial_context

HOST_ID = 5301
OOB_ID = 5302
INVENTORY = [{"entPhysicalIndex": 1, "entPhysicalContainedIn": 0, "entPhysicalClass": "chassis"}]
PORTS = [{"port_id": 7, "ifIndex": 3, "ifName": "eth3"}]
TRANSCEIVERS = [{"entity_physical_index": 9001, "port_id": 7, "model": "optic", "serial": "OPTIC"}]


def device(name, *, oob=None):
    return seed_mapping(make_device(name), own=HOST_ID, oob=oob, oob_type="idrac")


def corrupt_oob_device(name, oob_id="corrupt"):
    stored = {"default": {"id": HOST_ID, "oob": {"id": oob_id, "type": "idrac"}}}
    return seed_stored_mapping(make_device(name), stored, save=True)


def serve(server, *, inventory=INVENTORY, ports=PORTS, transceivers=TRANSCEIVERS):
    server.register(f"/api/v0/inventory/{HOST_ID}/all", {"status": "ok", "inventory": inventory})
    server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "ok", "ports": ports})
    server.register(f"/api/v0/devices/{HOST_ID}/transceivers", {"status": "ok", "transceivers": transceivers})


def acquire_modules(subject, **options):
    return acquire(plan_acquisition(subject, server_key="default", resources=[Resource.MODULES]), **options)


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
    name = "modules-oob-failed"
    subject = corrupt_oob_device(name) if oob_id == "corrupt" else device(name, oob=OOB_ID)
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
def test_corrupt_oob_mapping_blocks_modules_without_a_fake_endpoint(live_librenms):
    subject = corrupt_oob_device("modules-corrupt-oob")
    serve(live_librenms.server)

    snapshot = acquire_modules(subject)

    (blocker,) = blocked_scopes(snapshot, Resource.MODULES)["oob"]
    assert blocker.path == "oob"
    assert {request["path"] for request in live_librenms.server.requests} == {
        f"/api/v0/inventory/{HOST_ID}/all",
        f"/api/v0/devices/{HOST_ID}/ports",
        f"/api/v0/devices/{HOST_ID}/transceivers",
    }


@pytest.mark.django_db
def test_host_failure_blocks_obtained_oob_inventory(live_librenms):
    subject = device("modules-host-failed", oob=OOB_ID)
    serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "error"}, status=500)
    live_librenms.server.register(f"/api/v0/inventory/{OOB_ID}/all", {"status": "ok", "inventory": INVENTORY})
    snapshot = acquire_modules(subject)
    assert snapshot.value(Resource.MODULES, "inventory", MappingRole.OOB) == INVENTORY
    assert set(blocked_scopes(snapshot, Resource.MODULES)) == {"host", "oob"}
    assert not is_ready(snapshot, Resource.MODULES)


@pytest.mark.django_db
def test_modules_need_a_host_mapping(live_librenms):
    subject = seed_mapping(make_device("modules-oob-only"), oob=OOB_ID, oob_type="idrac")
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
        ("entPhysicalIndex", "-1"),
        ("entPhysicalIndex", "١"),
        ("entPhysicalIndex", " 1"),
        ("entPhysicalIndex", "1.0"),
        ("entPhysicalIndex", ""),
        pytest.param("entPhysicalIndex", "1" * 5000, id="oversized-digit-string"),
        ("entPhysicalIndex", -1),
        ("entPhysicalIndex", True),
        ("entPhysicalContainedIn", "+0"),
        ("entPhysicalContainedIn", -1),
    ],
)
def test_inventory_indices_must_support_assessed_oob_offsets(live_librenms, field, value):
    serve(live_librenms.server, inventory=[{**INVENTORY[0], field: value}])
    snapshot = acquire_modules(device("modules-index-contract"))
    assert snapshot.record(Resource.MODULES, "inventory").outcome is Outcome.FAILED
    assert not is_ready(snapshot, Resource.MODULES)


@pytest.mark.django_db
@pytest.mark.parametrize("string_field", ["entPhysicalIndex", "entPhysicalContainedIn", "both"])
def test_string_inventory_indices_assess_host_and_oob_without_changing_source(live_librenms, string_field):
    from netbox_librenms_plugin.assessment.modules import assess
    from netbox_librenms_plugin.tests.source_snapshot_helpers import inventory_row, transceiver_row

    def source_rows():
        rows = [
            inventory_row(1, "Chassis", physical_class="chassis"),
            inventory_row(100, "FPC 1", physical_class="container", parent=1, entPhysicalDescr="FPC: Card @ 1/*/*"),
            inventory_row(200, "PIC 1/0", parent=100, entPhysicalDescr="PIC: Card @ 1/0/*"),
            inventory_row(300, "Optic", physical_class="port", parent=999, entPhysicalDescr="SFP @ 1/0/0"),
        ]
        for row in rows:
            for field in ("entPhysicalIndex", "entPhysicalContainedIn"):
                if string_field in (field, "both"):
                    row[field] = str(row[field]).zfill(4)
        return rows

    host = source_rows()
    oob = source_rows()
    subject = device("modules-string-indices", oob=OOB_ID)
    serve(
        live_librenms.server,
        inventory=host,
        transceivers=[transceiver_row(300, port_id=7, model="SFP", serial="OPTIC")],
    )
    live_librenms.server.register(f"/api/v0/inventory/{OOB_ID}/all", {"status": "ok", "inventory": oob})
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "ok", "inventory": host[:1]})

    snapshot = acquire_modules(subject, inventory_probes=True)

    assert blocked_scopes(snapshot, Resource.MODULES) == {}
    assert snapshot.value(Resource.MODULES, "inventory") == host
    assert snapshot.value(Resource.MODULES, "inventory", MappingRole.OOB) == oob
    assert snapshot.value(Resource.MODULES, "inventory_root") == host[:1]
    assert snapshot.value(Resource.MODULES, "inventory_children") == host[:1]
    original = snapshot.to_payload()
    result = assess(snapshot, subject=subject, serial_context=module_serial_context(subject))
    assert result["blocked"] == {}
    assert {row["entPhysicalIndex"]: row["entPhysicalContainedIn"] for row in result["inventory"]} == {
        1: 0,
        100: 1,
        200: 100,
        300: 200,
        1001: 0,
        1100: 1001,
        1200: 1100,
        1300: 1200,
    }
    assert next(row for row in result["inventory"] if row["entPhysicalIndex"] == 300)["entPhysicalModelName"] == "SFP"
    assert snapshot.to_payload() == original
    assert assess(snapshot, subject=subject, serial_context=module_serial_context(subject)) == result


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


@pytest.fixture
def padded_inventory():
    return [
        {"entPhysicalIndex": "0010", "entPhysicalContainedIn": "0000", "entPhysicalClass": "stack"},
        {"entPhysicalIndex": "0011", "entPhysicalContainedIn": "0010", "entPhysicalClass": "chassis"},
        {"entPhysicalIndex": "0012", "entPhysicalContainedIn": "0010", "entPhysicalClass": "0010"},
        {"entPhysicalIndex": "0021", "entPhysicalContainedIn": "0020", "entPhysicalClass": "chassis"},
    ]


@pytest.mark.django_db
def test_empty_filtered_inventory_uses_padded_root_for_child_probe(live_librenms, padded_inventory):
    from netbox_librenms_plugin.acquisition import responses

    serve(live_librenms.server, inventory=padded_inventory, ports=[], transceivers=[])
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "ok", "inventory": []})

    snapshot = acquire_modules(device("modules-padded-root-fallback"), inventory_probes=True)

    assert snapshot.record(Resource.MODULES, "inventory_children").outcome is Outcome.OBTAINED
    requests = [
        request for request in live_librenms.server.requests if request["path"] == f"/api/v0/inventory/{HOST_ID}"
    ]
    assert [request["query"] for request in requests] == [
        {"entPhysicalContainedIn": ["0"]},
        {"entPhysicalContainedIn": ["10"]},
    ]
    original = snapshot.to_payload()
    for part, expected in (("inventory_root", padded_inventory[:1]), ("inventory_children", padded_inventory[1:3])):
        record = snapshot.record(Resource.MODULES, part)
        assert record.value == []
        assert (
            responses.effective_inventory(
                record,
                snapshot.record(Resource.MODULES, "inventory"),
                params=record.request.params,
                http_status=record.http_status,
            )
            == expected
        )
    assert snapshot.value(Resource.MODULES, "inventory") == padded_inventory
    assert snapshot.to_payload() == original


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query,expected_rows",
    [
        ({"ent_physical_contained_in": 0}, [0]),
        ({"ent_physical_contained_in": 10}, [1, 2]),
        ({"ent_physical_contained_in": "0010", "ent_physical_class": "chassis"}, [1]),
        ({"ent_physical_contained_in": 10, "ent_physical_class": "0010"}, [2]),
        ({"ent_physical_contained_in": 10, "ent_physical_class": "10"}, []),
    ],
)
def test_filtered_inventory_fallback_compares_indices_and_class_by_their_types(
    live_librenms, padded_inventory, query, expected_rows
):
    serve(live_librenms.server, inventory=padded_inventory)
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "ok", "inventory": []})

    assert live_librenms.api.get_inventory_filtered(HOST_ID, **query) == (
        True,
        [padded_inventory[index] for index in expected_rows],
    )


@pytest.mark.django_db
def test_failed_root_inventory_does_not_request_children(live_librenms):
    serve(live_librenms.server)
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "ok", "inventory": None})

    snapshot = acquire_modules(device("modules-probes-failed"), inventory_probes=True)

    assert snapshot.record(Resource.MODULES, "inventory_root").outcome is Outcome.FAILED
    assert snapshot.record(Resource.MODULES, "inventory_children").outcome is Outcome.NOT_REQUESTED
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


def _optic_subject(name):
    from dcim.models import DeviceType, Manufacturer
    from netbox_librenms_plugin.models import NormalizationRule

    subject = device(name)
    manufacturer = Manufacturer.objects.create(name="Optic vendor", slug="optic-vendor")
    subject.device_type = DeviceType.objects.create(
        manufacturer=manufacturer, model="Optic device", slug="optic-device"
    )
    subject.save()
    NormalizationRule.objects.create(
        scope="serial", manufacturer=manufacturer, match_pattern=r"^TAG (.+)$", replacement=r"\1"
    )
    return subject


def _assessed_optics(subject):
    from netbox_librenms_plugin.assessment.modules import assess

    snapshot = acquire_modules(subject)
    original = snapshot.to_payload()
    result = assess(snapshot, subject=subject, serial_context=module_serial_context(subject))
    assert snapshot.to_payload() == original
    assert result["blocked"] == {}
    return result["inventory"]


@pytest.mark.django_db
@pytest.mark.parametrize("entity_index", [300, 7])
def test_optic_serial_comparison_uses_the_owner_manufacturers_rules(live_librenms, entity_index):
    from netbox_librenms_plugin.tests.source_snapshot_helpers import inventory_row, transceiver_row

    subject = _optic_subject("module-optic-comparison")
    inventory = [inventory_row(300, "Unlabelled optic", serial="TAG OPTIC-7", physical_class="port", ifIndex=7)]
    serve(
        live_librenms.server,
        inventory=inventory,
        transceivers=[transceiver_row(entity_index, port_id=42, model="SFP-10G-LR", serial="OPTIC-7", kind="SFP+")],
        ports=[{"port_id": 42, "ifIndex": 7, "ifName": "xe-0/0/0", "ifDescr": "uplink"}],
    )

    (optic,) = _assessed_optics(subject)

    assert optic["entPhysicalIndex"] == 300
    assert optic["_librenms_port_id"] == 42
    assert optic["entPhysicalModelName"] == "SFP-10G-LR"
    assert optic["entPhysicalSerialNum"] == "TAG OPTIC-7"


@pytest.mark.django_db
def test_optic_deduplication_uses_the_owner_manufacturers_rules(live_librenms):
    from netbox_librenms_plugin.tests.source_snapshot_helpers import inventory_row, transceiver_row

    subject = _optic_subject("module-optic-dedup")
    serve(
        live_librenms.server,
        inventory=[
            inventory_row(300, "Existing optic", model="SFP-10G-LR", serial="TAG OPTIC-7", physical_class="port")
        ],
        transceivers=[transceiver_row(400, port_id=42, model="SFP-10G-LR", serial="OPTIC-7", kind="SFP+")],
        ports=[{"port_id": 42, "ifName": "xe-0/0/0"}],
    )

    assert [item["entPhysicalIndex"] for item in _assessed_optics(subject)] == [300]


@pytest.mark.django_db
@pytest.mark.parametrize("existing_entity", [False, True])
def test_optics_without_normalized_serial_identity_are_not_deduplicated(live_librenms, existing_entity):
    from netbox_librenms_plugin.tests.source_snapshot_helpers import inventory_row, transceiver_row

    subject = _optic_subject("module-optic-empty-identity")
    serve(
        live_librenms.server,
        inventory=[inventory_row(400, "Existing optic", physical_class="port")] if existing_entity else [],
        transceivers=[
            transceiver_row(400, port_id=42, model="SFP-10G-LR", serial="TAG unknown", kind="SFP+"),
            transceiver_row(401, port_id=43, model="SFP-10G-LR", serial="TAG n/a", kind="SFP+"),
        ],
        ports=[{"port_id": 42, "ifName": "xe-0/0/0"}, {"port_id": 43, "ifName": "xe-0/0/1"}],
    )

    assert [item["entPhysicalIndex"] for item in _assessed_optics(subject)] == [400, 401]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "recording_name",
    ["arcos-lag-transceivers", "iosxe-subinterfaces", "junos-subinterfaces", "nokia-timos-transceivers"],
)
def test_recorded_optic_serials_and_their_ports_survive_assessment(recording_server, recording_name):
    from collections import Counter
    from netbox_librenms_plugin.tests.recordings import load_recording
    from netbox_librenms_plugin.tests.source_snapshot_helpers import acquire_recording
    from netbox_librenms_plugin.assessment.modules import assess, clean_librenms_value

    recording = load_recording(recording_name)
    _server, api = recording_server(recording)
    snapshot, subject = acquire_recording(api, recording, [Resource.MODULES])
    original = snapshot.to_payload()
    result = assess(snapshot, subject=subject, serial_context=module_serial_context(subject))
    assert result["blocked"] == {}
    inventory = result["inventory"]
    transceivers = snapshot.value(Resource.MODULES, "transceivers")
    serials = Counter(clean_librenms_value(row.get("serial")) for row in transceivers)
    bound = {(clean_librenms_value(row.get("entPhysicalSerialNum")), row.get("_librenms_port_id")) for row in inventory}
    assert set(serials) - {""} <= {serial for serial, _port in bound}
    unique = [row for row in transceivers if serials[clean_librenms_value(row.get("serial"))] == 1]
    assert unique
    for row in unique:
        assert (clean_librenms_value(row["serial"]), row["port_id"]) in bound
    if recording_name == "junos-subinterfaces":
        by_ifindex = {port["ifIndex"]: port for port in snapshot.value(Resource.MODULES, "ports")}
        by_index = {row["entPhysicalIndex"]: row for row in inventory}
        assert len(by_index) == len(inventory)
        optics = [
            entity
            for entity in snapshot.value(Resource.MODULES, "inventory")
            if entity["entPhysicalIndex"] in (640, 642)
        ]
        assert [(entity["ifIndex"], by_ifindex[entity["ifIndex"]]["ifName"]) for entity in optics] == [
            (600, "et-0/5/0"),
            (591, "et-0/5/2"),
        ]
        for entity in optics:
            if entity["entPhysicalIndex"] in (640, 642):
                port = by_ifindex[entity["ifIndex"]]
                row = by_index[entity["entPhysicalIndex"]]
                assert row["entPhysicalName"] == entity["entPhysicalName"]
                assert (row["_librenms_port_id"], row["_librenms_ifname"]) == (port["port_id"], port["ifName"])
    assert snapshot.to_payload() == original


@pytest.mark.django_db
def test_inventory_children_are_not_applicable_without_a_vc_parent(live_librenms):
    serve(live_librenms.server, inventory=[], ports=[], transceivers=[])
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "ok", "inventory": []})

    snapshot = acquire_modules(device("modules-no-vc-parent"), inventory_probes=True)

    assert snapshot.record(Resource.MODULES, "inventory_children").outcome is Outcome.NOT_APPLICABLE
    assert (
        len([request for request in live_librenms.server.requests if request["path"] == f"/api/v0/inventory/{HOST_ID}"])
        == 1
    )


@pytest.mark.django_db
@pytest.mark.parametrize("getter", ["full", "filtered"])
def test_inventory_getters_use_the_acquisition_index_contract(live_librenms, getter):
    path = f"/api/v0/inventory/{HOST_ID}" + ("/all" if getter == "full" else "")
    live_librenms.server.register(path, {"status": "ok", "inventory": [{"entPhysicalIndex": "bad-index"}]})

    success, _rows = (
        live_librenms.api.get_device_inventory(HOST_ID)
        if getter == "full"
        else live_librenms.api.get_inventory_filtered(HOST_ID)
    )

    assert success is False


@pytest.mark.django_db
def test_vc_discovery_keeps_a_known_devices_empty_inventory_404_policy(live_librenms):
    from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

    live_librenms.server.device_info_response(HOST_ID)
    live_librenms.server.register(f"/api/v0/inventory/{HOST_ID}", {"status": "error"}, status=404)

    result = get_virtual_chassis_data(live_librenms.api, HOST_ID, force_refresh=True)

    assert not result["detection_failed"]
    assert not result["is_stack"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "part,changes",
    [
        ("inventory", {"ifIndex": []}),
        ("inventory", {"entPhysicalName": {}}),
        ("ports", {"ifIndex": "bad-index"}),
        ("ports", {"ifIndex": True}),
        ("transceivers", {"entity_physical_index": None}),
        ("transceivers", {"entity_physical_index": "bad-index"}),
        ("transceivers", {"entity_physical_index": True}),
        ("transceivers", {"port_id": []}),
        ("transceivers", {"model": {}}),
        # A module writer locks and resolves the raw serial, so only text or a number reaches it.
        pytest.param("inventory", {"entPhysicalSerialNum": ["SERIAL-A"]}, id="list-inventory-serial"),
        pytest.param("inventory", {"entPhysicalSerialNum": {"serial": "SERIAL-A"}}, id="object-inventory-serial"),
        pytest.param("inventory", {"entPhysicalSerialNum": True}, id="bool-inventory-serial"),
        pytest.param("transceivers", {"serial": ["SERIAL-A"]}, id="list-transceiver-serial"),
        pytest.param("transceivers", {"serial": True}, id="bool-transceiver-serial"),
        pytest.param("ports", {"ifIndex": 0.0}, id="float-zero-ifindex"),
        pytest.param("transceivers", {"port_id": 0.0}, id="float-zero-port-id"),
    ],
)
def test_malformed_module_facts_are_refused_by_the_endpoint_contract(live_librenms, part, changes):
    values = {"inventory": INVENTORY, "ports": PORTS, "transceivers": TRANSCEIVERS}
    values[part] = [{**values[part][0], **changes}]
    serve(live_librenms.server, **values)

    snapshot = acquire_modules(device("modules-field-contract"))

    assert snapshot.record(Resource.MODULES, part).outcome is Outcome.FAILED
    assert not is_ready(snapshot, Resource.MODULES)


@pytest.mark.django_db
def test_nesting_preserves_entity_parents_and_repeats_from_source_facts(live_librenms):
    from netbox_librenms_plugin.tests.source_snapshot_helpers import inventory_row, transceiver_row
    from netbox_librenms_plugin.assessment.modules import assess

    subject = device("module-optic-nesting")
    serve(
        live_librenms.server,
        inventory=[
            inventory_row(100, "MDA 1/1"),
            inventory_row(200, "1/1/c1", physical_class="port", model="SFP", serial="ENTITY"),
            inventory_row(300, "1/1/c3", physical_class="port", model="SFP", serial="NESTED", parent=100),
        ],
        ports=[{"port_id": 7, "ifName": "1/1/c2"}, {"port_id": 8, "ifName": "9/9/c1"}],
        transceivers=[
            transceiver_row(400, port_id=7, model="SFP", serial="SYNTHETIC"),
            transceiver_row(500, port_id=8, model="SFP", serial="UNNESTED"),
        ],
    )
    snapshot = acquire_modules(subject)

    first = assess(snapshot, subject=subject, serial_context=module_serial_context(subject))
    second = assess(snapshot, subject=subject, serial_context=module_serial_context(subject))

    assert first == second
    assert {row["entPhysicalIndex"]: row["entPhysicalContainedIn"] for row in first["inventory"]} == {
        100: 0,
        200: 0,
        300: 100,
        400: 100,
        500: 0,
    }
    assert snapshot.value(Resource.MODULES, "inventory")[1]["entPhysicalContainedIn"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize("name", [0, 4410])
def test_numeric_inventory_names_remain_raw_and_assess_without_a_type_error(live_librenms, name):
    from netbox_librenms_plugin.assessment.modules import assess

    serve(live_librenms.server, inventory=[{**INVENTORY[0], "entPhysicalName": name}], ports=[], transceivers=[])
    subject = device("modules-numeric-inventory-name")

    snapshot = acquire_modules(subject)

    assert snapshot.record(Resource.MODULES, "inventory").outcome is Outcome.OBTAINED
    assert snapshot.value(Resource.MODULES, "inventory")[0]["entPhysicalName"] == name
    assert assess(snapshot, subject=subject, serial_context=module_serial_context(subject))["inventory"][0][
        "entPhysicalName"
    ] == str(name)


@pytest.mark.django_db
@pytest.mark.parametrize("expiry", ["delete", "expire"])
def test_cached_identity_acquisition_and_snapshot_binding_are_read_only(live_librenms, expiry):
    from core.models import ObjectChange
    from dcim.models import Device
    from django.core.cache import cache

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key

    subject = make_device("cache-only-source")
    key = librenms_id_cache_key(subject, "default")
    cache.set(key, str(HOST_ID), timeout=60)
    serve(live_librenms.server)
    before = (Device.objects.get(pk=subject.pk).last_updated, ObjectChange.objects.count(), cache.get(key))

    snapshot = acquire_modules(subject)

    assert snapshot.resources[Resource.MODULES].host.librenms_id == HOST_ID
    assert len(live_librenms.server.requests) == 3
    assert (Device.objects.get(pk=subject.pk).last_updated, ObjectChange.objects.count(), cache.get(key)) == before
    assert source_cache.store(subject, Resource.MODULES, snapshot)
    assert source_cache.read(subject, Resource.MODULES, "default") == snapshot
    if expiry == "expire":
        cache.expire(key, 0)
    else:
        cache.delete(key)
    assert source_cache.read(subject, Resource.MODULES, "default") is None
    assert not source_cache.store(subject, Resource.MODULES, snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize("cached", [True, -1, "not-an-id", "7/ports", {}, None])
def test_cached_identity_must_be_a_valid_id_on_the_selected_server(live_librenms, cached):
    from django.core.cache import cache
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key, stored_librenms_id

    subject = make_device("cache-only-invalid")
    cache.set(librenms_id_cache_key(subject, "other"), HOST_ID, timeout=60)
    cache.set(librenms_id_cache_key(subject, "default"), cached, timeout=60)
    assert stored_librenms_id(subject, "default") is None
    with pytest.raises(SubjectNotMapped):
        acquire_modules(subject)
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_stored_identity_precedes_cached_identity(live_librenms):
    from django.core.cache import cache
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key

    subject = device("cache-shadowed")
    cache.set(librenms_id_cache_key(subject, "default"), HOST_ID + 1, timeout=60)
    serve(live_librenms.server)
    assert acquire_modules(subject).resources[Resource.MODULES].host.librenms_id == HOST_ID


@pytest.mark.django_db
def test_modules_refresh_without_the_optional_mapping_field(client, live_librenms):
    from django.urls import reverse
    from extras.models import CustomField

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.server_mappings import read_mapping
    from netbox_librenms_plugin.tests.conftest import make_superuser

    CustomField.objects.filter(name="librenms_id").delete()
    subject = make_device("optional-mapping.example.invalid")
    live_librenms.server.register(
        f"/api/v0/devices/{subject.name}", {"status": "ok", "devices": [{"device_id": HOST_ID}]}
    )
    serve(live_librenms.server)
    client.force_login(make_superuser("optional-module-refresh"))
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[subject.pk]),
        {"server_key": "default"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response.context["module_sync"]["table"] is not None
    subject.refresh_from_db()
    assert read_mapping(subject).own_id("default") is None
    assert source_cache.read(subject, Resource.MODULES, "default") is not None


@pytest.mark.django_db
@pytest.mark.parametrize("mapped", [False, True])
def test_hidden_modules_source_owner_prevents_discovery_and_resource_http(client, live_librenms, mapped):
    from dcim.models import Device
    from django.urls import reverse

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    _vc, (owner, subject) = make_virtual_chassis_members("hidden-module-source")
    if mapped:
        seed_mapping(owner, "default", own=HOST_ID)
    live_librenms.server.register(
        f"/api/v0/devices/{owner.name}", {"status": "ok", "devices": [{"device_id": HOST_ID}]}
    )
    serve(live_librenms.server)
    user = make_user_with_perms("hidden-module-source", [("view", Device)], constraints={"pk": subject.pk})
    client.force_login(user)
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[subject.pk]),
        {"server_key": "default"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 404
    assert live_librenms.server.requests == []


@pytest.mark.django_db
@pytest.mark.parametrize("phase", ["acquire", "store"])
def test_identity_lost_after_discovery_reports_expiry_instead_of_not_found(client, live_librenms, monkeypatch, phase):
    from django.core.cache import cache
    from django.urls import reverse
    from extras.models import CustomField

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key
    from netbox_librenms_plugin.tests.conftest import make_superuser

    CustomField.objects.filter(name="librenms_id").delete()
    subject = make_device("expired-source.example.invalid")
    discovery_path = f"/api/v0/devices/{subject.name}"
    live_librenms.server.register(discovery_path, {"status": "ok", "devices": [{"device_id": HOST_ID}]})
    serve(live_librenms.server)
    real_operation = getattr(source_cache, phase)

    def expire_then_run(*args, **options):
        assert cache.get(librenms_id_cache_key(subject, "default")) == HOST_ID
        cache.expire(librenms_id_cache_key(subject, "default"), 0)
        return real_operation(*args, **options)

    monkeypatch.setattr(source_cache, phase, expire_then_run)
    client.force_login(make_superuser("expired-module-source"))
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[subject.pk]),
        {"server_key": "default"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response.context["module_sync"]["table"] is None
    messages = " ".join(str(message) for message in response.context["messages"])
    assert "identity changed or expired" in messages
    assert "Device not found" not in messages
    paths = [request["path"] for request in live_librenms.server.requests]
    assert paths[0] == discovery_path
    assert len(paths) == (1 if phase == "acquire" else 4)
    assert source_cache.read(subject, Resource.MODULES, "default") is None


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["replace-cache", "stored-replacement", "oob-link", "owner"])
def test_a_cached_source_cannot_survive_a_changed_effective_binding(live_librenms, change):
    from django.core.cache import cache
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

    subject = make_device("cache-binding-source")
    cache.set(librenms_id_cache_key(subject, "default"), HOST_ID, timeout=60)
    serve(live_librenms.server)
    snapshot = acquire_modules(subject)
    assert source_cache.store(subject, Resource.MODULES, snapshot)
    assert source_cache.read(subject, Resource.MODULES, "default") is not None
    if change == "replace-cache":
        cache.set(librenms_id_cache_key(subject, "default"), HOST_ID + 1, timeout=60)
    elif change == "stored-replacement":
        seed_mapping(subject, "default", own=HOST_ID + 1)
    elif change == "oob-link":
        seed_mapping(subject, "default", oob=OOB_ID)
    else:
        owner = seed_mapping(make_device("cache-binding-new-owner"), "default", own=HOST_ID)
        make_virtual_chassis("cache-binding-new-chassis", owner, subject)
    assert source_cache.read(subject, Resource.MODULES, "default") is None
    assert not source_cache.store(subject, Resource.MODULES, snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize("phase", ["acquisition", "discovery", "outcome"])
def test_a_refresh_clears_only_the_planned_owner_snapshot_after_owner_change(client, live_librenms, monkeypatch, phase):
    from dcim.models import Device, VirtualChassis
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, SyncTabState
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping_row
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_modules_snapshot
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    chassis, (planned, subject) = make_virtual_chassis_members("refresh-planned-snapshot")
    seed_mapping(planned, "default", own=HOST_ID)
    replacement = seed_mapping(make_device("refresh-hidden-replacement"), "default", own=OOB_ID)
    seed_modules_snapshot(planned, INVENTORY)
    seed_modules_snapshot(replacement, INVENTORY)
    planned_key = source_cache.snapshot_key(planned, Resource.MODULES, "default")
    replacement_key = source_cache.snapshot_key(replacement, Resource.MODULES, "default")
    old_planned = cache.get(planned_key)
    replacement_payload = cache.get(replacement_key)
    assert old_planned is not None and replacement_payload is not None
    planned_coordinator = SyncCacheConsistency(planned)
    replacement_coordinator = SyncCacheConsistency(replacement)
    planned_coordinator.mark_refresh_success(SyncTab.MODULES, "default")
    replacement_coordinator.mark_refresh_success(SyncTab.MODULES, "default")
    planned_state_key = planned_coordinator.state_key(SyncTab.MODULES, "default")
    replacement_state_key = replacement_coordinator.state_key(SyncTab.MODULES, "default")
    replacement_state = cache.get(replacement_state_key)
    user = make_user_with_perms(
        "refresh-planned-snapshot", [("view", Device)], constraints={"pk__in": [planned.pk, subject.pk]}
    )

    def change_owner():
        Device.objects.filter(pk=planned.pk).update(virtual_chassis=None, vc_position=None)
        Device.objects.filter(pk=replacement.pk).update(virtual_chassis=chassis, vc_position=1)
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=replacement)

    serve(live_librenms.server)
    if phase == "outcome":
        real_store = source_cache.store

        def store_then_change(*args):
            stored = real_store(*args)
            assert stored
            change_owner()
            return stored

        monkeypatch.setattr(source_cache, "store", store_then_change)
    else:
        if phase == "discovery":
            seed_mapping_row(planned, type(planned)())
        real_resolve = BaseModuleTableView.resolve_librenms_id

        def resolve_with_owner_change(view, owner):
            assert owner.pk == planned.pk
            if phase == "discovery":
                change_owner()
                return real_resolve(view, owner)
            identity = real_resolve(view, owner)
            change_owner()
            return identity

        monkeypatch.setattr(BaseModuleTableView, "resolve_librenms_id", resolve_with_owner_change)
    client.force_login(user)
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[subject.pk]),
        {"server_key": "default"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response.context["module_sync"]["table"] is None
    if phase == "outcome":
        assert cache.get(planned_key) != old_planned
    else:
        assert cache.get(planned_key) is None
    assert cache.get(replacement_key) == replacement_payload
    assert source_cache.read(replacement, Resource.MODULES, "default") is not None
    assert cache.get(replacement_state_key) == replacement_state
    assert replacement_coordinator.status("default")["modules"]["snapshot_available"]
    assert cache.get(planned_state_key)["state"] == SyncTabState.REFRESH_FAILED.value
    assert live_librenms.server.requests
    assert all(str(OOB_ID) not in request["path"] for request in live_librenms.server.requests)
