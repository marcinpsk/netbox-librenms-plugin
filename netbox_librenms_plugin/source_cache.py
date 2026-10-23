"""The web transport of source snapshots: the shared cache the sync page reads and refreshes."""

from django.core.cache import cache

from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, OwnerPolicy
from netbox_librenms_plugin.acquisition.resources import blocked_scopes, is_ready
from netbox_librenms_plugin.acquisition.snapshot import ObjectRef, SourceSnapshot
from netbox_librenms_plugin.librenms_api import configured_cache_timeout
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.sync_cache import sync_snapshot_key


def snapshot_owner(subject, resource, server_key):
    """Return the object whose cache entry holds the resource's snapshot."""
    if RESOURCE_SPECS[resource].owner_policy is OwnerPolicy.SUBJECT:
        return subject
    return get_librenms_sync_device(subject, server_key=server_key) or subject


def snapshot_key(subject, resource, server_key):
    """Return the cache key of the resource's snapshot on one server."""
    return sync_snapshot_key(
        snapshot_owner(subject, resource, server_key), RESOURCE_SPECS[resource].data_type, server_key
    )


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

    A payload of another schema, or one acquired for another mapping than the current one, is
    deleted and reads as None.

    """
    owner = snapshot_owner(subject, resource, server_key)
    key = sync_snapshot_key(owner, RESOURCE_SPECS[resource].data_type, server_key)
    payload = cache.get(key)
    if payload is None:
        return None
    snapshot = SourceSnapshot.from_payload(payload)
    if snapshot is None or resource not in snapshot.resources or not _binding_holds(snapshot, resource, owner):
        cache.delete(key)
        return None
    return snapshot


def _binding_holds(snapshot, resource, owner):
    """Return whether the snapshot was acquired for the owner's current mapping."""
    source = snapshot.resources[resource]
    return source.owner == ObjectRef.of(owner) and read_mapping(owner).own_id(snapshot.server_key) == (
        source.host.librenms_id
    )


def remaining_ttl(subject, resource, server_key):
    """Return the seconds the resource's snapshot has left, or None without a snapshot."""
    ttl = cache.ttl(snapshot_key(subject, resource, server_key))
    return ttl if ttl and ttl > 0 else None


def blocked_notices(snapshot, resource):
    """Return one notice per blocked scope, naming the response that blocks it."""
    return [
        f"LibreNMS data is incomplete: {records[0].request.path} was not obtained ({records[0].reason})."
        for records in blocked_scopes(snapshot, resource).values()
    ]
