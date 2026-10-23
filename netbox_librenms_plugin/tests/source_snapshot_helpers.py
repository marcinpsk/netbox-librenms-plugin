"""Seed source snapshots through the real snapshot types and the web transport."""

from uuid import uuid4

from django.utils import timezone

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.coordinator import INVALID_OOB_REASON, oob_link, resource_owner
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.snapshot import (
    MappingSource,
    MappingState,
    ObjectRef,
    Outcome,
    ResourceSource,
    ResponseRecord,
    SourceRequest,
    SourceSnapshot,
)
from netbox_librenms_plugin.server_mappings import MappingRole, read_mapping

# The parts that acquisition records for each resource, so a seed cannot be silently incomplete.
_PARTS = {
    Resource.INTERFACES: {"ports", "port_stack", "device"},
    Resource.IP_ADDRESSES: {"addresses", "ports", "device", "vrfs"},
    Resource.VLANS: {"vlans"},
}
_OOB_PARTS = {Resource.INTERFACES: {"ports"}}


class Failed:
    """A part whose request failed, for a seed that needs an incomplete source."""

    def __init__(self, reason="LibreNMS answered with HTTP 500."):
        self.reason = reason


class NotApplicable:
    """A part acquisition had no reason to request."""


def _record(path, value):
    request = SourceRequest(path)
    if isinstance(value, Failed):
        return ResponseRecord(request, Outcome.FAILED, http_status=500, reason=value.reason)
    if isinstance(value, NotApplicable):
        return ResponseRecord(request, Outcome.NOT_APPLICABLE)
    return ResponseRecord(request, Outcome.OBTAINED, http_status=200, value=value, received_at=timezone.now())


def _require(parts, expected, what):
    if set(parts) != expected:
        raise TypeError(f"A seed of {what} needs exactly the parts {sorted(expected)}, not {sorted(parts)}.")


def _oob_mapping(resource, mapping, server_key, oob, records):
    """Return the OOB mapping that the owner's current link gives, with its records added to *records*."""
    link = oob_link(mapping, server_key) if resource in _OOB_PARTS else None
    if link is None:
        if oob is not None:
            raise TypeError("The owner links no OOB controller, so the seed takes no OOB parts.")
        return None
    state, oob_id = link
    if state is MappingState.INVALID:
        # Acquisition makes no request for a corrupt id, and records each part as failed.
        oob = dict.fromkeys(_OOB_PARTS[resource], Failed(INVALID_OOB_REASON))
    elif oob is None:
        raise TypeError("The owner links an OOB controller, so the seed needs its parts.")
    _require(oob, _OOB_PARTS[resource], f"the {resource} OOB controller")
    oob_records = {name: _record(f"seeded/{resource}/oob/{name}", value) for name, value in oob.items()}
    records.update({record.request.key: record for record in oob_records.values()})
    return MappingSource(
        role=MappingRole.OOB,
        librenms_id=oob_id,
        state=state,
        parts=tuple((name, record.request.key) for name, record in oob_records.items()),
    )


def build_source_snapshot(subject, resource, server_key="default", *, oob=None, **parts):
    """
    Return a snapshot of *resource* whose parts hold *parts* (name -> value, Failed or NotApplicable).

    The snapshot follows the owner's current mapping. *oob* gives the OOB controller's parts when the
    owner links one; a link with a corrupt id gets failed parts, as acquisition records them.
    """
    _require(parts, _PARTS[resource], resource)
    owner = resource_owner(subject, resource, server_key)
    mapping = read_mapping(owner)
    host_records = {name: _record(f"seeded/{resource}/{name}", value) for name, value in parts.items()}
    records = {record.request.key: record for record in host_records.values()}
    oob_mapping = _oob_mapping(resource, mapping, server_key, oob, records)
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
                    librenms_id=mapping.own_id(server_key),
                    state=MappingState.RESOLVED,
                    parts=tuple((name, record.request.key) for name, record in host_records.items()),
                ),
                oob=oob_mapping,
            )
        },
        responses=records,
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
