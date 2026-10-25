"""The web transport of source snapshots: the shared cache the sync page reads and refreshes."""

from dataclasses import dataclass, field

from dcim.models import ConsoleServerPort, Device, Interface
from django.core.cache import cache
from django.db.models import Q

from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire, oob_link, resource_owner
from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, Resource
from netbox_librenms_plugin.acquisition.resources import (
    Blocker,
    NEIGHBOUR_PORTS_PREFIX,
    OOB_ACQUIRERS,
    blocked_scopes,
    is_ready,
)
from netbox_librenms_plugin.acquisition.snapshot import ObjectRef, SourceSnapshot
from netbox_librenms_plugin.librenms_api import configured_cache_timeout
from netbox_librenms_plugin.server_mappings import MappingRole, identity_q, read_mapping
from netbox_librenms_plugin.sync_cache import sync_snapshot_key


@dataclass(frozen=True)
class Refresh:
    """The result of one refresh: the acquired snapshot, and why nothing was stored when it was not."""

    snapshot: SourceSnapshot | None
    error: str | None = None
    # Each blocked scope of the acquired snapshot, with its blockers; a stored snapshot can have some.
    blocked: dict = field(default_factory=dict)


def _key(owner, resource, server_key):
    return sync_snapshot_key(owner, RESOURCE_SPECS[resource].data_type, server_key)


def snapshot_key(subject, resource, server_key):
    """Return the cache key of the resource's snapshot on one server."""
    return _key(resource_owner(subject, resource, server_key), resource, server_key)


def can_view_serial_ports(user, owner):
    """Return whether the actor can view a ConsoleServerPort on the source owner."""
    return bool(
        isinstance(owner, Device)
        and user.has_perm("dcim.view_consoleserverport")
        and ConsoleServerPort.objects.restrict(user, "view").filter(device=owner).exists()
    )


def visible_cable_port_sources(user, server_key, identities):
    """Return port sources whose device, chassis members and interfaces the actor can view."""
    sources = Device.objects.filter(
        identity_q(Device, server=server_key, identities=identities, roles=(MappingRole.OWN, MappingRole.OOB))
    )
    owners = list(
        Device.objects.filter(
            Q(pk__in=sources.values("pk"))
            | Q(virtual_chassis_id__in=sources.exclude(virtual_chassis_id=None).values("virtual_chassis_id"))
        )
    )
    owner_ids = {owner.pk for owner in owners}
    hidden_ids = owner_ids - set(
        Device.objects.restrict(user, "view").filter(pk__in=owner_ids).values_list("pk", flat=True)
    )
    hidden_ids.update(
        Interface.objects.filter(device_id__in=owner_ids)
        .exclude(pk__in=Interface.objects.restrict(user, "view").values("pk"))
        .values_list("device_id", flat=True)
    )
    hidden_chassis = {owner.virtual_chassis_id for owner in owners if owner.pk in hidden_ids}
    visibility = {}
    for owner in sources:
        visible = owner.pk not in hidden_ids and (
            owner.virtual_chassis_id is None or owner.virtual_chassis_id not in hidden_chassis
        )
        mapping = read_mapping(owner)
        for source_id in (mapping.own_id(server_key), mapping.oob_id(server_key)):
            if source_id in identities:
                visibility[source_id] = visibility.get(source_id, True) and visible
    return {source_id for source_id, visible in visibility.items() if visible}


def cable_blockers_for_user(snapshot, user, groups):
    """Redact failed auxiliary port sources without changing shared snapshot facts."""
    source = snapshot.resources[Resource.CABLES]
    paths = {
        snapshot.responses[key].request.path: int(part.removeprefix(NEIGHBOUR_PORTS_PREFIX))
        for mapping in (source.host, source.oob)
        if mapping is not None
        for part, key in mapping.parts
        if part.startswith(NEIGHBOUR_PORTS_PREFIX)
    }
    needed = {paths[blocker.path] for blockers in groups.values() for blocker in blockers if blocker.path in paths}
    if not needed:
        return groups
    visible = visible_cable_port_sources(user, snapshot.server_key, needed)
    redacted = Blocker("", "Port acquisition failed for a device or interface you cannot view.")
    return {
        scope: tuple(
            dict.fromkeys(
                redacted if blocker.path in paths and paths[blocker.path] not in visible else blocker
                for blocker in blockers
            )
        )
        for scope, blockers in groups.items()
    }


