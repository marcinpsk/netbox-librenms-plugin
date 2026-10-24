"""Capture exports acquisition responses and replays them through acquisition and assessment."""

import pytest

from netbox_librenms_plugin.acquisition.coordinator import acquire
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.assessment.cables import assess as assess_cables
from netbox_librenms_plugin.assessment.interfaces import assess_interfaces
from netbox_librenms_plugin.assessment.modules import assess as assess_modules
from netbox_librenms_plugin.data_shapes.capture import capture_device_recording
from netbox_librenms_plugin.data_shapes.recordings_store import SERIAL_SENSORS_META_KEY, SERIAL_SENSORS_UNAVAILABLE
from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis_members
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

RESOURCES = [Resource.INTERFACES, Resource.IP_ADDRESSES, Resource.CABLES, Resource.MODULES]
ROOT = {"entPhysicalIndex": 10, "entPhysicalContainedIn": 0, "entPhysicalClass": "stack"}
MEMBER = {
    "entPhysicalIndex": 11,
    "entPhysicalContainedIn": 10,
    "entPhysicalClass": "chassis",
    "entPhysicalSerialNum": "MEMBER",
}


def serve(server, device_id=5501, *, os_name="capture-test", sensors=()):
    server.register(
        f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [{"device_id": device_id, "os": os_name}]}
    )
    server.register(
        f"/api/v0/devices/{device_id}/ports",
        {"status": "ok", "ports": [{"port_id": 7, "ifName": "eth3", "ifType": "ethernetCsmacd"}]},
    )
    server.register(f"/api/v0/devices/{device_id}/port_stack", {"status": "ok", "mappings": []})
    server.register(f"/api/v0/devices/{device_id}/ip", {"status": "ok", "addresses": []})
    server.register(f"/api/v0/devices/{device_id}/links", {"status": "ok", "links": []})
    server.register(f"/api/v0/devices/{device_id}/transceivers", {"status": "ok", "transceivers": []})
    server.register(f"/api/v0/inventory/{device_id}/all", {"status": "ok", "inventory": [ROOT, MEMBER]})
    server.register(f"/api/v0/inventory/{device_id}", {"status": "ok", "inventory": []})
    server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": list(sensors)})


