"""The web transport of source snapshots: the shared cache the sync page reads and refreshes."""

from dataclasses import dataclass, field

from dcim.models import ConsoleServerPort, Device
from django.core.cache import cache

from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire, oob_link, resource_owner
from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, Resource
from netbox_librenms_plugin.acquisition.resources import OOB_ACQUIRERS, blocked_scopes, is_ready
from netbox_librenms_plugin.acquisition.snapshot import ObjectRef, SourceSnapshot
from netbox_librenms_plugin.librenms_api import configured_cache_timeout
from netbox_librenms_plugin.server_mappings import read_mapping
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


def refresh(subject, resource, server_key, *, request=None):
    """
    Acquire the resource's snapshot and store it; a refresh that stores nothing leaves no older snapshot.

    The caller runs discovery first, so the subject's mapping exists when one could be found.

    """
    options = {}
    if resource is Resource.CABLES:
        owner = resource_owner(subject, resource, server_key)
        options["include_serial"] = bool(
            request is not None
            and isinstance(owner, Device)
            and request.user.has_perm("dcim.view_consoleserverport")
            and ConsoleServerPort.objects.restrict(request.user, "view").filter(device=owner).exists()
        )
    delete(subject, resource, server_key)
    try:
        snapshot = acquire(subject, server_key=server_key, resources=[resource], **options)
    except SubjectNotMapped:
        # Discovery can keep an id in the cache only, when the mapping custom field is missing.
        return Refresh(None, "Device not found in LibreNMS.")
    blocked = blocked_scopes(snapshot, resource)
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
    if source.owner != ObjectRef.of(owner) or mapping.own_id(snapshot.server_key) != source.host.librenms_id:
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
