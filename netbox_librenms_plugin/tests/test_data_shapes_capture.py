"""
Round-trip tests for data-shape capture: capture from a seeded mock, replay, assert same outcome.

These exercise the real LibreNMSAPI (via get_response) over real HTTP against the mock server, then
re-run the real detection / relationship-resolution logic on the captured recording — proving
capture and replay are faithful.
"""

import pytest

from netbox_librenms_plugin.data_shapes.capture import capture_device_recording
from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature
from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links
from netbox_librenms_plugin.tests.recordings import load_recording
from netbox_librenms_plugin.tests.source_snapshot_helpers import (
    recording_serial_projection,
    recording_interface_assessment,
)

# capture_device_recording snapshots the device OS's PortStackLagPattern rows into the
# recording (recording["lag_patterns"]), so every capture touches the DB.
pytestmark = pytest.mark.django_db


# The row list each collection route answers with. An unregistered route must fall back to a
# WELL-FORMED empty body for its own field, the way a real LibreNMS does — a bare {"status": "ok"}
# is a malformed payload that the capture rightly refuses.
_STUB_ROW_FIELDS = {
    "all": "inventory",
    "ip": "addresses",
    "links": "links",
    "ports": "ports",
    "transceivers": "transceivers",
}


_STACK_RECORDING_ID = load_recording("cisco-stackwise-3member")["device_id"]
_SINGLE_RECORDING_ID = load_recording("cisco-lag-and-subinterface")["device_id"]


def _subject(api, device_id, *, oob_id=None):
    """Configure acquisition for the real HTTP client and return a mapped NetBox subject."""
    from copy import deepcopy
    from uuid import uuid4
    from django.conf import settings
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    config = deepcopy(settings.PLUGINS_CONFIG)
    config["netbox_librenms_plugin"]["servers"] = {
        api.server_key: {"librenms_url": api.librenms_url, "api_token": "test-token", "verify_ssl": False}
    }
    settings.PLUGINS_CONFIG = config
    return seed_mapping(make_device(f"capture-{uuid4().hex}"), api.server_key, own=device_id, oob=oob_id)


def _register_answer(server, path, answer):
    """Serve a status and JSON body, or disconnect at the real HTTP boundary."""
    status, body = answer
    if status is None:
        server.register_disconnect(f"/api/v0/{path}")
    else:
        server.register(f"/api/v0/{path}", body, status=status)


@pytest.fixture
def capture_http(live_librenms):
    """Serve controlled capture responses through the local HTTP stub."""

    def serve(routes):
        server = live_librenms.server
        device_ids = {path.split("/")[1] for path in routes if path.startswith(("devices/", "inventory/"))}
        for device_id in device_ids:
            for suffix, field in _STUB_ROW_FIELDS.items():
                path = f"inventory/{device_id}/all" if suffix == "all" else f"devices/{device_id}/{suffix}"
                _register_answer(server, path, (200, {"status": "ok", field: []}))
        server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": []})
        for path, answer in routes.items():
            _register_answer(server, path, answer)
        return server, live_librenms.api

    return serve


def _with_empty_inventory_source(recording):
    """Add definitive empty inventory responses to a capture source recording."""
    device_id = recording["device_id"]
    responses = recording["responses"]
    responses[f"GET /api/v0/inventory/{device_id}?entPhysicalContainedIn=0"] = {
        "status": "ok",
        "inventory": [],
    }
    responses[f"GET /api/v0/inventory/{device_id}/all"] = {"status": "ok", "inventory": []}
    return recording


def _transceiver_serial_seed():
    """Build a seed with per-device transceivers and instance-wide target, unrelated, and non-serial sensors."""
    return _with_empty_inventory_source(
        {
            "schema_version": 1,
            "name": "transceiver-serial-seed",
            "device_id": 2000,
            "meta": {"os": "sros"},
            "responses": {
                "GET /api/v0/devices/2000": {
                    "status": "ok",
                    "devices": [{"device_id": 2000, "os": "sros", "hostname": "acs01.example.net"}],
                },
                # Required structural routes: capture fails loudly on an error status for these
                # (a stale librenms_id answers 404 everywhere), so the seed must serve them.
                "GET /api/v0/devices/2000/ports": {"status": "ok", "ports": []},
                "GET /api/v0/devices/2000/port_stack": {"status": "ok", "mappings": []},
                "GET /api/v0/devices/2000/transceivers": {
                    "status": "ok",
                    "transceivers": [
                        {
                            "port_id": 519,
                            "entity_physical_index": 1610899520,
                            "type": "CFP2/QSFP28",
                            "model": "3HE10550AARA01",
                            "serial": "X42AU0D",
                            "channels": 4,
                            "connector": "LC",
                            "wavelength": 1301,
                        }
                    ],
                },
                "GET /api/v0/resources/sensors": {
                    "status": "ok",
                    "sensors": [
                        {
                            "sensor_id": 1,
                            "device_id": 2000,
                            "sensor_type": "acsSerialPortTable",
                            "sensor_index": "acsSerialPortTableStatus.7",
                            "sensor_descr": "ttyS7 Status",
                            "sensor_class": "state",
                        },
                        {
                            "sensor_id": 2,
                            "device_id": 2000,
                            "sensor_type": "acsSerialPortTable",
                            "sensor_index": "acsSerialPortTableStatus.8",
                            "sensor_descr": "PROD-LAB03A-RA1 Status",
                            "sensor_class": "state",
                        },
                        {
                            "sensor_id": 99,
                            "device_id": 3000,
                            "sensor_type": "acsSerialPortTable",
                            "sensor_index": "acsSerialPortTableStatus.1",
                            "sensor_descr": "OTHER-DEVICE Status",
                            "sensor_class": "state",
                        },
                        {
                            "sensor_id": 50,
                            "device_id": 2000,
                            "sensor_type": "temperature",
                            "sensor_index": "tempSensor.1",
                            "sensor_descr": "CPU temperature",
                            "sensor_class": "temperature",
                        },
                    ],
                },
            },
        }
    )


