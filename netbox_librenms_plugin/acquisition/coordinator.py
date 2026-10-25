"""Acquire the source snapshot of several resources for one synchronization subject."""

from dataclasses import dataclass
from uuid import uuid4

from dcim.models import Device
from django.utils import timezone
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, OwnerPolicy
from netbox_librenms_plugin.acquisition.resources import (
    ACQUIRERS,
    OOB_ACQUIRERS,
    NEIGHBOUR_PORTS_PREFIX,
    fetch_cable_ports,
)
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
from netbox_librenms_plugin.librenms_api import EXTENDED_API_TIMEOUT, build_librenms_api, stored_librenms_id
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
        self.server_key = api.server_key
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
    librenms_id = stored_librenms_id(owner, server_key)
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


def _server_api(server_key):
    require_server_key(server_key)
    api = build_librenms_api(server_key)
    if api is None or api.server_key != server_key:
        raise ValueError(f"LibreNMS server {server_key!r} is not configured.")
    return api


def acquire_cable_ports(server_key, owner_ids):
    """Acquire request-scoped port evidence without changing an existing snapshot or the cache."""
    fetch = _Fetch(_server_api(server_key))
    owner_ids = tuple(dict.fromkeys(owner_ids))
    parts = fetch_cable_ports(fetch, owner_ids)
    return {owner_id: parts[f"{NEIGHBOUR_PORTS_PREFIX}{owner_id}"] for owner_id in owner_ids}



@dataclass(frozen=True)
class AcquisitionPlan:
    """The subject, server and resolved owners of one acquisition, before any HTTP request."""

    subject: Device | VirtualMachine
    server_key: str
    owners: tuple

    def owner(self, resource):
        """Return the owner resolved for one selected resource."""
        return dict(self.owners)[resource]


def plan_acquisition(subject, *, server_key, resources):
    """Validate the selected resources and resolve each owner once, without discovery or HTTP."""
    require_server_key(server_key)
    if not isinstance(subject, (Device, VirtualMachine)):
        raise ValueError("Source acquisition supports only devices and virtual machines.")
    resources = tuple(resources)
    for resource in resources:
        if not isinstance(resource, Resource):
            raise ValueError("Resources must be Resource values.")
        if resource not in ACQUIRERS:
            raise ValueError(f"No acquisition exists for the {resource} resource.")
        if isinstance(subject, VirtualMachine) and not RESOURCE_SPECS[resource].supports_virtual_machine:
            raise ValueError(f"A virtual machine has no {resource} resource.")
    return AcquisitionPlan(
        subject, server_key, tuple((resource, resource_owner(subject, resource, server_key)) for resource in resources)
    )


def acquire(plan, *, include_serial=False):
    """
    Acquire the planned owners' current stored or cached identities from one configured server.

    The caller authorizes all planned owners before discovery or HTTP when acting for a user.
    Acquisition needs no request, session or user. It reads the existing identity cache, but writes
    neither NetBox nor the cache, and never discovers an identity or resolves an owner again.
    """
    if not isinstance(plan, AcquisitionPlan):
        raise TypeError("Source acquisition needs an AcquisitionPlan.")
    started_at = timezone.now()
    fetch = _Fetch(_server_api(plan.server_key))
    sources = {}
    for resource, planned_owner in plan.owners:
        owner = type(planned_owner).objects.filter(pk=planned_owner.pk).first()
        if owner is None:
            raise SubjectNotMapped(planned_owner, plan.server_key)
        sources[resource] = _resource_source(fetch, resource, owner, plan.server_key, include_serial=include_serial)
    return SourceSnapshot(
        snapshot_id=uuid4().hex,
        server_key=plan.server_key,
        subject=ObjectRef.of(plan.subject),
        started_at=started_at,
        finished_at=timezone.now(),
        resources=sources,
        responses=fetch.responses,
    )
