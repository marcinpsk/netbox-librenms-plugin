"""What each resource requests from LibreNMS, and which of its responses each scope needs."""

from netbox_librenms_plugin.acquisition import responses
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.snapshot import Outcome
from netbox_librenms_plugin.data_shapes.ports import port_has_vrf


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
    elif any(port_has_vrf(port) for port in ports.value):
        parts["vrfs"] = fetch(vrfs)
    else:
        parts["vrfs"] = fetch.skip(vrfs, Outcome.NOT_APPLICABLE, "No port names a VRF.")
    return parts


ACQUIRERS = {
    Resource.IP_ADDRESSES: _ip_addresses,
    Resource.VLANS: _vlans,
}

# Scope -> the parts it reads. A part that is not applicable does not block its scope.
SCOPES = {
    Resource.IP_ADDRESSES: {"addresses": ("addresses", "ports", "device", "vrfs")},
    Resource.VLANS: {"vlans": ("vlans",)},
}


def blocked_scopes(snapshot, resource):
    """Return each blocked scope of a resource with the records that block it."""
    blocked = {}
    for scope, parts in SCOPES[resource].items():
        records = tuple(
            record
            for record in (snapshot.record(resource, part) for part in parts)
            if record.outcome not in (Outcome.OBTAINED, Outcome.NOT_APPLICABLE)
        )
        if records:
            blocked[scope] = records
    return blocked


def is_ready(snapshot, resource):
    """Return whether at least one scope of the resource can be used."""
    return len(blocked_scopes(snapshot, resource)) < len(SCOPES[resource])