def test_capture_records_verbatim_transceiver_body(recording_server):
    """The per-device transceivers route is recorded byte-for-byte (safe — not instance-wide)."""
    seed = _transceiver_serial_seed()
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, 2000),
        server_key=api.server_key,
    )

    assert (
        captured["responses"]["GET /api/v0/devices/2000/transceivers"]
        == seed["responses"]["GET /api/v0/devices/2000/transceivers"]
    )


def test_capture_meta_os_does_not_override_captured_device_os(recording_server):
    """A caller-supplied meta['os'] must NOT override the OS captured from the device-info response (it scopes signature/novelty); other caller meta keys are preserved."""
    seed = _transceiver_serial_seed()  # device 2000 reports os "sros"
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, 2000), server_key=api.server_key, meta={"os": "SPOOFED", "note": "kept"}
    )

    assert captured["meta"]["os"] == "sros"  # the captured device OS wins over the caller's
    assert captured["meta"]["note"] == "kept"  # other caller meta survives


def test_capture_serial_sensors_excludes_other_devices(recording_server):
    """The instance-wide /resources/sensors route is device-filtered before recording (no cross-device PII)."""
    seed = _transceiver_serial_seed()
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, 2000),
        server_key=api.server_key,
    )

    recorded = captured["responses"]["GET /api/v0/resources/sensors"]["sensors"]
    # Only the target device's serial sensors survive: device 3000's sensor and the non-serial
    # temperature sensor are both gone — so the recording can never publish another device's data.
    assert {s["sensor_id"] for s in recorded} == {1, 2}
    assert all(s["device_id"] == 2000 for s in recorded)
    assert all(s["sensor_type"] == "acsSerialPortTable" for s in recorded)


def test_capture_serial_sensors_reads_the_server_every_time(recording_server):
    """Capture must fetch sensors from the server, so a recording always holds the current shape."""
    seed = _transceiver_serial_seed()  # server serves sensors 1 & 2 for device 2000
    server, api = recording_server(seed)

    sensors_status, sensors_body = server.routes["GET /api/v0/resources/sensors"]
    hits = []

    def _counting_sensors(method, path, query, headers, body):
        hits.append(path)
        return sensors_status, sensors_body

    server.register("/api/v0/resources/sensors", _counting_sensors, method="GET")

    # An earlier refresh in the same process must not let the capture skip the server.
    ok, _ = recording_serial_projection(api, {"device_id": 2000})
    assert ok
    assert len(hits) == 1
    hits.clear()

    captured = capture_device_recording(
        _subject(api, 2000),
        server_key=api.server_key,
    )

    # The capture asked the server itself rather than reusing the previous read.
    assert hits == ["/api/v0/resources/sensors"]
    recorded = {s["sensor_id"] for s in captured["responses"]["GET /api/v0/resources/sensors"]["sensors"]}
    assert recorded == {1, 2}


def test_capture_serial_sensors_roundtrip_outcome(recording_server):
    """Replaying the captured recording yields the same serial-link rows the source produced."""
    seed = _transceiver_serial_seed()
    _server, api = recording_server(seed)
    source_ok, source_sensors = recording_serial_projection(api, {"device_id": 2000})
    assert source_ok
    source_links = map_sensors_to_serial_links(source_sensors, device_id=2000)

    captured = capture_device_recording(
        _subject(api, 2000),
        server_key=api.server_key,
    )

    _server2, api2 = recording_server(captured, server_key="replay")
    replay_ok, replay_sensors = recording_serial_projection(api2, {"device_id": 2000})
    assert replay_ok
    replay_links = map_sensors_to_serial_links(replay_sensors, device_id=2000)

    assert replay_links == source_links
    # Non-vacuous: the rows really exercise both is_configured states (default ttyS7 vs custom label).
    assert {row["is_configured"] for row in replay_links} == {True, False}


def test_capture_embeds_serial_type_patterns(recording_server):
    """Capture snapshots the serial recognition map so custom patterns remain reproducible."""
    from netbox_librenms_plugin.models import SerialSensorTypePattern
    from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns

    SerialSensorTypePattern.objects.create(sensor_type="captestSerialTable", port_name_pattern="cap{N}")
    _server, api = recording_server(_transceiver_serial_seed())

    captured = capture_device_recording(
        _subject(api, 2000),
        server_key=api.server_key,
    )

    assert captured["serial_type_patterns"] == get_serial_sensor_type_patterns()
    assert captured["serial_type_patterns"]["captestSerialTable"] == "cap{N}"


def test_serial_roundtrip_survives_empty_pattern_table(recording_server):
    """A recording's serial recognition map reproduces source rows when the replay table is empty."""
    from netbox_librenms_plugin.models import SerialSensorTypePattern

    seed = _transceiver_serial_seed()
    _server, api = recording_server(seed)
    source_ok, source_sensors = recording_serial_projection(api, {"device_id": 2000})
    assert source_ok
    source_links = map_sensors_to_serial_links(source_sensors, device_id=2000)
    assert source_links  # non-vacuous: there must be rows to reproduce

    captured = capture_device_recording(
        _subject(api, 2000),
        server_key=api.server_key,
    )

    SerialSensorTypePattern.objects.all().delete()  # the bare replay host

    _server2, api2 = recording_server(captured, server_key="replay")
    replay_ok, replay_sensors = recording_serial_projection(
        api2, {"device_id": 2000}, sensor_types=captured["serial_type_patterns"]
    )
    assert replay_ok
    replay_links = map_sensors_to_serial_links(
        replay_sensors, device_id=2000, sensor_types=captured["serial_type_patterns"]
    )

    assert replay_links == source_links


