"""Assess the host changes of a NetBox device or VM on one server, and offer their interface cascade."""

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    ContainerStatus,
    ServerState,
    device_identity_owner,
    host_change_scope,
    host_entry,
    read_mapping,
    stored_entry_digest,
)

_LINKABLE = frozenset({ContainerStatus.ABSENT, ContainerStatus.SCOPED})


@dataclass(frozen=True)
class HostChangeIdentity:
    """The object, and the server whose entry the change replaces."""

    model: str
    pk: int
    server: str


@dataclass(frozen=True)
class HostEntryBinding:
    """The own ID and the OOB controller that the object holds on the server before the change, and its stored digest."""

    own_id: int | None
    oob_id: int | None
    oob_type: str | None
    digest: str


@dataclass(frozen=True)
class SetBinding:
    """A set of rows, bound by its size and the SHA-256 digest of its canonical sorted list."""

    count: int
    digest: str


@dataclass(frozen=True)
class HostChangeAssumptions:
    """The stored entry that the change replaces, the chassis membership, and the refused interface values."""

    scoped: bool
    migrated: bool
    entry: HostEntryBinding
    membership: SetBinding
    refused_interfaces: int


@dataclass(frozen=True)
class ClaimingHostChangeAssumptions(HostChangeAssumptions):
    """The assumptions of a change that stores a new ID, and whether another object owns that ID."""

    owned: bool


@dataclass(frozen=True)
class RelinkChoices:
    """The own ID that replaces the stored one; None clears it."""

    own_id: int | None


@dataclass(frozen=True)
class RelinkEffects:
    """The own ID that the relink stores, and the interface entries that it clears."""

    own_id: int | None
    interfaces: SetBinding


@dataclass(frozen=True)
class ChangeOOBChoices:
    """The OOB controller ID and type that replace the stored ones; a None ID clears them."""

    oob_id: int | None
    oob_type: str | None


@dataclass(frozen=True)
class ChangeOOBEffects:
    """The OOB controller that the change stores, and the interface entries that it clears."""

    oob_id: int | None
    oob_type: str | None
    interfaces: SetBinding


def _entry_claim(identity):
    return intent.Claim(f"mapping.entry:{identity.model}:{identity.pk}:{identity.server}", True)


def _identity_claim(identity, librenms_id):
    return intent.Claim(f"mapping.identity:{identity.server}:device:{librenms_id}", True)


@intent.register
@dataclass(frozen=True)
class Relink:
    """Replace or clear the own ID of the object on the server, and clear the server's port IDs in its scope."""

    RESOURCE = intent.Resource.MAPPING_LIFECYCLE
    ACTION = "relink"

    identity: HostChangeIdentity
    choices: RelinkChoices
    assumptions: ClaimingHostChangeAssumptions
    effects: RelinkEffects

    def claims(self):
        """Take the entry of the object on the server, and the new own ID in the device space."""
        own_id = self.effects.own_id
        return (_entry_claim(self.identity), *((_identity_claim(self.identity, own_id),) if own_id else ()))

    @staticmethod
    def target(entry, choices):
        """Return the entry after the change: the new own ID, and the OOB controller as it is."""
        return ServerState(entry.server, own_id=choices.own_id, oob_id=entry.oob_id, oob_type=entry.oob_type)

    @staticmethod
    def claimed_id(choices):
        """Return the own ID that the change stores, or None."""
        return choices.own_id


