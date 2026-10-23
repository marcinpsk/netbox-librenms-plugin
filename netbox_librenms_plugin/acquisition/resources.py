"""What each resource requests from LibreNMS, and which of its responses each scope needs."""

from dataclasses import dataclass
from time import monotonic

from netbox_librenms_plugin.acquisition import responses
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.snapshot import MappingState, Outcome
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns, project_serial_sensors
from netbox_librenms_plugin.server_mappings import MappingRole

REMOTE_ALIAS_FETCH_BUDGET_SECONDS = 15.0
NEIGHBOUR_PORTS_PREFIX = "neighbour_ports:"


@dataclass(frozen=True)
class Blocker:
    """One source that a scope cannot use, and the notice that names it."""

    path: str
    message: str

    @classmethod
    def of(cls, record):
        """Return the blocker of a response that was not obtained."""
        return cls(record.request.path, f"{record.request.path} was not obtained ({record.reason})")


def port_vrf_id(port):
    """Return the LibreNMS VRF id a port row names, or None; LibreNMS reports 0 for no VRF."""
    return coerce_librenms_id(port.get("ifVrf"))


def device_os(device_record):
    """Return the LibreNMS OS that a device record names, stripped, or "" when it names none."""
    os_name = device_record.get("os")
    return os_name.strip() if isinstance(os_name, str) else ""


def _vlans(fetch, librenms_id):
    return {"vlans": fetch(responses.vlans(librenms_id))}


def _ip_addresses(fetch, librenms_id):
    parts = {
        "addresses": fetch(responses.ip_addresses(librenms_id)),
        "ports": fetch(responses.ports(librenms_id, with_vlans=False)),
        "device": fetch(responses.device(librenms_id)),
    }
    vrfs = responses.vrfs(librenms_id)
    ports = parts["ports"]
    if not ports.obtained:
        parts["vrfs"] = fetch.skip(vrfs, Outcome.NOT_REQUESTED, "The ports were not obtained.")
    elif any(port_vrf_id(port) is not None for port in ports.value):
        parts["vrfs"] = fetch(vrfs)
    else:
        parts["vrfs"] = fetch.skip(vrfs, Outcome.NOT_APPLICABLE, "No port names a VRF.")
    return parts


def _interfaces(fetch, librenms_id):
    return {
        "ports": fetch(responses.ports(librenms_id, with_vlans=True)),
        "port_stack": fetch(responses.port_stack(librenms_id)),
        "device": fetch(responses.device(librenms_id)),
    }


def _oob_interfaces(fetch, librenms_id):
    return {"ports": fetch(responses.ports(librenms_id, with_vlans=True))}


def _cable_parts(fetch, librenms_id):
    parts = {
        "links": fetch(responses.links(librenms_id)),
        "ports": fetch(responses.ports(librenms_id, with_vlans=False)),
    }
    links = parts["links"]
    if not links.obtained:
        return parts
    neighbours = dict.fromkeys(
        neighbour_id
        for link in links.value
        if (neighbour_id := coerce_librenms_id(link.get("remote_device_id"))) is not None
    )
    for neighbour_id in neighbours:
        endpoint = responses.ports(neighbour_id, with_vlans=False)
        if endpoint.request.key in fetch.responses:
            record = fetch.responses[endpoint.request.key]
        else:
            if fetch.neighbour_deadline is None:
                fetch.neighbour_deadline = monotonic() + REMOTE_ALIAS_FETCH_BUDGET_SECONDS
            if monotonic() >= fetch.neighbour_deadline:
                record = fetch.skip(endpoint, Outcome.FAILED, "The 15 s neighbour request budget was spent.")
            else:
                record = fetch(endpoint)
        parts[f"{NEIGHBOUR_PORTS_PREFIX}{neighbour_id}"] = record
    return parts


def _cables(fetch, librenms_id, *, include_serial=False):
    parts = _cable_parts(fetch, librenms_id)
    endpoint = responses.sensors(librenms_id)
    parts["sensors"] = (
        fetch(endpoint)
        if include_serial
        else fetch.skip(endpoint, Outcome.NOT_REQUESTED, "Serial sensors were not requested.")
    )
    return parts