def test_capture_skips_sensors_route_when_device_has_none(recording_server):
    """A device with no serial sensors must NOT add the instance-wide /resources/sensors route."""
    seed = _transceiver_serial_seed()
    # Strip every serial sensor for the target device; only device 3000 remains.
    seed["responses"]["GET /api/v0/resources/sensors"]["sensors"] = [
        s for s in seed["responses"]["GET /api/v0/resources/sensors"]["sensors"] if s["device_id"] != 2000
    ]
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, 2000),
        server_key=api.server_key,
    )

    assert "GET /api/v0/resources/sensors" not in captured["responses"]
    assert "serial_sensors" not in captured["meta"]


@pytest.mark.parametrize("status", [500, 503])
def test_capture_marks_serial_sensors_unavailable_when_the_sensor_table_fails(recording_server, status):
    """A failing instance-wide sensor table leaves the capture complete and marks serial sensors as unknown."""
    server, api = recording_server(_transceiver_serial_seed())
    server.register("/api/v0/resources/sensors", {"message": "Server Error"}, status=status, method="GET")

    captured = capture_device_recording(
        _subject(api, 2000), server_key=api.server_key, meta={"serial_sensors": "spoofed"}
    )

    assert captured["meta"]["serial_sensors"] == "unavailable"
    assert "GET /api/v0/resources/sensors" not in captured["responses"]
    assert "serial_type_patterns" not in captured
    assert "GET /api/v0/devices/2000/transceivers" in captured["responses"]


def test_capture_drops_a_caller_supplied_serial_sensor_marker(recording_server):
    """Only the capture itself may mark serial sensors as unavailable."""
    _server, api = recording_server(_transceiver_serial_seed())

    captured = capture_device_recording(
        _subject(api, 2000), server_key=api.server_key, meta={"serial_sensors": "unavailable"}
    )

    assert "serial_sensors" not in captured["meta"]
    assert {s["sensor_id"] for s in captured["responses"]["GET /api/v0/resources/sensors"]["sensors"]} == {1, 2}


def _vrf_seed(*, ports, vrfs, ip_addresses=None, links=None):
    """Build a capture source serving the VRF / IP / links routes the tabs read."""
    responses = {
        "GET /api/v0/devices/3100": {
            "status": "ok",
            "devices": [{"device_id": 3100, "os": "timos", "hostname": "rtr.example.net"}],
        },
        "GET /api/v0/devices/3100/ports": {"status": "ok", "ports": ports},
        "GET /api/v0/devices/3100/port_stack": {"status": "ok", "mappings": []},
        "GET /api/v0/devices/3100/transceivers": {"status": "ok", "transceivers": []},
        "GET /api/v0/devices/3100/ip": {"status": "ok", "addresses": ip_addresses or []},
        "GET /api/v0/devices/3100/links": {"status": "ok", "links": links or []},
        "GET /api/v0/routing/vrf": {"status": "ok", "vrfs": vrfs, "count": len(vrfs)},
    }
    return _with_empty_inventory_source(
        {
            "schema_version": 1,
            "name": "vrf-seed",
            "device_id": 3100,
            "meta": {"os": "timos"},
            "responses": responses,
        }
    )


def test_capture_records_the_ip_and_links_routes(recording_server):
    """The IP and cables tabs read these; without them no recording can drive either tab."""
    seed = _vrf_seed(
        ports=[{"port_id": 1, "ifName": "1/1/c1/1", "ifVrf": 0}],
        vrfs=[],
        ip_addresses=[{"ipv4_address": "192.0.2.10", "ipv4_prefixlen": 31, "port_id": 1}],
        links=[{"id": 7, "local_port_id": 1, "remote_hostname": "peer.example.net", "remote_port": "et-0/0/1"}],
    )
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, 3100),
        server_key=api.server_key,
    )

    assert captured["responses"]["GET /api/v0/devices/3100/ip"] == seed["responses"]["GET /api/v0/devices/3100/ip"]
    assert (
        captured["responses"]["GET /api/v0/devices/3100/links"] == seed["responses"]["GET /api/v0/devices/3100/links"]
    )


def test_capture_records_only_this_devices_vrfs(recording_server):
    """/routing/vrf is instance-wide by default; a recording must never carry another device's VRFs."""
    seed = _vrf_seed(
        ports=[{"port_id": 1, "ifName": "1/1/c1/1", "ifVrf": 7}],
        vrfs=[
            {"vrf_id": 7, "vrf_name": "customer-a", "device_id": 3100},
            {"vrf_id": 9, "vrf_name": "someone-elses", "device_id": 4200},
        ],
    )
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, 3100),
        server_key=api.server_key,
    )

    recorded = captured["responses"]["GET /api/v0/routing/vrf"]
    assert [row["vrf_id"] for row in recorded["vrfs"]] == [7]
    assert "someone-elses" not in str(recorded)


@pytest.mark.parametrize("missing_owner", [{}, {"device_id": None}])
def test_capture_rejects_vrfs_without_a_device(missing_owner, recording_server):
    seed = _vrf_seed(
        ports=[{"port_id": 1, "ifName": "eth0", "ifVrf": 7}],
        vrfs=[
            {"vrf_id": 7, "vrf_name": "customer-a", "device_id": 3100},
            {"vrf_id": 9, "vrf_name": "unknown-owner", **missing_owner},
        ],
    )
    _server, api = recording_server(seed)

    with pytest.raises(RuntimeError, match="VRF.*device_id"):
        capture_device_recording(
            _subject(api, 3100),
            server_key=api.server_key,
        )


def test_capture_skips_the_vrf_route_when_no_port_is_vrf_tagged(recording_server):
    """Most devices have no VRF, and the instance-wide route must not reach their recordings."""
    seed = _vrf_seed(
        ports=[{"port_id": 1, "ifName": "eth0", "ifVrf": 0}, {"port_id": 2, "ifName": "eth1", "ifVrf": None}],
        vrfs=[{"vrf_id": 7, "vrf_name": "customer-a", "device_id": 3100}],
    )
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, 3100),
        server_key=api.server_key,
    )

    assert "GET /api/v0/routing/vrf" not in captured["responses"]


