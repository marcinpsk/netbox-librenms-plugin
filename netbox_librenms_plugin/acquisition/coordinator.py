"""Acquire the source snapshot of several resources for one synchronization subject."""

from uuid import uuid4

from django.utils import timezone
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, OwnerPolicy
from netbox_librenms_plugin.acquisition.resources import ACQUIRERS
from netbox_librenms_plugin.acquisition.snapshot import (
    MappingSource,
    MappingState,
    ObjectRef,
    ResourceSource,
    ResponseRecord,
    SourceSnapshot,
)
from netbox_librenms_plugin.librenms_api import build_librenms_api
from netbox_librenms_plugin.server_mappings import MappingRole, read_mapping, require_server_key
from netbox_librenms_plugin.server_selection import get_librenms_sync_device


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

    def __call__(self, endpoint):
        key = endpoint.request.key
        if key not in self.responses:
            status, body = self._api.get_response(endpoint.request.path, dict(endpoint.request.params))
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


def resource_owner(subject, resource, server_key):
    """Return the object a resource is acquired for: the subject, or its Virtual Chassis sync owner."""
    if RESOURCE_SPECS[resource].owner_policy is OwnerPolicy.SUBJECT:
        return subject
    return get_librenms_sync_device(subject, server_key=server_key) or subject


def acquire(subject, *, server_key, resources):
    """
    Acquire the source snapshot of each resource from one configured LibreNMS server.

    Acquisition needs no request, session or user, and it writes neither NetBox nor the cache.

    Args:
        subject (Device | VirtualMachine): The synchronization subject.
        server_key (str): A configured LibreNMS server key.
        resources (Iterable[Resource]): The resources to acquire.

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
    sources = {}
    for resource in resources:
        owner = resource_owner(subject, resource, server_key)
        librenms_id = read_mapping(owner).own_id(server_key)
        if librenms_id is None:
            raise SubjectNotMapped(owner, server_key)
        parts = ACQUIRERS[resource](fetch, librenms_id)
        sources[resource] = ResourceSource(
            resource=resource,
            owner=ObjectRef.of(owner),
            host=MappingSource(
                role=MappingRole.OWN,
                librenms_id=librenms_id,
                state=MappingState.RESOLVED,
                parts=tuple((name, record.request.key) for name, record in parts.items()),
            ),
        )
    return SourceSnapshot(
        snapshot_id=uuid4().hex,
        server_key=server_key,
        subject=ObjectRef.of(subject),
        started_at=started_at,
        finished_at=timezone.now(),
        resources=sources,
        responses=fetch.responses,
    )
