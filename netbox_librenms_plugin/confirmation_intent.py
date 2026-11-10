"""
Mint and verify confirmation intents: signed operations that an assessment offers and a writer applies.

An assessment offers each operation with a token. The applying route submits the tokens back to
``verify``, which authenticates them, rebuilds every approved operation from current state under the
writer's locks, and compares the two by name. Only an operation that compares equal reaches the writer.
"""

import dataclasses
import functools
import re
import types
import typing
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from django.core import signing

SALT_NAMESPACE = "netbox_librenms_plugin.confirmation_intent.v1"
MAX_AGE_SECONDS = 3600
# The repeated form field that carries the tokens of one request.
FIELD = "confirmation_intent"
ENVELOPE_VERSION = 1
MAX_TOKEN_LENGTH = 8192
_PARTS = ("identity", "choices", "assumptions", "effects")
_ENVELOPE_KEYS = frozenset({"v", "resource", "action", "actor", "subject", "server", "operation"})
_ACTION_NAME = re.compile(r"[a-z][a-z_]*")


class Resource(StrEnum):
    """The closed set of resources that carry confirmation intents. The value is the salt suffix."""

    VLAN = "vlan"
    IP_ADDRESS = "ip_address"
    INTERFACE = "interface"
    CABLE = "cable"
    MODULE = "module"
    DEVICE_FIELD = "device_field"
    IMPORT_IDENTITY_REPLACEMENT = "import_identity_replacement"
    MAPPING_LIFECYCLE = "mapping_lifecycle"
    MIGRATION = "migration"


class Role(StrEnum):
    """The role of an action on one applying route."""

    PRIMARY = "primary"
    PREREQUISITE = "prerequisite"


class StaleReason(StrEnum):
    """Why a dependency group does not reach its writer."""

    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"
    CONTEXT_CHANGED = "context_changed"
    ASSUMPTIONS_CHANGED = "assumptions_changed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class IntentContext:
    """The actor, subject and server that one intent belongs to."""

    actor_pk: int
    subject: tuple[str, int]
    server_key: str

    def __post_init__(self):
        if type(self.actor_pk) is not int:
            raise TypeError("IntentContext.actor_pk must be an int.")
        label, pk = self.subject if isinstance(self.subject, tuple) and len(self.subject) == 2 else (None, None)
        if type(label) is not str or not label or type(pk) is not int:
            raise TypeError("IntentContext.subject must be a (model label, pk) pair.")
        if type(self.server_key) is not str or not self.server_key:
            raise TypeError("IntentContext.server_key must be a non-empty str.")


def context_for(user, subject, server_key):
    """Return the context of *user* acting on the model instance *subject*, or None for an anonymous user."""
    if not getattr(user, "is_authenticated", False):
        return None
    return IntentContext(user.pk, (subject._meta.label_lower, subject.pk), server_key)


@dataclass(frozen=True)
class Offer:
    """One operation and the token that approves it."""

    token: str
    operation: object


@dataclass(frozen=True)
class Unavailable:
    """A rebuild result for an operation whose source or target cannot be rebuilt."""

    path: str


@dataclass(frozen=True)
class Verified:
    """A dependency group whose rebuilt members equal the approved ones, in the writer's schedule."""

    members: tuple[Offer, ...]


@dataclass(frozen=True)
class Stale:
    """A dependency group, or one submitted token, that must not write."""

    reason: StaleReason
    paths: tuple[str, ...]
    members: tuple = ()


@dataclass(frozen=True)
class Verification:
    """The outcome of one applying request: one result per dependency group or refused token."""

    results: tuple[Verified | Stale, ...]


