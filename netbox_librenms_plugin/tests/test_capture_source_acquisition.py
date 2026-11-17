"""Capture exports acquisition responses and replays them through acquisition and assessment."""

from netbox_librenms_plugin.acquisition.coordinator import plan_acquisition
from netbox_librenms_plugin.data_shapes.capture import CAPTURE_RESOURCES

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
from netbox_librenms_plugin.tests.source_snapshot_helpers import module_serial_context

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
        plan_acquisition(subject, server_key="default", resources=CAPTURE_RESOURCES),
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
        # The master holds the mapping of the chassis, so it is the subject of the capture.
        _vc, (subject, _member) = make_virtual_chassis_members("capture-source-vc")
        seed_mapping(subject, "default", own=5501)
    else:
        subject = seed_mapping(make_device("capture-source"), "default", own=5501)
    serve(live_librenms.server)
    before = acquire(
        plan_acquisition(subject, server_key="default", resources=RESOURCES), include_serial=True, inventory_probes=True
    )

    recording = capture(subject)

    assert recording["device_id"] == 5501
    assert recording["responses"]["GET /api/v0/inventory/5501?entPhysicalContainedIn=0"]["inventory"] == [ROOT]
    assert recording["responses"]["GET /api/v0/inventory/5501?entPhysicalContainedIn=10"]["inventory"] == [MEMBER]
    replay, replay_api = recording_server(recording)
    configure_librenms(
        {"default": {"librenms_url": replay.url, "api_token": replay_api.headers["X-Auth-Token"], "verify_ssl": False}}
    )
    after = acquire(
        plan_acquisition(subject, server_key="default", resources=RESOURCES), include_serial=True, inventory_probes=True
    )
    assert assess_modules(after, subject=subject, serial_context=module_serial_context(subject)) == assess_modules(
        before, subject=subject, serial_context=module_serial_context(subject)
    )
    assert assess_interfaces(after, "ifName") == assess_interfaces(before, "ifName")
    assert assess_cables(after, subject=subject) == assess_cables(before, subject=subject)
    assert after.value(Resource.IP_ADDRESSES, "addresses") == before.value(Resource.IP_ADDRESSES, "addresses")


@pytest.mark.django_db
def test_capture_modal_captures_the_chassis_on_the_master_and_refuses_another_member(client, live_librenms):
    import json

    from django.urls import reverse
    from django.utils.html import escape

    from netbox_librenms_plugin.mapping_owner import NOT_OWNER_MESSAGE
    from netbox_librenms_plugin.tests.conftest import make_superuser

    _vc, (master, member) = make_virtual_chassis_members("capture-modal-vc")
    seed_mapping(master, "default", own=5501)
    serve(live_librenms.server, 5501)
    live_librenms.server.register(
        "/api/v0/devices/5501/ip",
        {"status": "ok", "addresses": [{"port_id": 7, "ipv4_address": "198.18.0.1", "ipv4_prefixlen": 24}]},
    )
    client.force_login(make_superuser("capture-modal-vc-user"))
    url = "plugins:netbox_librenms_plugin:capture_data_shape"

    refused = client.get(
        reverse(url, kwargs={"device_id": member.pk}), {"server_key": "default"}, HTTP_HX_REQUEST="true"
    )
    assert refused.status_code == 200
    assert escape(NOT_OWNER_MESSAGE) in refused.content.decode()
    assert live_librenms.server.requests == []

    response = client.get(reverse(url, kwargs={"device_id": master.pk}), {"server_key": "default"})

    assert response.status_code == 200
    recording = json.loads(response.context["recording_json"])
    addresses = next(body["addresses"] for key, body in recording["responses"].items() if key.endswith("/ip"))
    assert len(addresses) == 1
    paths = {request["path"] for request in live_librenms.server.requests}
    assert "/api/v0/devices/5501/ip" in paths
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


@pytest.mark.django_db
@pytest.mark.parametrize("oob_id", [5501, "5501"])
def test_capture_of_a_self_link_preserves_host_ports_without_an_oob_stamp(live_librenms, oob_id):
    subject = seed_mapping(make_device("capture-self-oob"), "default", own=5501, oob=oob_id)
    serve(live_librenms.server)

    recording = capture(subject)

    assert "oob_id" not in recording["meta"]
    assert recording["responses"]["GET /api/v0/devices/5501/ports"]["ports"] == [
        {"port_id": 7, "ifName": "eth3", "ifType": "ethernetCsmacd"}
    ]
    requests = [
        request
        for request in live_librenms.server.requests
        if request["path"] == "/api/v0/devices/5501/ports" and request["query"].get("with") == ["vlans"]
    ]
    assert len(requests) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("status", [200, 503])
def test_capture_excludes_neighbour_port_tables(live_librenms, status):
    subject = seed_mapping(make_device("capture-neighbour-table"), "default", own=5501)
    serve(live_librenms.server)
    live_librenms.server.register(
        "/api/v0/devices/5501/links",
        {"status": "ok", "links": [{"local_port_id": 7, "remote_device_id": 5503, "remote_port_id": 17}]},
    )
    live_librenms.server.register(
        "/api/v0/devices/5503/ports",
        {"status": "ok" if status == 200 else "error", "ports": [{"port_id": 17, "ifName": "neighbour-only"}]},
        status=status,
    )

    recording = capture(subject)

    assert "/api/v0/devices/5503/ports" in [entry["path"] for entry in live_librenms.server.requests]
    assert "GET /api/v0/devices/5503/ports" not in recording["responses"]
    assert "GET /api/v0/devices/5501/ports" in recording["responses"]


