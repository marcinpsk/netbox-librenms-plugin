"""Seed source snapshots through the real snapshot types and the web transport."""

from uuid import uuid4

from django.utils import timezone

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.coordinator import oob_link, oob_source, resource_owner
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import ACQUIRERS, OOB_ACQUIRERS
from netbox_librenms_plugin.acquisition.snapshot import (
    MappingSource,
    MappingState,
    ObjectRef,
    Outcome,
    ResourceSource,
    ResponseRecord,
    SourceSnapshot,
)
from netbox_librenms_plugin.server_mappings import MappingRole, read_mapping


class Failed:
    """A part whose request failed, for a seed that needs an incomplete source."""

    def __init__(self, reason="LibreNMS answered with HTTP 500."):
        self.reason = reason


class NotApplicable:
    """A part acquisition had no reason to request."""


def _record(request, value):
    if isinstance(value, Failed):
        return ResponseRecord(request, Outcome.FAILED, http_status=500, reason=value.reason)
    if isinstance(value, NotApplicable):
        return ResponseRecord(request, Outcome.NOT_APPLICABLE)
    return ResponseRecord(request, Outcome.OBTAINED, http_status=200, value=value, received_at=timezone.now())


class _SeededFetch:
    """Answer each request of a real acquirer with the seeded value of its part, and keep each record."""

    def __init__(self, values=None):
        # Request key -> seeded value; without values, every request answers an empty list.
        self._values = values
        self.responses = {}
        self.neighbour_deadline = None

    def __call__(self, endpoint):
        return self._keep(_record(endpoint.request, [] if self._values is None else self._values[endpoint.request.key]))

    def skip(self, endpoint, outcome, reason):
        return self._keep(ResponseRecord(endpoint.request, outcome, reason=reason))

    def _keep(self, record):
        return self.responses.setdefault(record.request.key, record)


class _CableSeededFetch(_SeededFetch):
    """Decode each raw seeded envelope through the real cable endpoint contract."""

    def __call__(self, endpoint):
        value = self._values[endpoint.request.key]
        if isinstance(value, Failed):
            status = 500
            body = {"status": "error", "message": value.reason}
        else:
            status = 200
            field = endpoint.request.path.rsplit("/", 1)[-1]
            body = {"status": "ok", field: value}
        decoded = endpoint.decode(status, body)
        return self._keep(
            ResponseRecord(
                request=endpoint.request,
                outcome=decoded.outcome,
                http_status=status,
                body=body if decoded.body is None else decoded.body,
                value=decoded.value,
                reason=decoded.reason,
                received_at=timezone.now(),
            )
        )


def _seeded_fetch(acquirer, librenms_id, parts, what):
    """Return a fetch that answers the requests of a real acquirer with *parts*, which must name each of its parts."""
    requests = {name: record.request.key for name, record in acquirer(_SeededFetch(), librenms_id).items()}
    if set(parts) != set(requests):
        raise TypeError(f"A seed of {what} needs exactly the parts {sorted(requests)}, not {sorted(parts)}.")
    return _SeededFetch({requests[name]: value for name, value in parts.items()})


def _oob_mapping(resource, mapping, server_key, oob):
    """Acquire the OOB mapping that the owner's current link gives, as the coordinator does; return it and its fetch."""
    link = oob_link(mapping, server_key) if resource in OOB_ACQUIRERS else None
    if link is None or link.state is MappingState.INVALID:
        # The coordinator records the parts of a corrupt id as failed by itself.
        if oob is not None:
            raise TypeError("The owner links no usable OOB controller, so the seed takes no OOB parts.")
        fetch = _SeededFetch({})
    elif oob is None:
        raise TypeError("The owner links an OOB controller, so the seed needs its parts.")
    else:
        fetch = _seeded_fetch(OOB_ACQUIRERS[resource], link.librenms_id, oob, f"the {resource} OOB controller")
    return oob_source(fetch, resource, mapping, server_key), fetch