@intent.register
@dataclass(frozen=True)
class ChangeOOB:
    """Replace or clear the OOB controller of the object on the server, and clear the server's port IDs in its scope."""

    RESOURCE = intent.Resource.MAPPING_LIFECYCLE
    ACTION = "change_oob"

    identity: HostChangeIdentity
    choices: ChangeOOBChoices
    assumptions: ClaimingHostChangeAssumptions
    effects: ChangeOOBEffects

    def claims(self):
        """Take the entry of the object on the server, and the new OOB controller ID in the device space."""
        oob_id = self.effects.oob_id
        return (_entry_claim(self.identity), *((_identity_claim(self.identity, oob_id),) if oob_id else ()))

    @staticmethod
    def target(entry, choices):
        """Return the entry after the change: the own ID as it is, and the new OOB controller."""
        oob_type = choices.oob_type if choices.oob_id is not None else None
        return ServerState(entry.server, own_id=entry.own_id, oob_id=choices.oob_id, oob_type=oob_type)

    @staticmethod
    def claimed_id(choices):
        """Return the OOB controller ID that the change stores, or None."""
        return choices.oob_id


HOST_CHANGES = {operation.ACTION: operation for operation in (Relink, ChangeOOB)}


def _text(value):
    return value if value is None or isinstance(value, str) else str(value)


def build_host_change(cls, obj, server_key, choices, *, owned=False, lock=False):
    """
    Return the host change operation of *obj* for the signed *choices*, and its interface scope.

    The assessment and the rebuild both call this, so an operation compares equal only when the
    stored entry, the chassis membership, the refused interface values and the cascade are the
    same. With *lock*, the caller locked the owner rows, and the scope's interface rows are locked.

    Args:
        cls (type): ``Relink`` or ``ChangeOOB``.
        obj (Device | VirtualMachine): The object, as the caller read it.
        server_key (str): The server.
        choices: The choices of *cls*.
        owned (bool): Whether another object owns the ID that the change stores.
        lock (bool): Lock the scope's interface rows.

    Returns:
        tuple: The operation and its ``HostChangeScope``.

    """
    mapping = read_mapping(obj)
    entry = host_entry(obj, server_key)
    stored = mapping.server(server_key)
    scope = host_change_scope(obj, server_key, cls.target(entry, choices), lock=lock)
    fields = {
        "scoped": mapping.container in _LINKABLE,
        "migrated": stored is not None and stored.migration.recorded,
        "entry": HostEntryBinding(
            entry.own_id, entry.oob_id, _text(entry.oob_type), stored_entry_digest(obj, server_key)
        ),
        "membership": SetBinding(len(scope.members), scope.membership_digest),
        "refused_interfaces": len(scope.refused),
    }
    cascade = SetBinding(len(scope.interfaces), scope.digest)
    identity = HostChangeIdentity(obj._meta.label_lower, obj.pk, server_key)
    if cls is Relink:
        assumptions = ClaimingHostChangeAssumptions(**fields, owned=owned)
        operation = Relink(identity, choices, assumptions, RelinkEffects(choices.own_id, cascade))
    else:
        assumptions = ClaimingHostChangeAssumptions(**fields, owned=owned)
        target = cls.target(entry, choices)
        operation = ChangeOOB(identity, choices, assumptions, ChangeOOBEffects(target.oob_id, target.oob_type, cascade))
    return operation, scope


def _permitted(required, user):
    by_model = defaultdict(lambda: defaultdict(set))
    for model, pk, actions in required:
        for action in actions:
            by_model[model][action].add(pk)
    return all(
        model.objects.restrict(user, action).filter(pk__in=pks).count() == len(pks)
        for model, actions in by_model.items()
        for action, pks in actions.items()
    )


def _members_visible(scope, user):
    """Return whether *user* may view each object whose interfaces are the scope."""
    return _permitted([(type(member), member.pk, ("view",)) for member in scope.members], user)


def scope_permitted(scope, user):
    """Return whether *user* may view each object of the scope, and view and change each interface that it clears."""
    required = [(type(member), member.pk, ("view",)) for member in scope.members]
    required += [(type(row.interface), row.interface.pk, ("view", "change")) for row in scope.interfaces]
    return _permitted(required, user)


class HostChangeState(StrEnum):
    """What the assessment of a host change found."""

    OFFERED = "offered"
    UNCHANGED = "unchanged"
    REFUSED = "refused"
    MIGRATED = "migrated"
    INTERFACES = "interfaces"
    SCOPE = "scope"
    OWNED = "owned"