@pytest.mark.django_db
@pytest.mark.parametrize("explicit_sap", [False, True])
def test_recording_assessment_preserves_unnamed_os_patterns(live_librenms, recording_server, explicit_sap):
    from netbox_librenms_plugin.tests.source_snapshot_helpers import recording_interface_assessment

    subject = seed_mapping(make_device("recording-os-patterns"), "default", own=5501)
    serve(live_librenms.server, os_name="pattern-test")
    pattern = PortStackLagPattern.objects.create(librenms_os="pattern-test", lag_name_pattern=r"^Bundle\d+$")
    recording = capture(subject)
    recording["lag_patterns"] = {}
    recording["sap_patterns"] = {"pattern-test": ":"} if explicit_sap else {}
    _server, api = recording_server(recording)

    recording_interface_assessment(api, recording)

    pattern.refresh_from_db()
    if explicit_sap:
        assert all(regex.search("") is None for regex in PortStackLagPattern.compiled_patterns_for_os("pattern-test"))
    else:
        assert pattern.lag_name_pattern == r"^Bundle\d+$"


@pytest.mark.django_db
def test_capture_preserves_device_facts_needed_by_replay(live_librenms, recording_server):
    subject = seed_mapping(make_device("capture-device-replay-facts"), "default", own=5501)
    serve(live_librenms.server)
    kept = {"device_id": 5501, "os": "capture-test", "serial": "EXAMPLE-MASTER", "poller_group": 7}
    live_librenms.server.register(
        "/api/v0/devices/5501",
        {"status": "ok", "devices": [{**kept, "snmp_community": "example-secret"}]},
    )

    recording = capture(subject)
    _server, api = recording_server(recording)

    assert recording["responses"]["GET /api/v0/devices/5501"]["devices"] == [kept]
    assert api.get_device_info(5501, use_cache=False) == (True, kept)


@pytest.mark.django_db
def test_a_missing_transceiver_route_is_captured_and_replays_the_same_modules(
    live_librenms, configure_librenms, recording_server
):
    from netbox_librenms_plugin.data_shapes.envelope import unwrap_response

    subject = seed_mapping(make_device("capture-no-transceiver-route"), "default", own=5501)
    serve(live_librenms.server)
    missing = {"status": "error", "message": "Not Found"}
    live_librenms.server.register("/api/v0/devices/5501/transceivers", missing, status=404)
    before = assess_modules(
        acquire(plan_acquisition(subject, server_key="default", resources=RESOURCES)),
        subject=subject,
        serial_context=module_serial_context(subject),
    )

    recording = capture(subject)
    replay, replay_api = recording_server(recording)
    configure_librenms(
        {"default": {"librenms_url": replay.url, "api_token": replay_api.headers["X-Auth-Token"], "verify_ssl": False}}
    )

    assert unwrap_response(recording["responses"]["GET /api/v0/devices/5501/transceivers"]) == (404, missing)
    assert before["inventory"]
    assert (
        assess_modules(
            acquire(plan_acquisition(subject, server_key="default", resources=RESOURCES)),
            subject=subject,
            serial_context=module_serial_context(subject),
        )
        == before
    )


@pytest.mark.django_db
def test_capture_uses_the_owners_the_request_authorized_before_a_chassis_change(client, live_librenms, monkeypatch):
    from dcim.models import Device
    from django.urls import reverse

    from netbox_librenms_plugin.data_shapes import capture as capture_module
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    chassis, (owner, subject, hidden) = make_virtual_chassis_members("capture-planned-owner", count=3)
    seed_mapping(owner, "default", own=5501)
    serve(live_librenms.server, 5501)
    user = make_user_with_perms(
        "capture-planned-owner", [("view", Device)], constraints={"pk__in": [owner.pk, subject.pk]}
    )
    client.force_login(user)
    real_acquire = capture_module.acquire

    def move_identity_then_acquire(plan, **options):
        # The master is the capture subject and the owner of every resource.
        assert {resource: planned.pk for resource, planned in plan.owners} == dict.fromkeys(
            (Resource.INTERFACES, Resource.IP_ADDRESSES, Resource.CABLES, Resource.MODULES), owner.pk
        )
        # A concurrent assignment changes future owner selection after this request's permission checks.
        seed_mapping(hidden, "default", own=5503)
        # Move the authorized owner out of the chassis. Its identity remains a usable source.
        Device.objects.filter(pk=owner.pk).update(virtual_chassis=None, vc_position=None)
        return real_acquire(plan, **options)

    monkeypatch.setattr(capture_module, "acquire", move_identity_then_acquire)
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:capture_data_shape", kwargs={"device_id": owner.pk}),
        {"server_key": "default"},
    )
    assert response.status_code == 200
    assert response.context["recording_json"]
    assert all("5503" not in request["path"] for request in live_librenms.server.requests)