def refresh(request, subject, resource, server_key):
    """
    Acquire the resource's snapshot and store it; a refresh that stores nothing leaves no older snapshot.

    The caller runs discovery first, so the subject's mapping exists when one could be found.

    """
    if request is None:
        raise TypeError("A source refresh needs a request.")
    options = {}
    if RESOURCE_SPECS[resource].serial_sensors:
        owner = resource_owner(subject, resource, server_key)
        options["include_serial"] = can_view_serial_ports(request.user, owner)
    delete(subject, resource, server_key)
    try:
        snapshot = acquire(subject, server_key=server_key, resources=[resource], **options)
    except SubjectNotMapped:
        # Discovery can keep an id in the cache only, when the mapping custom field is missing.
        return Refresh(None, "Device not found in LibreNMS.")
    blocked = blocked_scopes(snapshot, resource)
    if resource is Resource.CABLES:
        blocked = cable_blockers_for_user(snapshot, request.user, blocked)
    if store(subject, resource, snapshot):
        return Refresh(snapshot, blocked=blocked)
    return Refresh(snapshot, blocker_notice(blocker for blockers in blocked.values() for blocker in blockers), blocked)


def blocker_notice(blockers):
    """Return one notice that names each failed source once."""
    return "; ".join(dict.fromkeys(blocker.message for blocker in blockers))


def store(subject, resource, snapshot):
    """
    Store the resource's snapshot when at least one of its scopes can be used.

    Returns:
        bool: Whether the snapshot was stored. A snapshot with no usable scope is not.

    """
    key = snapshot_key(subject, resource, snapshot.server_key)
    if not is_ready(snapshot, resource):
        cache.delete(key)
        return False
    cache.set(key, snapshot.to_payload(), timeout=configured_cache_timeout(snapshot.server_key))
    return True


def delete(subject, resource, server_key):
    """Drop the resource's snapshot, so a failed refresh never leaves an older one to act on."""
    cache.delete(snapshot_key(subject, resource, server_key))


def read(subject, resource, server_key):
    """
    Return the resource's snapshot, or None when there is none to act on.

    A payload of another schema or plugin version, or one acquired for another mapping than the current
    one, reads as None. A read never deletes: a refresh can store a new snapshot under the same key.

    """
    owner = resource_owner(subject, resource, server_key)
    snapshot = SourceSnapshot.from_payload(cache.get(_key(owner, resource, server_key)))
    if snapshot is None or resource not in snapshot.resources or not _binding_holds(snapshot, resource, owner):
        return None
    return snapshot


def _binding_holds(snapshot, resource, owner):
    """Return whether the snapshot was acquired for the owner's current mapping, its OOB link included."""
    source = snapshot.resources[resource]
    mapping = read_mapping(owner)
    host_id = source.host.librenms_id if source.host is not None else None
    if source.owner != ObjectRef.of(owner) or mapping.own_id(snapshot.server_key) != host_id:
        return False
    if resource not in OOB_ACQUIRERS:
        return True
    return oob_link(mapping, snapshot.server_key) == (source.oob.link if source.oob is not None else None)


def remaining_ttl(subject, resource, server_key):
    """Return the seconds the resource's snapshot has left, or None without a snapshot."""
    # An entry that read() refuses, e.g. after a mapping change, still has its TTL.
    if read(subject, resource, server_key) is None:
        return None
    ttl = cache.ttl(snapshot_key(subject, resource, server_key))
    return ttl if ttl and ttl > 0 else None
