"""Acquire the source snapshot of several resources for one synchronization subject."""

from uuid import uuid4

from django.utils import timezone
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, OwnerPolicy
from netbox_librenms_plugin.acquisition.resources import ACQUIRERS, OOB_ACQUIRERS
from netbox_librenms_plugin.acquisition.snapshot import (
    MappingLink,
    MappingSource,
    MappingState,
    ObjectRef,
    Outcome,
    ResourceSource,
    ResponseRecord,
    SourceSnapshot,
)
from netbox_librenms_plugin.librenms_api import EXTENDED_API_TIMEOUT, build_librenms_api
from netbox_librenms_plugin.server_mappings import MappingRole, read_mapping, require_server_key
from netbox_librenms_plugin.server_selection import get_librenms_sync_device


INVALID_OOB_REASON = "The linked OOB controller has no valid LibreNMS id."


class SubjectNotMapped(Exception):
    """The resource's owner has no LibreNMS identity on the server; acquisition never discovers one."""

    def __init__(self, owner, server_key):
        super().__init__(f"{owner} has no LibreNMS mapping on server {server_key!r}.")
        self.owner = owner
        self.server_key = server_key


class _Fetch:
    """Make each distinct request once per acquisition and record what its contract decided."""

    def __init__(self, api):
        self._api = api
        self.responses = {}
        self.neighbour_deadline = None

    def __call__(self, endpoint):
        key = endpoint.request.key
        if key not in self.responses or self.responses[key].received_at is None:
            # A budget omission is still available to a resource that requires this request.
            options = {"timeout": EXTENDED_API_TIMEOUT} if endpoint.extended_timeout else {}
            status, body = self._api.get_response(endpoint.request.path, dict(endpoint.request.params), **options)
            decoded = endpoint.decode(status, body)
            self.responses[key] = ResponseRecord(
                request=endpoint.request,
                outcome=decoded.outcome,
                http_status=status,
                body=body if decoded.body is None else decoded.body,
                value=decoded.value,
                reason=decoded.reason,
                received_at=timezone.now(),
            )
        return self.responses[key]

    def skip(self, endpoint, outcome, reason):
        """Record a request that acquisition did not make, and why."""
        record = ResponseRecord(request=endpoint.request, outcome=outcome, reason=reason)
        self.responses.setdefault(endpoint.request.key, record)
        return self.responses[endpoint.request.key]


class _Refused:
    """Record requests not made because a mapping has no usable device."""

    def __init__(self, fetch, reason):
        self._fetch = fetch
        self._reason = reason

    def __call__(self, endpoint):
        return self._fetch.skip(endpoint, Outcome.FAILED, self._reason)


def oob_link(mapping, server_key):
    """
    Return the OOB controller link that a mapping records on the server.

    Args:
        mapping (server_mappings.MappingState): The owner's mapping, from ``read_mapping``.
        server_key (str): The LibreNMS server key.

    Returns:
        MappingLink | None: The link, or None when the owner links no OOB controller. A corrupt stored
            id is an INVALID link with no id.

    """
    if not mapping.has_oob(server_key):
        return None
    oob_id = mapping.oob_id(server_key)
    return MappingLink(MappingState.RESOLVED if oob_id is not None else MappingState.INVALID, oob_id)


def _part_keys(parts):
    return tuple((name, record.request.key) for name, record in parts.items())


def oob_source(fetch, resource, mapping, server_key):
    """
    Acquire the OOB controller as its own mapping through *fetch*, or return None without one.

    Returns:
        MappingSource | None: None when the resource shows no OOB rows or the owner links no OOB controller.

    """
    link = oob_link(mapping, server_key) if resource in OOB_ACQUIRERS else None
    if link is None:
        return None
    oob_fetch = fetch if link.state is MappingState.RESOLVED else _Refused(fetch, INVALID_OOB_REASON)
    return MappingSource(
        role=MappingRole.OOB,
        librenms_id=link.librenms_id,
        state=link.state,
        parts=_part_keys(OOB_ACQUIRERS[resource](oob_fetch, link.librenms_id)),
    )


def _resource_source(fetch, resource, owner, server_key, *, include_serial=False):
    """Acquire one resource from the owner's own mapping and, when it links one, its OOB controller."""
    mapping = read_mapping(owner)
    librenms_id = mapping.own_id(server_key)
    spec = RESOURCE_SPECS[resource]
    oob_only = spec.permits_oob_only and librenms_id is None and mapping.has_oob(server_key)
    if librenms_id is None and not oob_only:
        raise SubjectNotMapped(owner, server_key)
    host = None
    if librenms_id is not None:
        parts = ACQUIRERS[resource](fetch, librenms_id, **spec.acquisition_options(include_serial=include_serial))
        host = MappingSource(
            role=MappingRole.OWN,
            librenms_id=librenms_id,
            state=MappingState.RESOLVED,
            parts=_part_keys(parts),
        )
    return ResourceSource(
        resource=resource,
        owner=ObjectRef.of(owner),
        host=host,
        oob=oob_source(fetch, resource, mapping, server_key),
    )


def resource_owner(subject, resource, server_key):
    """Return the object a resource is acquired for: the subject, or its Virtual Chassis sync owner."""
    if RESOURCE_SPECS[resource].owner_policy is OwnerPolicy.SUBJECT:
        return subject
    return get_librenms_sync_device(subject, server_key=server_key) or subject


def acquire(subject, *, server_key, resources, include_serial=False):
    """
    Acquire the source snapshot of each resource from one configured LibreNMS server.

    Acquisition needs no request, session or user, and it writes neither NetBox nor the cache.

    Args:
        subject (Device | VirtualMachine): The synchronization subject.
        server_key (str): A configured LibreNMS server key.
        resources (Iterable[Resource]): The resources to acquire.
        include_serial (bool): Whether cables acquisition requests serial sensors.

    Returns:
        SourceSnapshot: Every response obtained, with the outcome of each request.

    Raises:
        ValueError: The server key is blank or names no configured server, or a resource has no
            acquisition or does not apply to the subject.
        SubjectNotMapped: A resource owner has no LibreNMS mapping on the server.

    """
    require_server_key(server_key)
    resources = tuple(resources)
    api = build_librenms_api(server_key)
    if api is None or api.server_key != server_key:
        raise ValueError(f"LibreNMS server {server_key!r} is not configured.")
    for resource in resources:
        if resource not in ACQUIRERS:
            raise ValueError(f"No acquisition exists for the {resource} resource.")
        if isinstance(subject, VirtualMachine) and not RESOURCE_SPECS[resource].supports_virtual_machine:
            raise ValueError(f"A virtual machine has no {resource} resource.")

    started_at = timezone.now()
    fetch = _Fetch(api)
    sources = {
        resource: _resource_source(
            fetch, resource, resource_owner(subject, resource, server_key), server_key, include_serial=include_serial
        )
        for resource in resources
    }
    return SourceSnapshot(
        snapshot_id=uuid4().hex,
        server_key=server_key,
        subject=ObjectRef.of(subject),
        started_at=started_at,
        finished_at=timezone.now(),
        resources=sources,
        responses=fetch.responses,
    )
