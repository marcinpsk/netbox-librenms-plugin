"""The source snapshot: the LibreNMS responses one acquisition obtained, and nothing derived from them."""

import copy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from netbox_librenms_plugin import __version__
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.server_mappings import MappingRole

# Increase when the payload changes meaning. A reader also refuses a payload that another plugin version
# wrote, because a decoder change alters what a stored value means.
SNAPSHOT_SCHEMA_VERSION = 2


class Outcome(StrEnum):
    """What acquisition got for one request."""

    # The response passed its endpoint contract. A valid empty list is obtained too.
    OBTAINED = "obtained"
    FAILED = "failed"
    NOT_REQUESTED = "not_requested"
    NOT_APPLICABLE = "not_applicable"


class MappingState(StrEnum):
    """Whether a mapping's stored LibreNMS id was usable."""

    RESOLVED = "resolved"
    INVALID = "invalid"


class IncompleteSource(Exception):
    """A reader asked for a response that acquisition did not obtain."""

    def __init__(self, record):
        super().__init__(f"{record.request.route_key} was not obtained: {record.outcome} ({record.reason})")
        self.record = record


@dataclass(frozen=True)
class SourceRequest:
    """One LibreNMS GET, with the key a data-shape recording stores it under."""

    path: str
    params: tuple[tuple[str, str], ...] = ()
    # A recording keys some routes on the path only, so replay serves them for any query.
    key_params: bool = True

    @property
    def key(self):
        """Return the request's identity: two requests to one path with other parameters differ."""
        return self.path + "".join(f"&{name}={value}" for name, value in self.params)

    @property
    def route_key(self):
        """Return the key a data-shape recording stores the response under."""
        key = f"GET /api/v0/{self.path}"
        if self.key_params and self.params:
            key += "?" + "&".join(f"{name}={value}" for name, value in self.params)
        return key


@dataclass(frozen=True)
class ResponseRecord:
    """What one request returned: the verbatim body and the value its contract validated."""

    request: SourceRequest
    outcome: Outcome
    http_status: int | None = None
    body: object = None
    value: object = None
    reason: str = ""
    received_at: datetime | None = None

    @property
    def obtained(self):
        return self.outcome is Outcome.OBTAINED


@dataclass(frozen=True)
class ObjectRef:
    """A NetBox object by model label and primary key."""

    label: str
    pk: int

    @classmethod
    def of(cls, obj):
        return cls(obj._meta.label_lower, obj.pk)


@dataclass(frozen=True)
class MappingLink:
    """The state and the LibreNMS id of one mapping link, so a stored mapping and a current one compare."""

    state: MappingState
    librenms_id: int | None


@dataclass(frozen=True)
class MappingSource:
    """The requests made for one LibreNMS device: the owner itself, or its out-of-band controller."""

    role: MappingRole
    librenms_id: int | None
    state: MappingState
    # Part name -> request key of the record in SourceSnapshot.responses.
    parts: tuple[tuple[str, str], ...]

    @property
    def link(self):
        """Return the mapping link that this source was acquired for."""
        return MappingLink(self.state, self.librenms_id)


@dataclass(frozen=True)
class ResourceSource:
    """The mappings one resource was acquired from."""

    resource: Resource
    owner: ObjectRef
    host: MappingSource | None
    # None means the owner has no out-of-band controller.
    oob: MappingSource | None = None

    def mapping(self, role):
        return self.host if role is MappingRole.OWN else self.oob


@dataclass(frozen=True)
class SourceSnapshot:
    """Every response one acquisition obtained, per resource and mapping."""

    snapshot_id: str
    server_key: str
    subject: ObjectRef
    started_at: datetime
    finished_at: datetime
    resources: dict
    responses: dict

    def record(self, resource, part, role=MappingRole.OWN):
        """Return the response record of one part of a resource's mapping."""
        mapping = self.resources[resource].mapping(role)
        if mapping is None:
            raise LookupError(f"The {resource} source has no {role} mapping.")
        return self.responses[dict(mapping.parts)[part]]

    def value(self, resource, part, role=MappingRole.OWN):
        """Return a copy of an obtained part's value; raise IncompleteSource for any other outcome."""
        record = self.record(resource, part, role)
        if not record.obtained:
            raise IncompleteSource(record)
        return copy.deepcopy(record.value)

    def to_payload(self):
        """Return the plain data the web transport stores."""
        return {
            "schema": SNAPSHOT_SCHEMA_VERSION,
            "plugin_version": __version__,
            "snapshot_id": self.snapshot_id,
            "server_key": self.server_key,
            "subject": (self.subject.label, self.subject.pk),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "resources": [_resource_payload(source) for source in self.resources.values()],
            "responses": [_record_payload(record) for record in self.responses.values()],
        }

    @classmethod
    def from_payload(cls, payload):
        """Return the snapshot a payload holds, or None when another schema or plugin version wrote it."""
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != SNAPSHOT_SCHEMA_VERSION
            or payload.get("plugin_version") != __version__
        ):
            return None
        resources = {}
        for item in payload["resources"]:
            source = ResourceSource(
                resource=Resource(item["resource"]),
                owner=ObjectRef(*item["owner"]),
                host=_mapping_from_payload(item["host"]),
                oob=_mapping_from_payload(item["oob"]) if item["oob"] is not None else None,
            )
            resources[source.resource] = source
        responses = {}
        for item in payload["responses"]:
            record = ResponseRecord(
                request=SourceRequest(item["path"], tuple(tuple(pair) for pair in item["params"]), item["key_params"]),
                outcome=Outcome(item["outcome"]),
                http_status=item["http_status"],
                body=item["body"],
                value=item["value"],
                reason=item["reason"],
                received_at=item["received_at"],
            )
            responses[record.request.key] = record
        return cls(
            snapshot_id=payload["snapshot_id"],
            server_key=payload["server_key"],
            subject=ObjectRef(*payload["subject"]),
            started_at=payload["started_at"],
            finished_at=payload["finished_at"],
            resources=resources,
            responses=responses,
        )


def _mapping_payload(mapping):
    if mapping is None:
        return None
    return {
        "role": mapping.role.value,
        "librenms_id": mapping.librenms_id,
        "state": mapping.state.value,
        "parts": [list(part) for part in mapping.parts],
    }


def _mapping_from_payload(item):
    if item is None:
        return None
    return MappingSource(
        role=MappingRole(item["role"]),
        librenms_id=item["librenms_id"],
        state=MappingState(item["state"]),
        parts=tuple(tuple(part) for part in item["parts"]),
    )


def _resource_payload(source):
    return {
        "resource": source.resource.value,
        "owner": (source.owner.label, source.owner.pk),
        "host": _mapping_payload(source.host),
        "oob": _mapping_payload(source.oob) if source.oob is not None else None,
    }


def _record_payload(record):
    return {
        "path": record.request.path,
        "params": [list(pair) for pair in record.request.params],
        "key_params": record.request.key_params,
        "outcome": record.outcome.value,
        "http_status": record.http_status,
        "body": record.body,
        "value": record.value,
        "reason": record.reason,
        "received_at": record.received_at,
    }
