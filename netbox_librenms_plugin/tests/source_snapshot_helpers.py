"""Seed source snapshots through the real snapshot types and the web transport."""

from django.utils import timezone

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.coordinator import resource_owner
from netbox_librenms_plugin.acquisition.resources import SCOPES
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


class Failed:
    """A part whose request failed, for a seed that needs an incomplete source."""

    def __init__(self, reason="LibreNMS answered with HTTP 500."):
        self.reason = reason


class NotApplicable:
    """A part acquisition had no reason to request."""


def _record(resource, name, value):
    request = SourceRequest(f"seeded/{resource}/{name}")
    if isinstance(value, Failed):
        return ResponseRecord(request, Outcome.FAILED, http_status=500, reason=value.reason)
    if isinstance(value, NotApplicable):
        return ResponseRecord(request, Outcome.NOT_APPLICABLE)
    return ResponseRecord(request, Outcome.OBTAINED, http_status=200, value=value, received_at=timezone.now())


def build_source_snapshot(subject, resource, server_key="default", **parts):
    """
    Return a snapshot of *resource* whose parts hold *parts* (name -> value, Failed or NotApplicable).

    Every part a scope of the resource reads must be given, so a seed cannot be silently incomplete.
    """
    missing = {part for scope_parts in SCOPES[resource].values() for part in scope_parts} - set(parts)
    if missing:
        raise TypeError(f"build_source_snapshot() needs the parts {sorted(missing)} of {resource}.")
    owner = resource_owner(subject, resource, server_key)
    records = {name: _record(resource, name, value) for name, value in parts.items()}
    now = timezone.now()
    return SourceSnapshot(
        snapshot_id=f"seeded-{resource}-{owner.pk}",
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
                    librenms_id=read_mapping(owner).own_id(server_key),
                    state=MappingState.RESOLVED,
                    parts=tuple((name, record.request.key) for name, record in records.items()),
                ),
            )
        },
        responses={record.request.key: record for record in records.values()},
    )


def seed_source_snapshot(subject, resource, server_key="default", **parts):
    """Store a snapshot built by :func:`build_source_snapshot` and return it; a seed that is not stored fails."""
    snapshot = build_source_snapshot(subject, resource, server_key, **parts)
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
    from netbox_librenms_plugin.acquisition.registry import Resource
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