@dataclass(frozen=True)
class RoutePolicy:
    """The (resource, action, role) entries that one applying route accepts."""

    entries: frozenset[tuple[Resource, str, Role]]

    def __post_init__(self):
        keys = [(resource, action) for resource, action, _role in self.entries]
        if not keys or len(keys) != len(set(keys)):
            raise ValueError("A route policy needs at least one entry and one role per action.")
        for resource, action, role in self.entries:
            if (resource, action) not in _ACTIONS or not isinstance(role, Role):
                raise ValueError(f"The route policy names an unregistered action {resource}.{action}.")

    def role(self, resource, action):
        """Return the role of an action on this route, or None when the route does not accept it."""
        return next((role for r, a, role in self.entries if (r, a) == (resource, action)), None)

    @property
    def resources(self):
        """Return the resources whose token prefix this route accepts."""
        return frozenset(resource for resource, _action, _role in self.entries)


_ACTIONS: dict[tuple[Resource, str], type] = {}


def register(cls):
    """Register a frozen operation dataclass as the schema of its ``RESOURCE`` and ``ACTION``."""
    resource, action = getattr(cls, "RESOURCE", None), getattr(cls, "ACTION", None)
    if not isinstance(resource, Resource) or type(action) is not str or not _ACTION_NAME.fullmatch(action):
        raise TypeError(f"{cls.__name__} must declare a Resource RESOURCE and a lower-case ACTION.")
    if (resource, action) in _ACTIONS:
        raise TypeError(f"{resource}.{action} is already registered.")
    if not _is_frozen_dataclass(cls) or tuple(field.name for field in dataclasses.fields(cls)) != _PARTS:
        raise TypeError(f"{cls.__name__} must be a frozen dataclass with the fields {_PARTS}.")
    for hint in _field_hints(cls).values():
        _check_schema(hint)
    _ACTIONS[(resource, action)] = cls
    return cls


def registered_actions():
    """Return every registered (resource, action) pair."""
    return frozenset(_ACTIONS)


def offer(operation, context):
    """Sign one operation for *context*. Only an assessment entry point calls this."""
    resource, action = _action_of(operation)
    if not isinstance(context, IntentContext):
        raise TypeError("offer() needs an IntentContext.")
    envelope = {
        "v": ENVELOPE_VERSION,
        "resource": resource.value,
        "action": action,
        "actor": context.actor_pk,
        "subject": list(context.subject),
        "server": context.server_key,
        "operation": _encode(operation, type(operation)),
    }
    return Offer(f"{resource.value}:{signing.dumps(envelope, salt=_salt(resource), compress=True)}", operation)


def signed_operations(tokens, *, policy):
    """
    Return the operations that authentic tokens sign, without context, duplicate or state checks.

    Use it only to widen a permission gate before ``verify`` runs. It compares nothing, so it never
    approves a write.
    """
    operations = []
    for token in tokens:
        decoded = _authenticate(token, policy)
        if not isinstance(decoded, Stale):
            operations.append(decoded[1])
    return tuple(operations)


def verify(tokens, *, policy, context, rebuild):
    """
    Verify the tokens of one applying request against *policy*, *context* and current state.

    Call it inside the caller's transaction. ``rebuild(approved)`` takes the writer's locks, rebuilds
    each approved operation from current state and its signed choices, writes nothing, and returns
    the dependency groups as sequences of ``(approved, rebuilt or Unavailable)`` pairs in the
    writer's schedule.
    """
    tokens = list(tokens)
    if not tokens:
        return Verification((Stale(StaleReason.MISSING, (FIELD,)),))
    refused = []
    authentic = []
    counts = Counter(tokens)
    for token in counts:
        if not token:
            refused.append(Stale(StaleReason.MISSING, (FIELD,)))
        elif isinstance(decoded := _authenticate(token, policy), Stale):
            refused.append(Stale(StaleReason.INVALID, (f"{FIELD}.duplicate",)) if counts[token] > 1 else decoded)
        elif paths := _context_differences(decoded[0], context):
            refused.append(Stale(StaleReason.CONTEXT_CHANGED, paths, (decoded[1],)))
        else:
            authentic.append((token, decoded[1]))
    by_identity = {}
    for token, operation in authentic:
        by_identity.setdefault((type(operation).RESOURCE, operation.identity), []).append((token, operation))
    approved = {}
    for members in by_identity.values():
        # A repeated token still counts against its identity, so it cannot clear the way for another token.
        if len(members) > 1:
            refused.append(Stale(StaleReason.INVALID, ("identity.duplicate",), tuple(op for _t, op in members)))
        elif counts[members[0][0]] > 1:
            refused.append(Stale(StaleReason.INVALID, (f"{FIELD}.duplicate",), (members[0][1],)))
        else:
            approved[members[0][1]] = members[0][0]
    if not approved:
        return Verification(tuple(refused))
    groups = rebuild(tuple(approved))
    return Verification(tuple(refused) + _verify_groups(groups, approved, policy))