def test_capture_replays_the_vrf_route_for_the_reader(recording_server):
    """Round-trip: the recorded VRF body is what acquisition reads back on replay."""
    seed = _vrf_seed(
        ports=[{"port_id": 1, "ifName": "1/1/c1/1", "ifVrf": 7}],
        vrfs=[{"vrf_id": 7, "vrf_name": "customer-a", "device_id": 3100}],
    )
    _server, api = recording_server(seed)
    captured = capture_device_recording(
        _subject(api, 3100),
        server_key=api.server_key,
    )

    _server2, api2 = recording_server(captured)
    from netbox_librenms_plugin.tests.source_snapshot_helpers import acquire_recording
    from netbox_librenms_plugin.acquisition.registry import Resource

    snapshot, replay_subject = acquire_recording(api2, captured, [Resource.IP_ADDRESSES])
    rows = snapshot.value(Resource.IP_ADDRESSES, "vrfs")
    assert [row["vrf_name"] for row in rows] == ["customer-a"]


def test_capture_fails_when_the_vrf_read_fails(recording_server):
    """A VRF-tagged device whose VRF table cannot be read must not ship as a complete capture."""
    seed = _vrf_seed(ports=[{"port_id": 1, "ifName": "1/1/c1/1", "ifVrf": 7}], vrfs=[])
    seed["responses"]["GET /api/v0/routing/vrf"] = {"status": "error", "message": "boom"}
    _server, api = recording_server(seed)

    with pytest.raises(RuntimeError, match="routing/vrf"):
        capture_device_recording(
            _subject(api, 3100),
            server_key=api.server_key,
        )


def test_capture_roundtrip_preserves_vc_outcome(recording_server):
    """Capturing a VC device and replaying the capture yields the same detection result."""
    from netbox_librenms_plugin.models import LibreNMSSettings

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"vc_member_name_pattern": "-M{position}"})
    seed = load_recording("cisco-stackwise-3member")
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, seed["device_id"]), server_key=api.server_key, name="captured-cisco"
    )

    # os metadata is lifted verbatim from the captured device-info response (the bundled seed's OS
    # is already pseudonymized, and capture does not anonymize — it records what the server returns).
    assert captured["meta"]["os"] == seed["responses"][f"GET /api/v0/devices/{_STACK_RECORDING_ID}"]["devices"][0]["os"]
    # The capture recorded device info, both inventory variants, ports, and port_stack.
    keys = list(captured["responses"])
    assert f"GET /api/v0/devices/{_STACK_RECORDING_ID}" in keys
    assert any(f"inventory/{_STACK_RECORDING_ID}?entPhysicalContainedIn=0" in k for k in keys)
    assert f"GET /api/v0/inventory/{_STACK_RECORDING_ID}?entPhysicalContainedIn=1" in keys
    # The seed serves real empty ports and port_stack responses. Both routes are required,
    # so an omitted route would abort capture instead of storing its 404 response.
    assert captured["responses"][f"GET /api/v0/devices/{_STACK_RECORDING_ID}/ports"] == {"status": "ok", "ports": []}
    assert captured["responses"][f"GET /api/v0/devices/{_STACK_RECORDING_ID}/port_stack"] == {
        "status": "ok",
        "mappings": [],
    }

    # Round-trip: replay the CAPTURED recording in a fresh mock and re-run VC detection.
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

    _server2, api2 = recording_server(captured)
    result = detect_virtual_chassis_from_inventory(api2, _STACK_RECORDING_ID)

    assert result is not None
    assert result["member_count"] == 3
    assert [m["serial"] for m in result["members"]] == seed["expected"]["virtual_chassis"]["member_serials"]


def test_capture_roundtrip_preserves_a_junos_fpc_virtual_chassis(recording_server):
    """The capture records the root's direct children, so the FPC members replay as a stack."""
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

    seed = load_recording("juniper-ex4400-vc-2member")
    # The capture holds inventory only; capture also requires the port routes.
    device_id = seed["device_id"]
    seed["responses"][f"GET /api/v0/devices/{device_id}/ports"] = {"status": "ok", "ports": []}
    seed["responses"][f"GET /api/v0/devices/{device_id}/port_stack"] = {"status": "ok", "mappings": []}
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, seed["device_id"]), server_key=api.server_key, name="captured-junos"
    )

    _server2, api2 = recording_server(captured)
    result = detect_virtual_chassis_from_inventory(api2, seed["device_id"])
    assert result is not None
    assert [m["serial"] for m in result["members"]] == seed["expected"]["virtual_chassis"]["member_serials"]
    assert [m["position"] for m in result["members"]] == seed["expected"]["virtual_chassis"]["member_positions"]


def test_capture_roundtrip_preserves_port_relationships(recording_server):
    """Capturing a device with LAG + sub-interface rows preserves ports/port_stack on replay."""
    seed = _with_empty_inventory_source(load_recording("cisco-lag-and-subinterface"))
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, seed["device_id"]), server_key=api.server_key, name="captured-lag"
    )

    _server2, api2 = recording_server(captured)
    relationships = recording_interface_assessment(api2, captured)["port_stack_relationships"]
    lag = {str(k): str(v) for k, v in relationships["lag_members"].items()}
    sub = {str(k): str(v) for k, v in relationships["sub_interfaces"].items()}
    assert lag == {str(k): str(v) for k, v in seed["expected"]["lag_members"].items()}
    assert sub == {str(k): str(v) for k, v in seed["expected"]["sub_interfaces"].items()}


def test_capture_records_verbatim_port_body(recording_server):
    """The captured ports body matches the source server's ports payload byte-for-byte."""
    seed = _with_empty_inventory_source(load_recording("cisco-lag-and-subinterface"))
    _server, api = recording_server(seed)

    captured = capture_device_recording(
        _subject(api, _SINGLE_RECORDING_ID),
        server_key=api.server_key,
    )

    captured_ports = captured["responses"][f"GET /api/v0/devices/{_SINGLE_RECORDING_ID}/ports"]
    assert captured_ports == seed["responses"][f"GET /api/v0/devices/{_SINGLE_RECORDING_ID}/ports"]