@dataclass(frozen=True)
class HostChangeAssessment:
    """The state of one host change, the entry it replaces, its interface scope, and its offer."""

    state: HostChangeState
    action: str
    server_key: str
    entry: ServerState
    choices: object
    scope: object = None
    owner: object = None
    offer: intent.Offer | None = None


def host_change_action(entry, own_id, oob_id, oob_type):
    """
    Return the host change that turns *entry* into the IDs an operator named, as ``(action, choices)``.

    Return None when the IDs remove or replace no host ID: the editor writes that change itself.
    Return ``("mixed", None)`` when they replace or clear both IDs, clear one while they change the
    other, or replace the own ID with a new OOB type: each of those is its own confirmation.
    """
    own_changes = entry.own_id is not None and own_id != entry.own_id
    oob_changes = entry.oob_id is not None and oob_id != entry.oob_id
    # A relink keeps the stored OOB controller, so a new OOB type with it would be lost.
    if own_changes and (oob_changes or oob_id != entry.oob_id or (oob_type or None) != (entry.oob_type or None)):
        return "mixed", None
    if oob_changes and own_id != entry.own_id:
        return "mixed", None
    if own_changes:
        return Relink.ACTION, RelinkChoices(own_id)
    if oob_changes:
        return ChangeOOB.ACTION, ChangeOOBChoices(oob_id, oob_type if oob_id is not None else None)
    return None


def read_host_change_assessment(obj, server_key, action, choices, *, user, context):
    """
    Assess the host change *action* of *obj* on *server_key* with *choices*, and offer it.

    It writes nothing and locks nothing. The offer needs the stored value to be absent or
    server-scoped, a readable entry and no migration marker on the server, no legacy or invalid
    interface value in the scope, a free ID to store, and the view of every scope object and the
    view and change of every interface that the change clears.

    Args:
        obj (Device | VirtualMachine): The object.
        server_key (str): The server, configured or not.
        action (str): ``relink`` or ``change_oob``.
        choices: The choices of the action.
        user (User): The actor, whose permissions must cover the scope.
        context (IntentContext | None): The context of the offer; None offers nothing.

    Returns:
        HostChangeAssessment: The state, and for an offered change its scope and offer.

    """
    cls = HOST_CHANGES[action]
    entry = host_entry(obj, server_key)
    mapping = read_mapping(obj)
    stored = mapping.server(server_key)

    def answer(state, **extra):
        return HostChangeAssessment(state, action, server_key, entry, choices, **extra)

    # Every other refusal names the object, so an object outside the view scope refuses first.
    if not _permitted([(type(obj), obj.pk, ("view",))], user):
        return answer(HostChangeState.SCOPE)
    if mapping.container not in _LINKABLE or (stored is not None and stored.malformed):
        return answer(HostChangeState.REFUSED)
    if stored is not None and stored.migration.recorded:
        return answer(HostChangeState.MIGRATED)
    if (claimed := cls.claimed_id(choices)) is not None:
        try:
            owner = device_identity_owner(server_key, claimed, exclude_model=type(obj), exclude_pk=obj.pk)
        except AmbiguousLibreNMSIdError:
            return answer(HostChangeState.OWNED)
        if owner is not None:
            return answer(HostChangeState.OWNED, owner=owner)
    operation, scope = build_host_change(cls, obj, server_key, choices)
    if not scope.changes_host:
        return answer(HostChangeState.UNCHANGED)
    # The refusal names the parent of each refused interface, so a hidden member refuses first.
    if not _members_visible(scope, user):
        return answer(HostChangeState.SCOPE)
    if scope.refused:
        return answer(HostChangeState.INTERFACES, scope=scope)
    if not scope_permitted(scope, user):
        return answer(HostChangeState.SCOPE)
    return answer(
        HostChangeState.OFFERED,
        scope=scope,
        offer=intent.offer(operation, context) if context is not None else None,
    )