def _verify_groups(groups, approved, policy):
    """Return one result per rebuilt group, after checking that the rebuild covered every approval once."""
    groups = [tuple(group) for group in groups]
    if not all(groups):
        raise TypeError("rebuild() returned an empty dependency group.")
    if Counter(operation for pairs in groups for operation, _rebuilt in pairs) != Counter(list(approved)):
        raise TypeError("rebuild() must return every approved operation in exactly one group.")
    return tuple(_verify_group(pairs, approved, policy) for pairs in groups)


def _verify_group(pairs, approved, policy):
    members = tuple(approved_operation for approved_operation, _rebuilt in pairs)
    unavailable = [rebuilt.path for _approved, rebuilt in pairs if isinstance(rebuilt, Unavailable)]
    if unavailable:
        return Stale(StaleReason.UNAVAILABLE, tuple(dict.fromkeys(unavailable)), members)
    # A group of prerequisites alone has no primary operation that consumes them.
    if not any(policy.role(*_action_of(operation)) is Role.PRIMARY for operation in members):
        return Stale(StaleReason.INVALID, ("role.unreferenced",), members)
    paths = []
    for approved_operation, rebuilt in pairs:
        _action_of(rebuilt, registered=False)
        paths.extend(_differences(approved_operation, rebuilt))
    if paths:
        return Stale(StaleReason.ASSUMPTIONS_CHANGED, tuple(dict.fromkeys(paths)), members)
    return Verified(tuple(Offer(approved[operation], rebuilt) for operation, rebuilt in pairs))


