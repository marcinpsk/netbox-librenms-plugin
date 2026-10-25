"""Export source acquisition responses as a replayable data-shape recording."""

from netbox_librenms_plugin.acquisition.coordinator import INVALID_OOB_REASON, SubjectNotMapped, acquire
from netbox_librenms_plugin.acquisition import responses
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import NEIGHBOUR_PORTS_PREFIX, device_os
from netbox_librenms_plugin.acquisition.snapshot import MappingState, Outcome
from netbox_librenms_plugin.data_shapes.envelope import MAX_HTTP_STATUS, MIN_HTTP_STATUS, wrap_response
from netbox_librenms_plugin.data_shapes.recordings_store import SERIAL_SENSORS_META_KEY, SERIAL_SENSORS_UNAVAILABLE
from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns, project_serial_sensors

SCHEMA_VERSION = 1
CAPTURE_RESOURCES = (Resource.INTERFACES, Resource.IP_ADDRESSES, Resource.CABLES, Resource.MODULES)


def _require(record, *, allow_missing=False, optional_status=False):
    """Require one definitive recording source, with an optional unsupported endpoint answer."""
    if record.obtained:
        if 200 <= record.http_status < 300 and not optional_status and record.body.get("status") != "ok":
            raise RuntimeError(f"Capture failed for {record.request.path!r}: response was not a success object")
        return
    if allow_missing and record.http_status == responses.HTTP_NOT_FOUND:
        return
    detail = "no HTTP response" if record.http_status is None else f"HTTP {record.http_status}: {record.reason}"
    raise RuntimeError(f"Capture failed for {record.request.path!r}: {detail}")


def _recording_records(snapshot):
    """Select recording sources by their resource roles, including a route shared with a neighbour."""
    for resource in CAPTURE_RESOURCES:
        source = snapshot.resources[resource]
        for mapping in (source.host, source.oob):
            if mapping is None:
                continue
            if mapping.state is MappingState.INVALID:
                raise RuntimeError(INVALID_OOB_REASON)
            for part, key in mapping.parts:
                if resource is Resource.CABLES and (part == "sensors" or part.startswith(NEIGHBOUR_PORTS_PREFIX)):
                    continue
                yield snapshot.responses[key]


def _requirements(snapshot):
    """Require the mapped source parts that a structural recording needs."""
    host_id = snapshot.resources[Resource.INTERFACES].host.librenms_id
    stack_request = responses.port_stack(host_id).request
    transceiver_request = responses.transceivers(host_id).request
    for record in _recording_records(snapshot):
        if record.outcome is not Outcome.NOT_APPLICABLE:
            _require(
                record,
                allow_missing=record.request == transceiver_request,
                optional_status=record.request == stack_request,
            )


def _export_responses(snapshot):
    """Export real HTTP answers and keep the VLAN-bearing ports answer for each path."""
    exported = {}
    host_id = snapshot.resources[Resource.INTERFACES].host.librenms_id
    sensors_request = responses.sensors(host_id).request
    stack_request = responses.port_stack(host_id).request
    for record in _recording_records(snapshot):
        if record.request == sensors_request:
            continue
        status = record.http_status
        if status is None or not MIN_HTTP_STATUS <= status <= MAX_HTTP_STATUS:
            continue
        if 200 <= status < 300 and record.body is None:
            raise RuntimeError(f"Capture failed for {record.request.path!r}: HTTP {status} did not contain JSON")
        if record.obtained:
            _require(record, optional_status=record.request == stack_request)
        key = record.request.route_key
        if key not in exported or dict(record.request.params).get("with") == "vlans":
            exported[key] = wrap_response(status, record.body)
    # A filtered query that answered [] uses obtained /all rows in production too.
    for part in ("inventory_root", "inventory_children"):
        record = snapshot.record(Resource.MODULES, part)
        if not record.obtained:
            continue
        rows = responses.effective_inventory(
            record,
            snapshot.record(Resource.MODULES, "inventory"),
            params=record.request.params,
            http_status=record.http_status,
        )
        if not record.value and rows:
            exported[record.request.route_key] = {"status": "ok", "inventory": rows}
    return exported


def capture_device_recording(subject, *, server_key, name=None, description="", meta=None, include_serial=True):
    """
    Acquire the subject's structural sources and export a data-shape recording.

    Args:
        subject (Device): The mapped synchronization subject.
        server_key (str): The configured LibreNMS server.
        name (str | None): The recording name, or None for a name derived from the host id.
        description (str): The recording description.
        meta (dict | None): Extra recording metadata.
        include_serial (bool): Whether to acquire serial sensors.

    Returns:
        dict: The recording, with HTTP status envelopes and source metadata.

    Raises:
        RuntimeError: A required recording source was not obtained.

    """
    try:
        snapshot = acquire(
            subject,
            server_key=server_key,
            resources=CAPTURE_RESOURCES,
            include_serial=include_serial,
            inventory_probes=True,
        )
    except SubjectNotMapped as exc:
        raise RuntimeError(str(exc)) from exc
    _requirements(snapshot)
    responses = _export_responses(snapshot)
    source = snapshot.resources[Resource.INTERFACES]
    device_id = source.host.librenms_id
    device = snapshot.value(Resource.INTERFACES, "device")
    meta_out = {key: value for key, value in (meta or {}).items() if key not in ("oob_id", SERIAL_SENSORS_META_KEY)}
    meta_out["os"] = device.get("os")
    if source.oob is not None and source.oob.librenms_id != device_id:
        meta_out["oob_id"] = source.oob.librenms_id
    sensor_record = snapshot.record(Resource.CABLES, "sensors")
    projection = (
        project_serial_sensors(sensor_record.value, get_serial_sensor_type_patterns())
        if sensor_record.obtained
        else None
    )
    if projection is None or not projection.ready:
        meta_out[SERIAL_SENSORS_META_KEY] = SERIAL_SENSORS_UNAVAILABLE
    elif projection.rows:
        responses[sensor_record.request.route_key] = {"status": "ok", "sensors": projection.rows}
    patterns = PortStackLagPattern.patterns_for_os_queryset(device_os(device))
    pattern_rows = list(patterns) if patterns is not None else []
    sap_rows = PortStackLagPattern.sap_patterns_for_os_queryset(device_os(device))
    recording = {
        "schema_version": SCHEMA_VERSION,
        "name": name or f"device-{device_id}",
        "description": description,
        "meta": meta_out,
        "device_id": device_id,
        "lag_patterns": {row.librenms_os: row.lag_name_pattern for row in pattern_rows if row.lag_name_pattern},
        "sap_patterns": {row.librenms_os: row.sap_name_pattern for row in sap_rows if row.sap_name_pattern},
        "responses": responses,
    }
    if projection is not None and projection.ready and projection.rows:
        recording["serial_type_patterns"] = projection.type_map
    return recording