def build_source_snapshot(subject, resource, server_key="default", *, oob=None, **parts):
    """
    Return a snapshot of *resource* whose parts hold *parts* (name -> value, Failed or NotApplicable).

    The resource's real acquirer and the coordinator's OOB rule decide the parts and their records, so a
    seed holds what an acquisition of these answers holds. The snapshot follows the owner's current mapping.
    *oob* gives the OOB controller's parts when the owner links one with a valid id.
    """
    owner = resource_owner(subject, resource, server_key)
    mapping = read_mapping(owner)
    librenms_id = mapping.own_id(server_key)
    fetch = _seeded_fetch(ACQUIRERS[resource], librenms_id, parts, resource)
    host_parts = ACQUIRERS[resource](fetch, librenms_id)
    oob_mapping, oob_fetch = _oob_mapping(resource, mapping, server_key, oob)
    now = timezone.now()
    return SourceSnapshot(
        snapshot_id=uuid4().hex,
        server_key=server_key,
        subject=ObjectRef.of(subject),
        started_at=now,
        finished_at=now,
        resources={
            resource: ResourceSource(
                resource=resource,
                owner=ObjectRef.of(owner),
                host=MappingSource(
                    role=MappingRole.OWN,
                    librenms_id=librenms_id,
                    state=MappingState.RESOLVED,
                    parts=tuple((name, record.request.key) for name, record in host_parts.items()),
                ),
                oob=oob_mapping,
            )
        },
        responses={**fetch.responses, **oob_fetch.responses},
    )


def seed_source_snapshot(subject, resource, server_key="default", *, oob=None, **parts):
    """Store a snapshot built by :func:`build_source_snapshot` and return it; a seed that is not stored fails."""
    snapshot = build_source_snapshot(subject, resource, server_key, oob=oob, **parts)
    assert source_cache.store(subject, resource, snapshot), f"the {resource} seed has no usable scope"
    return snapshot


def stored_source_value(subject, resource, part, server_key="default"):
    """Return the value of one part of the stored snapshot, or None when no snapshot is stored."""
    snapshot = source_cache.read(subject, resource, server_key)
    return snapshot.value(resource, part) if snapshot is not None else None


def ip_address_row(address, port_id, prefix_length=24):
    """Return one LibreNMS ``/devices/{id}/ip`` row as the endpoint serves it."""
    return {"ip_address": address, "prefix_length": prefix_length, "port_id": port_id}


def seed_ip_snapshot(subject, addresses, ports=(), *, device=None, vrfs=None, server_key="default"):
    """
    Store an IP address snapshot of *addresses* and *ports*, with *device* as the device record.

    A port that names a VRF needs *vrfs*, as acquisition reads them only then; without such a port
    the VRF part is not applicable.
    """
    from netbox_librenms_plugin.acquisition.resources import port_vrf_id

    ports = list(ports)
    if vrfs is None:
        if any(port_vrf_id(port) is not None for port in ports):
            raise TypeError("seed_ip_snapshot() needs vrfs when a port names a VRF.")
        vrfs = NotApplicable()
    return seed_source_snapshot(
        subject,
        Resource.IP_ADDRESSES,
        server_key,
        addresses=list(addresses),
        ports=ports,
        device={} if device is None else device,
        vrfs=vrfs,
    )


def port_pair(high_port_id, low_port_id):
    """Return one LibreNMS ``/devices/{id}/port_stack`` mapping as the endpoint serves it."""
    return {"high_port_id": high_port_id, "low_port_id": low_port_id}