def _authenticate(token, policy):
    """Return ``(envelope, operation)`` for an authentic, current, well-formed token, or a Stale result."""
    invalid = Stale(StaleReason.INVALID, (FIELD,))
    if type(token) is not str or len(token) > MAX_TOKEN_LENGTH:
        return invalid
    prefix, _separator, signed = token.partition(":")
    if prefix not in {resource.value for resource in policy.resources} or not signed:
        return invalid
    resource = Resource(prefix)
    try:
        envelope = signing.loads(signed, salt=_salt(resource), max_age=MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        return Stale(StaleReason.EXPIRED, (FIELD,))
    except (signing.BadSignature, ValueError):
        return invalid
    if (
        not isinstance(envelope, dict)
        or set(envelope) != _ENVELOPE_KEYS
        or type(envelope["v"]) is not int
        or envelope["v"] != ENVELOPE_VERSION
        or envelope["resource"] != resource.value
        or type(envelope["action"]) is not str
        or policy.role(resource, envelope["action"]) is None
        or not _is_context_shape(envelope)
    ):
        return invalid
    try:
        operation = _decode(envelope["operation"], _ACTIONS[(resource, envelope["action"])])
    except (TypeError, ValueError):
        return invalid
    return envelope, operation


def _is_context_shape(envelope):
    """Return whether the context fields of an envelope hold exact types: an int actor, a (label, pk) subject, a server."""
    subject = envelope["subject"]
    return (
        type(envelope["actor"]) is int
        and type(subject) is list
        and len(subject) == 2
        and type(subject[0]) is str
        and bool(subject[0])
        and type(subject[1]) is int
        and type(envelope["server"]) is str
        and bool(envelope["server"])
    )


def _context_differences(envelope, context):
    paths = []
    if envelope["actor"] != context.actor_pk:
        paths.append("context.actor")
    if tuple(envelope["subject"]) != context.subject:
        paths.append("context.subject")
    if envelope["server"] != context.server_key:
        paths.append("context.server")
    return tuple(paths)


def _differences(approved, rebuilt):
    """Return the dotted names of the parts where *rebuilt* differs from *approved*, in schema order."""
    paths = []
    for part in _PARTS:
        paths.extend(_part_differences(getattr(approved, part), getattr(rebuilt, part), part))
    if not paths and type(approved) is not type(rebuilt):
        paths.append("action")
    return paths


def _part_differences(approved, rebuilt, path):
    if _is_frozen_dataclass(type(approved)) and type(approved) is type(rebuilt):
        paths = []
        for field in dataclasses.fields(approved):
            paths.extend(
                _part_differences(getattr(approved, field.name), getattr(rebuilt, field.name), f"{path}.{field.name}")
            )
        return paths
    return [] if type(approved) is type(rebuilt) and approved == rebuilt else [path]


def _action_of(operation, *, registered=True):
    cls = type(operation)
    key = (getattr(cls, "RESOURCE", None), getattr(cls, "ACTION", None))
    if registered and _ACTIONS.get(key) is not cls:
        raise TypeError(f"{cls.__name__} is not a registered operation.")
    if not registered and not all(hasattr(operation, part) for part in _PARTS):
        raise TypeError(f"rebuild() returned {cls.__name__}, which is not an operation.")
    return key


def _salt(resource):
    return f"{SALT_NAMESPACE}:{resource.value}"


def _is_frozen_dataclass(cls):
    return isinstance(cls, type) and dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen


@functools.cache
def _field_hints(cls):
    """Return the resolved type of each dataclass field of *cls*, in field order."""
    hints = typing.get_type_hints(cls)
    return {field.name: hints[field.name] for field in dataclasses.fields(cls)}


def _optional_member(hint):
    """Return X for an ``X | None`` hint, else None."""
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        members = [member for member in typing.get_args(hint) if member is not type(None)]
        if len(members) == 1 and len(typing.get_args(hint)) == 2:
            return members[0]
    return None


def _check_schema(hint):
    """Raise TypeError unless *hint* is a supported exact type: int, str, bool, X | None or a frozen dataclass."""
    if hint in (int, str, bool):
        return
    if (member := _optional_member(hint)) is not None:
        _check_schema(member)
        return
    if not _is_frozen_dataclass(hint):
        raise TypeError(f"Unsupported operation field type {hint!r}.")
    for field_hint in _field_hints(hint).values():
        _check_schema(field_hint)


def _encode(value, hint):
    """Return the JSON value of *value*, raising TypeError when it does not match *hint* exactly."""
    if hint in (int, str, bool):
        if type(value) is not hint:
            raise TypeError(f"Expected {hint.__name__}, got {type(value).__name__}.")
        return value
    if (member := _optional_member(hint)) is not None:
        return None if value is None else _encode(value, member)
    if type(value) is not hint:
        raise TypeError(f"Expected {hint.__name__}, got {type(value).__name__}.")
    return {name: _encode(getattr(value, name), field_hint) for name, field_hint in _field_hints(hint).items()}


def _decode(value, hint):
    """Return the *hint* value of a JSON value, raising TypeError or ValueError for any other shape."""
    if hint in (int, str, bool):
        if type(value) is not hint:
            raise TypeError(f"Expected {hint.__name__}.")
        return value
    if (member := _optional_member(hint)) is not None:
        return None if value is None else _decode(value, member)
    hints = _field_hints(hint)
    if not isinstance(value, dict) or set(value) != set(hints):
        raise ValueError(f"Expected the fields of {hint.__name__}.")
    return hint(**{name: _decode(value[name], field_hint) for name, field_hint in hints.items()})
