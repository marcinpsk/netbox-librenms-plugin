"""The web transport of source snapshots: the shared cache the sync page reads and refreshes."""

from dataclasses import dataclass

from django.apps import apps
from django.core.cache import cache
from django.http import Http404

from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire, plan_acquisition, resource_owner
from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS
from netbox_librenms_plugin.acquisition.resources import blocked_scopes, is_ready
from netbox_librenms_plugin.acquisition.snapshot import ObjectRef, SourceSnapshot
from netbox_librenms_plugin.librenms_api import configured_cache_timeout, stored_librenms_id
from netbox_librenms_plugin.sync_cache import sync_snapshot_key

IDENTITY_CHANGED = "LibreNMS source identity changed or expired. Refresh this tab and try again."


@dataclass(frozen=True)
class Refresh:
    """The result of one refresh: the acquired snapshot, and why nothing was stored when it was not."""

    snapshot: SourceSnapshot | None
    error: str | None = None


def _key(owner, resource, server_key):
    return sync_snapshot_key(owner, RESOURCE_SPECS[resource].data_type, server_key)


def snapshot_key(subject, resource, server_key):
    """Return the cache key of the resource's snapshot on one server."""
    return _key(resource_owner(subject, resource, server_key), resource, server_key)


def authorized_plan(request, subject, *, server_key, resources):
    """Resolve and authorize every source owner before discovery or any resource request."""
    if request is None:
        raise TypeError("A source refresh needs a request.")
    plan = plan_acquisition(subject, server_key=server_key, resources=resources)
    for _resource, owner in plan.owners:
        if not type(owner).objects.restrict(request.user, "view").filter(pk=owner.pk).exists():
            raise Http404
    return plan


def snapshot_visible(user, snapshot, resource):
    """Return whether the actor can still view the source owner bound to this snapshot."""
    ref = snapshot.resources[resource].owner
    model = apps.get_model(ref.label)
    return model.objects.restrict(user, "view").filter(pk=ref.pk).exists()


def refresh(request, plan):
    """Acquire one authorized resource and replace its snapshot; a failed refresh clears old data."""
    if request is None:
        raise TypeError("A source refresh needs a request.")
    ((resource, owner),) = plan.owners
    if not type(owner).objects.restrict(request.user, "view").filter(pk=owner.pk).exists():
        raise Http404
    delete_planned(plan)
    try:
        snapshot = acquire(plan)
    except SubjectNotMapped:
        return Refresh(None, IDENTITY_CHANGED)
    if store(plan.subject, resource, snapshot):
        return Refresh(snapshot)
    if is_ready(snapshot, resource):
        return Refresh(None, IDENTITY_CHANGED)
    record = next(iter(blocked_scopes(snapshot, resource).values()))[0]
    return Refresh(snapshot, f"{record.request.path} was not obtained ({record.reason})")


def store(subject, resource, snapshot):
    """
    Store the resource's snapshot when at least one of its scopes can be used.

    Returns:
        bool: Whether the snapshot was stored. A snapshot with no usable scope is not.

    """
    current = type(subject).objects.filter(pk=subject.pk).first()
    if current is None:
        return False
    owner = resource_owner(current, resource, snapshot.server_key)
    key = _key(owner, resource, snapshot.server_key)
    if not _binding_holds(snapshot, resource, owner):
        return False
    if not is_ready(snapshot, resource):
        cache.delete(key)
        return False
    cache.set(key, snapshot.to_payload(), timeout=configured_cache_timeout(snapshot.server_key))
    return True


def delete_planned(plan):
    """Drop the selected owners' snapshots without resolving a different owner after planning."""
    for resource, owner in plan.owners:
        cache.delete(_key(owner, resource, plan.server_key))


def delete(subject, resource, server_key):
    """Drop the resource's snapshot, so a failed refresh never leaves an older one to act on."""
    cache.delete(snapshot_key(subject, resource, server_key))


def read(subject, resource, server_key, *, user=None):
    """
    Return the resource's snapshot, or None when there is none to act on.

    A payload of another schema or plugin version, or one acquired for another mapping than the current
    one, reads as None. A read never deletes: a refresh can store a new snapshot under the same key.

    """
    current = type(subject).objects.filter(pk=subject.pk).first()
    if current is None:
        return None
    owner = resource_owner(current, resource, server_key)
    snapshot = SourceSnapshot.from_payload(cache.get(_key(owner, resource, server_key)))
    if snapshot is None or resource not in snapshot.resources or not _binding_holds(snapshot, resource, owner):
        return None
    if user is not None and not snapshot_visible(user, snapshot, resource):
        return None
    return snapshot


def _binding_holds(snapshot, resource, owner):
    """Return whether the snapshot was acquired for the owner's current effective identity."""
    source = snapshot.resources[resource]
    return source.owner == ObjectRef.of(owner) and stored_librenms_id(owner, snapshot.server_key) == (
        source.host.librenms_id
    )


def remaining_ttl(subject, resource, server_key):
    """Return the seconds the resource's snapshot has left, or None without a snapshot."""
    # An entry that read() refuses, e.g. after a mapping change, still has its TTL.
    if read(subject, resource, server_key) is None:
        return None
    ttl = cache.ttl(snapshot_key(subject, resource, server_key))
    return ttl if ttl and ttl > 0 else None