def port_vlans(mode=None, untagged=None, tagged=()):
    """
    Return the raw LibreNMS port fields from which the assessment parses these VLANs.

    Args:
        mode (str | None): ``"access"``, ``"tagged"`` or None, as the parsed row shows it.
        untagged (int | None): The untagged VID.
        tagged (Iterable[int]): The tagged VIDs.

    Returns:
        dict: The ``ifVlan``, ``ifTrunk`` and ``vlans`` fields of a port row.

    """
    if mode not in (None, "access", "tagged"):
        raise ValueError(f"No LibreNMS port fields give the mode {mode!r}.")
    if mode is not None and untagged is None and not tagged:
        # LibreNMS sets ifVlan for a port with a mode, and the parser reads it as the untagged VID.
        raise ValueError(f"No LibreNMS port fields give the mode {mode!r} without a VLAN.")
    vlans = ([{"vlan": untagged, "untagged": 1}] if untagged is not None else []) + [
        {"vlan": vid, "untagged": 0} for vid in tagged
    ]
    # LibreNMS sets ifVlan for an access or a trunk port; only ifTrunk tells them apart.
    if_vlan = "" if mode is None else str(untagged if untagged is not None else 1)
    return {"ifVlan": if_vlan, "ifTrunk": "dot1Q" if mode == "tagged" else None, "vlans": vlans}


# The OS of a seeded device record: no stored LAG or bridge name rule names it.
SEEDED_OS = "seeded-os"


def seed_interfaces_snapshot(subject, ports, port_stack=(), *, device=None, oob_ports=None, server_key="default"):
    """
    Store an interfaces snapshot of the raw *ports* and *port_stack* mappings, as a refresh acquires them.

    Args:
        subject (Device | VirtualMachine): The page object; the snapshot belongs to its VC sync owner.
        ports (Iterable[dict]): The host's LibreNMS port rows.
        port_stack (Iterable[dict]): The host's port_stack mappings, see :func:`port_pair`.
        device (dict | Failed | None): The device record; None names :data:`SEEDED_OS`.
        oob_ports (Iterable[dict] | Failed | None): The OOB controller's port rows, when the owner links one.
        server_key (str): The LibreNMS server key.

    Returns:
        SourceSnapshot: The stored snapshot.

    """
    return seed_source_snapshot(
        subject,
        Resource.INTERFACES,
        server_key,
        oob=None if oob_ports is None else {"ports": oob_ports if isinstance(oob_ports, Failed) else list(oob_ports)},
        ports=list(ports),
        port_stack=list(port_stack),
        device={"os": SEEDED_OS} if device is None else device,
    )


def interface_rows(subject, interface_name_field="ifName", server_key="default"):
    """Return the stored interfaces snapshot of *subject*, assessed as every interface reader assesses it."""
    from netbox_librenms_plugin.views.base.interfaces_view import read_interface_assessment

    return read_interface_assessment(subject, server_key, interface_name_field)


def port_row(
    port_id,
    name,
    *,
    device_id,
    if_descr=None,
    if_type="ethernetCsmacd",
    if_speed=1_000_000_000,
    deleted=0,
    **fields,
):
    """Return one raw LibreNMS port with complete interface rule inputs."""
    return {
        "port_id": port_id,
        "device_id": device_id,
        "ifName": name,
        "ifDescr": name if if_descr is None else if_descr,
        "ifType": if_type,
        "ifSpeed": if_speed,
        "deleted": deleted,
        **fields,
    }


def link_row(
    local_port_id,
    remote_port,
    *,
    remote_device_id=None,
    remote_hostname="",
    remote_port_id=None,
    protocol="lldp",
    id=None,  # noqa: A002
    local_port=None,
):
    """Return one raw LibreNMS link, with no assessed row fields."""
    return {
        "id": id,
        "local_port_id": local_port_id,
        "local_port": local_port,
        "remote_port": remote_port,
        "remote_device_id": remote_device_id,
        "remote_hostname": remote_hostname,
        "remote_port_id": remote_port_id,
        "protocol": protocol,
    }


def sensor_row(
    sensor_id,
    index,
    *,
    device_id,
    sensor_type="acsSerialPortTable",
    sensor_descr="",
    sensor_deleted=0,
):
    """Return one raw LibreNMS sensor with an explicit index and device identity."""
    return {
        "sensor_id": sensor_id,
        "sensor_index": f"acsSerialPortTableStatus.{index}",
        "device_id": device_id,
        "sensor_type": sensor_type,
        "sensor_descr": sensor_descr,
        "sensor_deleted": sensor_deleted,
    }