def test_capture_mirrors_inventory_all_fallback_when_server_ignores_filters(recording_server):
    """A server that returns empty filtered inventory but populates /all must still capture the VC."""
    root = [{"entPhysicalIndex": 1, "entPhysicalClass": "stack", "entPhysicalContainedIn": 0}]
    members = [
        {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "chassis",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 200,
            "entPhysicalClass": "chassis",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 2,
        },
    ]
    seed = {
        "schema_version": 1,
        "name": "no-filter-srv",
        "device_id": 1000,
        "meta": {"os": "ios"},
        "responses": {
            "GET /api/v0/devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]},
            # The server ignores the entPhysical* filter params → empty filtered inventory...
            "GET /api/v0/inventory/1000?entPhysicalContainedIn=0": {"status": "ok", "inventory": []},
            "GET /api/v0/inventory/1000?entPhysicalContainedIn=1": {
                "status": "ok",
                "inventory": [],
            },
            # ...but /inventory/{id}/all is populated (the get_inventory_filtered fallback source).
            "GET /api/v0/inventory/1000/all": {"status": "ok", "inventory": root + members},
            "GET /api/v0/devices/1000/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Gi1/0/1", "ifType": "ethernetCsmacd"}],
            },
            "GET /api/v0/devices/1000/port_stack": {"status": "ok", "mappings": []},
        },
    }
    _server, api = recording_server(seed)
    captured = capture_device_recording(_subject(api, 1000), server_key=api.server_key, name="captured-fallback")

    # Without the fallback the filtered inventory is empty → the VC is silently downgraded to a plain
    # device. The capture must reproduce the entities under the filtered key the signature/replay read.
    sig = compute_shape_signature(captured)
    assert sig["virtual_chassis"]["present"] is True
    assert sig["virtual_chassis"]["member_count"] == 2


def test_capture_records_the_full_inventory_the_modules_tab_reads(recording_server):
    """A server that honors the inventory filters still gets /all recorded, because the Modules tab reads it."""
    chassis = {"entPhysicalIndex": 1, "entPhysicalClass": "chassis", "entPhysicalContainedIn": 0}
    psu = {
        "entPhysicalIndex": 20,
        "entPhysicalClass": "powerSupply",
        "entPhysicalContainedIn": 1,
        "entPhysicalModelName": "PWR-1",
    }
    seed = {
        "schema_version": 1,
        "name": "filter-srv",
        "device_id": 1000,
        "meta": {"os": "nxos"},
        "responses": {
            "GET /api/v0/devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "nxos"}]},
            "GET /api/v0/inventory/1000?entPhysicalContainedIn=0": {"status": "ok", "inventory": [chassis]},
            "GET /api/v0/inventory/1000?entPhysicalContainedIn=1": {"status": "ok", "inventory": [psu]},
            "GET /api/v0/inventory/1000/all": {"status": "ok", "inventory": [chassis, psu]},
            "GET /api/v0/devices/1000/ports": {"status": "ok", "ports": []},
            "GET /api/v0/devices/1000/port_stack": {"status": "ok", "mappings": []},
        },
    }
    _server, api = recording_server(seed)
    captured = capture_device_recording(
        _subject(api, 1000),
        server_key=api.server_key,
    )

    _replay_server, replay_api = recording_server(captured)
    assert replay_api.get_device_inventory(1000) == (True, [chassis, psu])


