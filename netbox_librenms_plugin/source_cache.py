"""The web transport of source snapshots: the shared cache the sync page reads and refreshes."""

from dataclasses import dataclass, field

from dcim.models import ConsoleServerPort, Device
from django.apps import apps
from django.core.cache import cache
from django.http import Http404

from netbox_librenms_plugin.acquisition.coordinator import (
    SubjectNotMapped,
    acquire,
    oob_link,
    plan_acquisition,
    resource_owner,
)
from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, Resource
from netbox_librenms_plugin.acquisition.resources import OOB_ACQUIRERS, blocked_scopes, is_ready
from netbox_librenms_plugin.acquisition.snapshot import (
    MappingLink,
    MappingState,
    ObjectRef,
    SourceBinding,
    SourceSnapshot,
)
from netbox_librenms_plugin.librenms_api import configured_cache_timeout, stored_librenms_id
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.sync_cache import sync_snapshot_key

IDENTITY_CHANGED = "LibreNMS source identity changed or expired. Refresh this tab and try again."


class SourceChanged(Exception):
    """The acquired snapshot no longer names the page's current source."""


class SnapshotUnavailable(Exception):
    """The acquired receipt has no matching snapshot in the shared cache."""


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
    options = {}
    if resource is Resource.CABLES:
        options["include_serial"] = bool(
            isinstance(owner, Device)
            and request.user.has_perm("dcim.view_consoleserverport")
            and ConsoleServerPort.objects.restrict(request.user, "view").filter(device=owner).exists()
        )
    delete_planned(plan)
    try:
        snapshot = acquire(plan, **options)
    except SubjectNotMapped:
        return Refresh(None, IDENTITY_CHANGED)
    blocked = blocked_scopes(snapshot, resource)
    if store(plan.subject, resource, snapshot):
        return Refresh(snapshot, blocked=blocked)
    if is_ready(snapshot, resource):
        return Refresh(None, IDENTITY_CHANGED, blocked)
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
    """Compare the acquired binding with the owner's current effective identity and source."""
    host_id = stored_librenms_id(owner, snapshot.server_key)
    link = oob_link(read_mapping(owner), snapshot.server_key) if resource in OOB_ACQUIRERS else None
    current = SourceBinding(
        snapshot.server_key,
        ObjectRef.of(owner),
        MappingLink(MappingState.RESOLVED, host_id) if host_id is not None else None,
        link,
    )
    return snapshot.binding(resource) == current


def receipt_owner(subject, resource, server_key, snapshot, *, user=None):
    """Return the current owner only when this exact snapshot still names the page's selected source."""
    if snapshot.server_key != server_key or resource not in snapshot.resources:
        return None
    current = type(subject).objects.filter(pk=subject.pk).first()
    if current is None:
        return None
    owner = resource_owner(current, resource, server_key)
    if not _binding_holds(snapshot, resource, owner):
        return None
    if user is not None and not snapshot_visible(user, snapshot, resource):
        return None
    return owner


def require_current(subject, resource, server_key, snapshot, *, user=None):
    """Require the acquired receipt to remain current before assessment, without loading another snapshot."""
    owner = receipt_owner(subject, resource, server_key, snapshot, user=user)
    if owner is None:
        raise SourceChanged(IDENTITY_CHANGED)
    return owner


def require_cached(subject, resource, server_key, snapshot, *, user=None):
    """Require a current receipt and an available cache entry for that same source."""
    owner = require_current(subject, resource, server_key, snapshot, user=user)
    cached = SourceSnapshot.from_payload(cache.get(receipt_key(snapshot, resource)))
    if cached is None or resource not in cached.resources or cached.binding(resource) != snapshot.binding(resource):
        raise SnapshotUnavailable("The acquired source snapshot is not cached.")
    return owner


def receipt_key(snapshot, resource):
    """Return the acquired owner's cache key without selecting a different source owner."""
    ref = snapshot.resources[resource].owner
    owner = apps.get_model(ref.label)(pk=ref.pk)
    return _key(owner, resource, snapshot.server_key)


def remaining_ttl(subject, resource, server_key, *, snapshot=None):
    """Return the seconds the resource's snapshot has left, or None without a snapshot."""
    # An entry that read() refuses, e.g. after a mapping change, still has its TTL.
    if snapshot is None:
        snapshot = read(subject, resource, server_key)
    if snapshot is None or receipt_owner(subject, resource, server_key, snapshot) is None:
        return None
    ttl = cache.ttl(receipt_key(snapshot, resource))
    return ttl if ttl and ttl > 0 else None