def cable_rows(request, subject, server_key="default", interface_name_field="ifName"):
    """Read assessed cable rows with the same actor as the view or mutation."""
    from netbox_librenms_plugin.views.base.cables_view import read_cable_assessment

    return read_cable_assessment(request, subject, server_key, interface_name_field)


def seed_cables_snapshot(
    subject,
    links=(),
    ports=(),
    neighbour_ports=None,
    *,
    oob_links=None,
    oob_ports=None,
    sensors=None,
    server_key="default",
):
    """Store raw cable endpoint facts through the coordinator's real resource acquirer."""
    from netbox_librenms_plugin.acquisition import responses
    from netbox_librenms_plugin.acquisition.coordinator import _resource_source

    owner = resource_owner(subject, Resource.CABLES, server_key)
    mapping = read_mapping(owner)
    host_id = mapping.own_id(server_key)
    values = {
        responses.links(host_id).request.key: links if isinstance(links, Failed) else list(links),
        responses.ports(host_id, with_vlans=False).request.key: ports if isinstance(ports, Failed) else list(ports),
    }
    if sensors is not None:
        values[responses.sensors(host_id).request.key] = sensors if isinstance(sensors, Failed) else list(sensors)
    for device_id, rows in (neighbour_ports or {}).items():
        values[responses.ports(device_id, with_vlans=False).request.key] = (
            rows if isinstance(rows, Failed) else list(rows)
        )
    oob_id = mapping.oob_id(server_key)
    if oob_id is not None:
        if oob_links is None or oob_ports is None:
            raise TypeError("A linked OOB controller needs links and ports in the cable seed.")
        values[responses.links(oob_id).request.key] = oob_links if isinstance(oob_links, Failed) else list(oob_links)
        values[responses.ports(oob_id, with_vlans=False).request.key] = (
            oob_ports if isinstance(oob_ports, Failed) else list(oob_ports)
        )
    fetch = _CableSeededFetch(values)
    source = _resource_source(fetch, Resource.CABLES, owner, server_key, include_serial=sensors is not None)
    now = timezone.now()
    snapshot = SourceSnapshot(
        snapshot_id=uuid4().hex,
        server_key=server_key,
        subject=ObjectRef.of(subject),
        started_at=now,
        finished_at=now,
        resources={Resource.CABLES: source},
        responses=fetch.responses,
    )
    assert source_cache.store(subject, Resource.CABLES, snapshot), "the cables seed has no usable scope"
    return snapshot


def seed_modules_snapshot(subject, inventory, ports=(), transceivers=(), *, oob_inventory=None, server_key="default"):
    """Store raw module source facts through the real acquirer and source transport."""
    for rows in (inventory, () if oob_inventory is None or isinstance(oob_inventory, Failed) else oob_inventory):
        if any(key.startswith("_") for row in rows for key in row):
            raise TypeError("Module seeds require raw endpoint facts.")
    return seed_source_snapshot(
        subject,
        Resource.MODULES,
        server_key,
        inventory=list(inventory),
        ports=list(ports),
        transceivers=list(transceivers),
        oob=None
        if oob_inventory is None
        else {"inventory": oob_inventory if isinstance(oob_inventory, Failed) else list(oob_inventory)},
    )


def module_inventory(subject, server_key="default"):
    """Read the current module assessment through the bound source transport."""
    from netbox_librenms_plugin.views.base.modules_view import read_module_assessment

    assessed = read_module_assessment(subject, server_key)
    return assessed["inventory"] if assessed is not None else None