ACQUIRERS = {
    Resource.INTERFACES: _interfaces,
    Resource.CABLES: _cables,
    Resource.IP_ADDRESSES: _ip_addresses,
    Resource.VLANS: _vlans,
}

# The parts acquired from the owner's out-of-band controller, for a resource that shows its rows.
OOB_ACQUIRERS = {
    Resource.INTERFACES: _oob_interfaces,
    Resource.CABLES: _cable_parts,
}


def _blockers(snapshot, resource, parts, role=MappingRole.OWN):
    """Return a blocker for each part that was not obtained; a part that is not applicable blocks nothing."""
    oob = snapshot.resources[resource].oob
    if role is MappingRole.OOB and oob is not None and oob.state is MappingState.INVALID:
        return (Blocker("oob", "the linked OOB controller has no valid LibreNMS id"),)
    return tuple(
        Blocker.of(record)
        for record in (snapshot.record(resource, part, role) for part in parts)
        if record.outcome not in (Outcome.OBTAINED, Outcome.NOT_APPLICABLE)
    )


def _ip_address_scopes(snapshot):
    return {"addresses": _blockers(snapshot, Resource.IP_ADDRESSES, ("addresses", "ports", "device", "vrfs"))}


def _vlan_scopes(snapshot):
    return {"vlans": _blockers(snapshot, Resource.VLANS, ("vlans",))}


def _interface_scopes(snapshot):
    resource = Resource.INTERFACES
    host = _blockers(snapshot, resource, ("ports", "port_stack"))
    port_stack = snapshot.record(resource, "port_stack")
    if port_stack.obtained and port_stack.value:
        # The OS scopes the name rules that classify the pairs.
        device = snapshot.record(resource, "device")
        if not device.obtained:
            host += (Blocker.of(device),)
        elif not device_os(device.value):
            path = device.request.path
            host += (Blocker(path, f"{path} names no device OS, which the port_stack pairs need"),)
    scopes = {"host": host}
    if snapshot.resources[resource].oob is not None:
        # The OOB rows are compared with the host rows, so they need the host scope too.
        scopes["oob"] = _blockers(snapshot, resource, ("ports",), MappingRole.OOB) or host
    return scopes


def _cable_scopes(snapshot):
    resource = Resource.CABLES
    source = snapshot.resources[resource]
    scopes = {}
    for name, mapping in (("host", source.host), ("oob", source.oob)):
        if mapping is None or snapshot.record(resource, "links", mapping.role).outcome is Outcome.NOT_APPLICABLE:
            continue
        scopes[name] = _blockers(snapshot, resource, ("links", "ports"), mapping.role)
        for part, _key in mapping.parts:
            if part.startswith(NEIGHBOUR_PORTS_PREFIX):
                neighbour_id = part.removeprefix(NEIGHBOUR_PORTS_PREFIX)
                scopes[f"{name}:neighbour:{neighbour_id}"] = scopes[name] + _blockers(
                    snapshot, resource, (part,), mapping.role
                )
    sensors = snapshot.record(resource, "sensors")
    if sensors.outcome is not Outcome.NOT_APPLICABLE:
        serial = _blockers(snapshot, resource, ("sensors",))
        if sensors.obtained:
            projection = project_serial_sensors(sensors.value, get_serial_sensor_type_patterns())
            if not projection.ready:
                path = sensors.request.path
                serial = (Blocker(path, f"{path} was not usable ({projection.reason})"),)
        scopes["serial"] = serial
    return scopes


# Resource -> a function that returns each scope with its blockers; a scope without blockers is ready.
SCOPES = {
    Resource.INTERFACES: _interface_scopes,
    Resource.CABLES: _cable_scopes,
    Resource.IP_ADDRESSES: _ip_address_scopes,
    Resource.VLANS: _vlan_scopes,
}


def blocked_scopes(snapshot, resource):
    """Return each blocked scope of a resource with the blockers that name its failed sources."""
    return {scope: blockers for scope, blockers in SCOPES[resource](snapshot).items() if blockers}


def is_ready(snapshot, resource):
    """Return whether at least one scope of the resource can be used."""
    return any(not blockers for blockers in SCOPES[resource](snapshot).values())