def test_capture_raises_when_filtered_inventory_request_has_transport_error(capture_http):
    """A filtered inventory transport failure must not fall back to /all as if the request returned an empty list."""
    routes = {
        "devices/1000": (200, {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]}),
        # Production returns this failure immediately. It does not consult /all.
        "inventory/1000": (None, None),
        # A successful /all response must not hide the failed filtered request.
        "inventory/1000/all": (200, {"status": "ok", "inventory": []}),
        "devices/1000/ports": (200, {"status": "ok", "ports": []}),
        "devices/1000/port_stack": (200, {"status": "ok", "mappings": []}),
        "devices/1000/transceivers": (200, {"status": "ok", "transceivers": []}),
    }
    server, api = capture_http(routes)

    with pytest.raises(RuntimeError, match="inventory/1000"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


def test_capture_rejects_transceivers_when_request_has_transport_error(recording_server):
    """A failed transceiver connection must not produce a recording with no optics."""
    server, api = recording_server(load_recording("cisco-stackwise-3member"))

    def refuse_request(**_request):
        raise ConnectionResetError("simulated connection reset")

    server.register(f"/api/v0/devices/{_STACK_RECORDING_ID}/transceivers", refuse_request, method="GET")

    with pytest.raises(RuntimeError, match="transceivers.*no HTTP response"):
        capture_device_recording(
            _subject(api, _STACK_RECORDING_ID),
            server_key=api.server_key,
        )


def test_capture_rejects_transceiver_server_error(capture_http):
    """An optional transceiver route's HTTP failure must not look like a successful no-optics capture."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "devices/1000/transceivers", (503, {"status": "error", "message": "unavailable"}))

    with pytest.raises(RuntimeError, match="transceivers"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


def _all_ok_routes():
    """Routes where every structural endpoint answers 200 — a happy-path capture base."""
    return {
        "devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]},
        "inventory/1000": {"status": "ok", "inventory": []},
        "inventory/1000/all": {"status": "ok", "inventory": []},
        "devices/1000/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
        "devices/1000/port_stack": {"status": "ok", "mappings": []},
        "devices/1000/transceivers": {"status": "ok", "transceivers": []},
    }


def test_capture_raises_when_required_route_has_transport_error(capture_http):
    """A required structural route (here port_stack) with no HTTP response must fail the capture, not persist a partial recording."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "devices/1000/port_stack", (None, None))

    with pytest.raises(RuntimeError, match="port_stack"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


def test_capture_ignores_caller_supplied_oob_id_without_controller(capture_http):
    """A caller-supplied meta['oob_id'] must not mark a plain recording as OOB — oob_id is authoritative and set only when controller ports are actually captured."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})

    captured = capture_device_recording(
        _subject(api, 1000), server_key=api.server_key, meta={"oob_id": 999, "note": "kept"}
    )

    # The spoofed oob_id is dropped (no controller was captured); unrelated caller meta is preserved.
    assert "oob_id" not in captured["meta"]
    assert captured["meta"].get("note") == "kept"
    assert compute_shape_signature(captured)["oob"] is False


def test_capture_raises_when_inventory_all_fallback_fails(capture_http):
    """Once the filtered inventory comes back empty, /all is the ONLY inventory source — a transport failure there must fail the capture, not silently record an empty (wrong-topology) inventory."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    # Filtered inventory is empty (see _all_ok_routes), so capture falls back to /all — make it fail.
    _register_answer(server, "inventory/1000/all", (None, None))

    with pytest.raises(RuntimeError, match="inventory/1000/all"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


def test_capture_raises_when_inventory_all_fallback_errors_5xx(capture_http):
    """A 5xx on the sole /all inventory source is a failed fetch, not "no inventory" — capture must raise."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    # Filtered inventory is empty, so /all is the only source. A server error there means we don't
    # KNOW the topology — recording an empty inventory would ship a VC device as a plain one.
    _register_answer(server, "inventory/1000/all", (500, {"status": "error", "message": "boom"}))

    with pytest.raises(RuntimeError, match="inventory/1000/all"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, None),
        (401, {"status": "error", "message": "unauthorized"}),
        (429, {"status": "error", "message": "rate limited"}),
    ],
)
def test_capture_raises_when_inventory_all_fallback_has_no_definitive_answer(status, body, capture_http):
    """A malformed or refused fallback must not synthesize a plain-device inventory."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "inventory/1000/all", (status, body))

    with pytest.raises(RuntimeError, match="inventory/1000/all"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


def test_capture_raises_when_inventory_all_fallback_returns_404(capture_http):
    """A missing /all endpoint must remain a failed production fallback."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "inventory/1000/all", (404, {"status": "error", "message": "not found"}))

    with pytest.raises(RuntimeError, match="inventory/1000/all"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


def test_capture_replays_successful_empty_inventory_fallback(recording_server, capture_http):
    """A successful empty /all fallback must remain successful on replay."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})

    captured = capture_device_recording(
        _subject(api, 1000),
        server_key=api.server_key,
    )
    _server, replay_api = recording_server(captured)

    assert replay_api.get_inventory_filtered(1000, ent_physical_contained_in=0) == (True, [])


def test_capture_omits_serial_type_patterns_when_device_has_no_serial_sensors(capture_http):
    """serial_type_patterns is snapshotted ONLY when the device has serial sensors, not embedded in every capture."""
    from netbox_librenms_plugin.models import SerialSensorTypePattern

    SerialSensorTypePattern.objects.create(sensor_type="captestSerialTable", port_name_pattern="cap{N}")
    # A definitive empty sensors answer exports no route or recognition map.
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})

    captured = capture_device_recording(
        _subject(api, 1000),
        server_key=api.server_key,
    )

    assert "GET /api/v0/resources/sensors" not in captured["responses"]
    assert "serial_type_patterns" not in captured


def test_capture_fails_when_configured_oob_controller_ports_fail(capture_http):
    """A failed configured OOB fetch must not become a successful non-OOB capture."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "devices/2000/ports", (None, None))

    with pytest.raises(RuntimeError, match="devices/2000/ports"):
        capture_device_recording(
            _subject(api, 1000, oob_id=2000),
            server_key=api.server_key,
        )


def test_capture_fails_when_successful_route_has_no_json_body(capture_http):
    """A 2xx HTML response must not be recorded as an absent structural payload."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "devices/1000/ports", (200, None))

    with pytest.raises(RuntimeError, match="devices/1000/ports"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


@pytest.mark.parametrize(
    ("path", "payload", "oob_id"),
    [
        ("devices/1000", {"status": "error", "devices": []}, None),
        ("devices/1000", {"status": "ok", "devices": [None]}, None),
        ("inventory/1000", {"status": "ok", "inventory": [None]}, None),
        ("inventory/1000/all", {"status": "ok", "inventory": [None]}, None),
        ("devices/1000/ports", {"status": "ok", "ports": [None]}, None),
        ("devices/1000/port_stack", {"status": "error", "mappings": []}, None),
        ("devices/1000/transceivers", {"status": "ok", "transceivers": [None]}, None),
        ("devices/2000/ports", {"status": "error", "ports": []}, 2000),
    ],
)
def test_capture_rejects_malformed_success_envelopes(path, payload, oob_id, capture_http):
    """A valid JSON error or malformed row list must not become absent topology."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, path, (200, payload))

    with pytest.raises(RuntimeError, match=path):
        capture_device_recording(
            _subject(api, 1000, oob_id=oob_id),
            server_key=api.server_key,
        )


@pytest.mark.parametrize("payload", [{"mappings": []}, {"status": "OK", "mappings": []}])
def test_capture_accepts_port_stack_success_envelopes(payload, capture_http):
    """Capture must accept every success envelope that production port-stack replay accepts."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "devices/1000/port_stack", (200, payload))

    captured = capture_device_recording(
        _subject(api, 1000),
        server_key=api.server_key,
    )

    assert captured["responses"]["GET /api/v0/devices/1000/port_stack"] == payload


@pytest.mark.parametrize("mapping", [{}, {"high_port_id": 1}, {"low_port_id": 2}])
def test_capture_rejects_incomplete_port_stack_mappings(mapping, capture_http):
    """Capture must reject a port-stack row that production replay cannot consume."""
    server, api = capture_http({k: (200, v) for k, v in _all_ok_routes().items()})
    _register_answer(server, "devices/1000/port_stack", (200, {"status": "ok", "mappings": [mapping]}))

    with pytest.raises(RuntimeError, match="port_stack"):
        capture_device_recording(
            _subject(api, 1000),
            server_key=api.server_key,
        )


def test_capture_required_route_http_error_raises(capture_http):
    """A real HTTP error (404) on a required structural route must fail the capture loudly — a stale librenms_id answers 404 everywhere and would otherwise ship a junk all-404 recording presented as a success."""
    server, api = capture_http({"devices/55": (404, {"status": "error", "message": "Device not found"})})

    with pytest.raises(RuntimeError, match="HTTP 404"):
        capture_device_recording(
            _subject(api, 55),
            server_key=api.server_key,
        )


@pytest.mark.django_db
def test_capture_snapshots_os_scoped_lag_patterns(capture_http):
    """The recording carries the device OS's PortStackLagPattern regexes — signature and replay read recording["lag_patterns"], so a pattern-based LAG shape is unfingerprintable/unreplayable without the snapshot."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    # Unique OS names: default patterns for real OSes may already be seeded (CI-unique).
    PortStackLagPattern.objects.create(librenms_os="captest-os", lag_name_pattern=r"^Po\d+$")
    PortStackLagPattern.objects.create(librenms_os="captest-other", lag_name_pattern=r"^ae\d+$")
    server, api = capture_http(
        {
            "devices/77": (200, {"status": "ok", "devices": [{"device_id": 77, "os": "captest-os"}]}),
            "inventory/77": (200, {"status": "ok", "inventory": []}),
            "inventory/77/all": (200, {"status": "ok", "inventory": []}),
            "devices/77/ports": (200, {"status": "ok", "ports": []}),
            "devices/77/port_stack": (200, {"status": "ok", "mappings": []}),
            "devices/77/transceivers": (200, {"status": "ok", "transceivers": []}),
        }
    )

    recording = capture_device_recording(
        _subject(api, 77),
        server_key=api.server_key,
    )

    # Scoped to the captured OS: the other platform's pattern is not embedded.
    assert recording["lag_patterns"] == {"captest-os": r"^Po\d+$"}


@pytest.mark.django_db
def test_capture_uses_the_models_normalized_os_scope(capture_http):
    """Capture must include a stored OS rule whose surrounding whitespace is insignificant."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    pattern = PortStackLagPattern.objects.create(
        librenms_os="captest-normalized",
        lag_name_pattern=r"^Bundle\d+$",
        sap_name_pattern=":",
    )
    PortStackLagPattern.objects.filter(pk=pattern.pk).update(librenms_os="  captest-normalized  ")
    server, api = capture_http(
        {
            "devices/87": (200, {"status": "ok", "devices": [{"device_id": 87, "os": "captest-normalized"}]}),
            "inventory/87": (200, {"status": "ok", "inventory": []}),
            "inventory/87/all": (200, {"status": "ok", "inventory": []}),
            "devices/87/ports": (200, {"status": "ok", "ports": []}),
            "devices/87/port_stack": (200, {"status": "ok", "mappings": []}),
            "devices/87/transceivers": (200, {"status": "ok", "transceivers": []}),
        }
    )

    recording = capture_device_recording(
        _subject(api, 87),
        server_key=api.server_key,
    )

    assert recording["lag_patterns"] == {"  captest-normalized  ": r"^Bundle\d+$"}
    assert recording["sap_patterns"] == {"  captest-normalized  ": ":"}


@pytest.mark.django_db
def test_capture_omits_an_empty_lag_pattern(capture_http):
    """A database-level blank must not become a regex that classifies every port as a LAG."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.bulk_create([PortStackLagPattern(librenms_os="captest-empty-lag", lag_name_pattern="")])
    server, api = capture_http(
        {
            "devices/77": (200, {"status": "ok", "devices": [{"device_id": 77, "os": "captest-empty-lag"}]}),
            "inventory/77": (200, {"status": "ok", "inventory": []}),
            "inventory/77/all": (200, {"status": "ok", "inventory": []}),
            "devices/77/ports": (200, {"status": "ok", "ports": []}),
            "devices/77/port_stack": (200, {"status": "ok", "mappings": []}),
            "devices/77/transceivers": (200, {"status": "ok", "transceivers": []}),
        }
    )

    assert (
        capture_device_recording(
            _subject(api, 77),
            server_key=api.server_key,
        )["lag_patterns"]
        == {}
    )


@pytest.mark.django_db
def test_capture_snapshots_the_os_sap_pattern_and_omits_a_blank_one(capture_http):
    """A replay without the SAP rule resolves a service access point as a LAG member, so the rule travels with the recording exactly as the LAG pattern does."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.create(librenms_os="captest-sap", lag_name_pattern=r"^lag-\d+$", sap_name_pattern=":")
    # An OS with no SAP notation contributes no key rather than a pattern matching nothing.
    PortStackLagPattern.objects.create(librenms_os="captest-nosap", lag_name_pattern=r"^ae\d+$")
    server, api = capture_http(
        {
            "devices/78": (200, {"status": "ok", "devices": [{"device_id": 78, "os": "captest-sap"}]}),
            "inventory/78": (200, {"status": "ok", "inventory": []}),
            "inventory/78/all": (200, {"status": "ok", "inventory": []}),
            "devices/78/ports": (200, {"status": "ok", "ports": []}),
            "devices/78/port_stack": (200, {"status": "ok", "mappings": []}),
            "devices/78/transceivers": (200, {"status": "ok", "transceivers": []}),
        }
    )

    recording = capture_device_recording(
        _subject(api, 78),
        server_key=api.server_key,
    )

    assert recording["sap_patterns"] == {"captest-sap": ":"}

    server_nosap, api_nosap = capture_http(
        {
            "devices/79": (200, {"status": "ok", "devices": [{"device_id": 79, "os": "captest-nosap"}]}),
            "inventory/79": (200, {"status": "ok", "inventory": []}),
            "inventory/79/all": (200, {"status": "ok", "inventory": []}),
            "devices/79/ports": (200, {"status": "ok", "ports": []}),
            "devices/79/port_stack": (200, {"status": "ok", "mappings": []}),
            "devices/79/transceivers": (200, {"status": "ok", "transceivers": []}),
        }
    )

    assert (
        capture_device_recording(
            _subject(api_nosap, 79),
            server_key=api_nosap.server_key,
        )["sap_patterns"]
        == {}
    )


@pytest.mark.django_db
@pytest.mark.parametrize("blank_os", ["", "   "])
def test_capture_blank_os_embeds_no_lag_patterns(blank_os, capture_http):
    """A present but blank OS embeds no LAG patterns because production applies none."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.create(librenms_os="captest-blank", lag_name_pattern=r"^Po\d+$")
    server, api = capture_http(
        {
            "devices/78": (200, {"status": "ok", "devices": [{"device_id": 78, "os": blank_os}]}),
            "inventory/78": (200, {"status": "ok", "inventory": []}),
            "inventory/78/all": (200, {"status": "ok", "inventory": []}),
            "devices/78/ports": (200, {"status": "ok", "ports": []}),
            "devices/78/port_stack": (200, {"status": "ok", "mappings": []}),
            "devices/78/transceivers": (200, {"status": "ok", "transceivers": []}),
        }
    )

    recording = capture_device_recording(
        _subject(api, 78),
        server_key=api.server_key,
    )

    assert recording["lag_patterns"] == {}


@pytest.mark.django_db
def test_capture_without_an_os_embeds_no_lag_patterns(capture_http):
    """An unknown OS selects no vendor pattern."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.create(librenms_os="captest-noneos", lag_name_pattern=r"^Po\d+$")
    server, api = capture_http(
        {
            "devices/79": (200, {"status": "ok", "devices": [{"device_id": 79}]}),
            "inventory/79": (200, {"status": "ok", "inventory": []}),
            "inventory/79/all": (200, {"status": "ok", "inventory": []}),
            "devices/79/ports": (200, {"status": "ok", "ports": []}),
            "devices/79/port_stack": (200, {"status": "ok", "mappings": []}),
            "devices/79/transceivers": (200, {"status": "ok", "transceivers": []}),
        }
    )

    recording = capture_device_recording(
        _subject(api, 79),
        server_key=api.server_key,
    )

    assert recording["lag_patterns"] == {}


class TestRecordedResponseEnvelope:
    """The stored status framing must not collide with a body LibreNMS can actually send."""

    def test_a_list_body_replays_whole(self):
        """
        `[1, {...}]` is a body, not a status and a body.

        The old framing read any two-item list whose head was an int as [status, body], so this
        body registered HTTP status 1 and lost its first item.
        """
        from netbox_librenms_plugin.data_shapes.envelope import unwrap_response, wrap_response

        body = [1, {"value": "x"}]

        status, replayed = unwrap_response(wrap_response(200, body))

        assert status == 200
        assert replayed == body

    def test_a_non_2xx_keeps_its_status(self):
        """The status still has to survive a round trip, or a 404 recording replays as 200."""
        from netbox_librenms_plugin.data_shapes.envelope import unwrap_response, wrap_response

        stored = wrap_response(404, {"status": "error"})

        assert unwrap_response(stored) == (404, {"status": "error"})
        assert not isinstance(stored, list), "a list frame is what made the format ambiguous"

    def test_a_2xx_body_is_stored_bare(self):
        """A successful body stays exactly as LibreNMS sent it, with no wrapper to strip."""
        from netbox_librenms_plugin.data_shapes.envelope import wrap_response

        assert wrap_response(200, {"ports": []}) == {"ports": []}

    def test_a_half_written_envelope_is_refused_rather_than_read_as_a_body(self):
        """
        The reserved status key has no other use, so a value carrying it must be complete.

        Otherwise it unwraps as a successful body that merely contains the key, turning a
        recorded error into a recorded success.
        """
        from netbox_librenms_plugin.data_shapes.envelope import STATUS_KEY
        from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

        recording = {
            "schema_version": 1,
            "name": "half",
            "description": "",
            "device_id": 1,
            "responses": {"GET /api/v0/devices/1": {STATUS_KEY: 404}},
        }

        errors = recording_schema_errors(recording)

        assert any(STATUS_KEY in error for error in errors), errors

    def test_a_recorded_list_body_passes_validation(self):
        """Capture stores a 2xx list body bare, so validation must not refuse it as a framing."""
        from netbox_librenms_plugin.data_shapes.envelope import wrap_response
        from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

        recording = {
            "schema_version": 1,
            "name": "list-body",
            "description": "",
            "device_id": 1,
            "responses": {"GET /api/v0/devices/1/ports": wrap_response(200, [1, {"value": "x"}])},
        }

        assert recording_schema_errors(recording) == []


def test_a_list_body_survives_a_real_replay(recording_server):
    """
    End to end: a recorded list body replays whole, through the real loader and HTTP.

    `[1, {...}]` is exactly the shape the removed framing mistook for [status, body]: the loader
    registered HTTP status 1 and served only the second item.
    """
    recording = {
        "schema_version": 1,
        "name": "list-body",
        "description": "",
        "meta": {},
        "device_id": 77,
        "responses": {
            "GET /api/v0/devices/77": {"status": "ok", "devices": [{"device_id": 77, "os": "linux"}]},
            "GET /api/v0/devices/77/ports": [1, {"value": "x"}],
        },
    }

    _server, api = recording_server(recording)
    # get_response takes a path relative to /api/v0/.
    status, body = api.get_response("devices/77/ports", None)

    assert status == 200, "the first list item was read as an HTTP status"
    assert body == [1, {"value": "x"}]


def test_a_body_cannot_claim_the_reserved_status_key():
    """A mapping with the reserved key must satisfy the status-envelope contract."""
    from netbox_librenms_plugin.data_shapes.envelope import STATUS_KEY, unwrap_response

    body = {STATUS_KEY: 404, "status": "ok", "ports": []}

    with pytest.raises(ValueError, match="status envelope"):
        unwrap_response(body)