def acquire_recording(api, recording, resources, *, include_serial=False, inventory_probes=False):
    """Replay real HTTP responses through acquisition for a mapped NetBox subject."""
    from copy import deepcopy
    from django.conf import settings
    from django.test import override_settings
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    subject = seed_mapping(
        make_device(f"replay-{uuid4().hex}"),
        api.server_key,
        own=recording["device_id"],
        oob=(recording.get("meta") or {}).get("oob_id"),
    )
    config = deepcopy(settings.PLUGINS_CONFIG)
    config["netbox_librenms_plugin"]["servers"] = {
        api.server_key: {
            "librenms_url": api.librenms_url,
            "api_token": api.headers["X-Auth-Token"],
            "verify_ssl": False,
        }
    }
    with override_settings(PLUGINS_CONFIG=config):
        snapshot = acquire(
            subject,
            server_key=api.server_key,
            resources=resources,
            include_serial=include_serial,
            inventory_probes=inventory_probes,
        )
    return snapshot, subject


def recording_interface_assessment(api, recording):
    """Acquire and assess a recording with its stored OS name patterns."""
    from netbox_librenms_plugin.assessment.interfaces import assess_interfaces
    from netbox_librenms_plugin.models import PortStackLagPattern

    snapshot, _subject = acquire_recording(api, recording, [Resource.INTERFACES])
    device = snapshot.record(Resource.INTERFACES, "device")
    os_name = device.value.get("os") if device.obtained else None
    lag_patterns = recording.get("lag_patterns") or {}
    sap_patterns = recording.get("sap_patterns") or {}
    names = set(lag_patterns) | set(sap_patterns)
    if isinstance(os_name, str) and os_name.strip():
        names.add(os_name)
    for rule_os in names:
        PortStackLagPattern.objects.update_or_create(
            librenms_os=rule_os,
            defaults={
                "lag_name_pattern": lag_patterns.get(rule_os) or "^$",
                "sap_name_pattern": sap_patterns.get(rule_os, ""),
                "bridge_name_pattern": "",
            },
        )
    return assess_interfaces(snapshot, "ifName")


def recording_serial_projection(api, recording, sensor_types=None):
    """Acquire raw sensors and apply the recording's serial recognition map."""
    from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns, project_serial_sensors

    snapshot, _subject = acquire_recording(api, recording, [Resource.CABLES], include_serial=True)
    record = snapshot.record(Resource.CABLES, "sensors")
    if not record.obtained:
        return False, record.reason
    projection = project_serial_sensors(
        record.value, get_serial_sensor_type_patterns() if sensor_types is None else sensor_types
    )
    return (True, projection.rows) if projection.ready else (False, projection.reason)


def recording_part(api, device_id, resource, part):
    """Acquire a recording part and return its obtained value or failure reason."""
    snapshot, _subject = acquire_recording(
        api, {"device_id": device_id}, [resource], include_serial=resource is Resource.CABLES
    )
    record = snapshot.record(resource, part)
    return (True, record.value) if record.obtained else (False, record.reason)


def acquire_ports_record(api, device_id):
    """Acquire a port response through the coordinator over the configured HTTP server."""
    snapshot, _subject = acquire_recording(api, {"device_id": device_id}, [Resource.INTERFACES])
    return snapshot.record(Resource.INTERFACES, "ports")


def inventory_row(index, name, *, model="", serial="", physical_class="module", parent=0, **facts):
    """Build one raw ENTITY-MIB inventory row."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalName": name,
        "entPhysicalModelName": model,
        "entPhysicalSerialNum": serial,
        "entPhysicalClass": physical_class,
        "entPhysicalContainedIn": parent,
        **facts,
    }


def transceiver_row(index, *, port_id=None, model="", serial="", kind=""):
    """Build one raw transceiver response row."""
    return {"entity_physical_index": index, "port_id": port_id, "model": model, "serial": serial, "type": kind}


def seed_mapped_modules(
    subject,
    inventory,
    ports=(),
    transceivers=(),
    *,
    librenms_id=1,
    server_key="default",
    oob_inventory=None,
    oob_id=None,
):
    """Map the subject and seed explicit module endpoint facts."""
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    seed_mapping(subject, server_key, own=librenms_id)
    if oob_id is not None:
        seed_mapping(subject, server_key, oob=oob_id)
    subject.__dict__.pop("cf", None)
    return seed_modules_snapshot(
        subject, inventory, ports, transceivers, server_key=server_key, oob_inventory=oob_inventory
    )