def capture(subject, *, include_serial=True):
    return capture_device_recording(
        subject,
        server_key="default",
        name="captured",
        description="Captured fixture.",
        meta={},
        include_serial=include_serial,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("vc_member", [False, True])
def test_capture_replays_source_acquisition_including_a_vc_member(
    live_librenms, configure_librenms, recording_server, vc_member
):
    if vc_member:
        _vc, (owner, subject) = make_virtual_chassis_members("capture-source-vc")
        seed_mapping(owner, "default", own=5501)
        seed_mapping(subject, "default", own=5502)
        serve(live_librenms.server, 5502)
    else:
        subject = seed_mapping(make_device("capture-source"), "default", own=5501)
    serve(live_librenms.server)
    before = acquire(subject, server_key="default", resources=RESOURCES, include_serial=True, inventory_probes=True)

    recording = capture(subject)

    assert recording["device_id"] == 5501
    assert recording["responses"]["GET /api/v0/inventory/5501?entPhysicalContainedIn=0"]["inventory"] == [ROOT]
    assert recording["responses"]["GET /api/v0/inventory/5501?entPhysicalContainedIn=10"]["inventory"] == [MEMBER]
    replay, replay_api = recording_server(recording)
    configure_librenms(
        {"default": {"librenms_url": replay.url, "api_token": replay_api.headers["X-Auth-Token"], "verify_ssl": False}}
    )
    after = acquire(subject, server_key="default", resources=RESOURCES, include_serial=True, inventory_probes=True)
    assert assess_modules(after, subject=subject) == assess_modules(before, subject=subject)
    assert assess_interfaces(after, "ifName") == assess_interfaces(before, "ifName")
    assert assess_cables(after, subject=subject) == assess_cables(before, subject=subject)
    assert after.value(Resource.IP_ADDRESSES, "addresses") == before.value(Resource.IP_ADDRESSES, "addresses")


@pytest.mark.django_db
def test_capture_modal_keeps_the_vc_member_as_the_acquisition_subject(client, live_librenms):
    from django.urls import reverse
    from netbox_librenms_plugin.tests.conftest import make_superuser

    _vc, (owner, subject) = make_virtual_chassis_members("capture-modal-vc")
    seed_mapping(owner, "default", own=5501)
    seed_mapping(subject, "default", own=5502)
    serve(live_librenms.server, 5501)
    serve(live_librenms.server, 5502)
    live_librenms.server.ports_response(5502, [{"port_id": 17, "ifName": "eth4", "ifType": "ethernetCsmacd"}])
    client.force_login(make_superuser("capture-modal-vc-user"))

    response = client.get(
        reverse("plugins:netbox_librenms_plugin:capture_data_shape", kwargs={"device_id": subject.pk}),
        {"server_key": "default"},
    )

    assert response.status_code == 200
    assert response.context["recording_json"]
    paths = {request["path"] for request in live_librenms.server.requests}
    assert "/api/v0/devices/5502/ip" in paths
    assert "/api/v0/devices/5501/ip" not in paths
    assert "/api/v0/inventory/5501/all" in paths


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["active", "deleted", "invalid", "failed", "not_requested"])
def test_capture_serial_projection_controls_the_recorded_route_and_map(live_librenms, state):
    subject = seed_mapping(make_device("capture-serial-projection"), "default", own=5501)
    sensor = {
        "sensor_id": 1,
        "device_id": 5501,
        "sensor_type": "acsSerialPortTable",
        "sensor_index": "acsSerialPortTableStatus.7",
        "sensor_descr": "ttyS7 Status",
    }
    if state == "deleted":
        sensor.update(sensor_deleted="1", sensor_id=False, sensor_index=None)
    if state == "invalid":
        sensor["sensor_deleted"] = True
    serve(live_librenms.server, sensors=[sensor])
    if state == "failed":
        live_librenms.server.register("/api/v0/resources/sensors", {"status": "error"}, status=500)

    recording = capture(subject, include_serial=state != "not_requested")

    if state == "active":
        assert recording["responses"]["GET /api/v0/resources/sensors"]["sensors"] == [sensor]
        assert "acsSerialPortTable" in recording["serial_type_patterns"]
    else:
        assert "GET /api/v0/resources/sensors" not in recording["responses"]
        assert "serial_type_patterns" not in recording
    assert (recording["meta"].get(SERIAL_SENSORS_META_KEY) == SERIAL_SENSORS_UNAVAILABLE) is (
        state in {"invalid", "failed", "not_requested"}
    )


@pytest.mark.django_db
def test_capture_without_an_os_uses_productions_lag_and_sap_scopes(live_librenms):
    subject = seed_mapping(make_device("capture-no-os"), "default", own=5501)
    serve(live_librenms.server, os_name=None)
    PortStackLagPattern.objects.create(
        librenms_os="capture-vendor", lag_name_pattern=r"^Bundle\d+$", sap_name_pattern=":"
    )

    recording = capture(subject)

    assert recording["lag_patterns"] == {}
    assert recording["sap_patterns"]["capture-vendor"] == ":"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path,field", [("devices/5501", "devices"), ("devices/5501/ports", "ports"), ("inventory/5501/all", "inventory")]
)
def test_capture_requires_success_envelopes_for_its_structural_sources(live_librenms, path, field):
    subject = seed_mapping(make_device("capture-success-envelope"), "default", own=5501)
    serve(live_librenms.server)
    body = {field: [{"device_id": 5501, "os": "capture-test"}]} if field == "devices" else {field: []}
    live_librenms.server.register(f"/api/v0/{path}", body)
    with pytest.raises(RuntimeError, match=path):
        capture(subject)
