"""
Assess the mapping lifecycle operations of a NetBox device or VM on one server, and offer them.

Link stores the LibreNMS device that the object matches. Unlink, relink and change_oob remove or
replace a host ID, and clear that server's port IDs on the interfaces of the object's scope.
"""

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.matching import (
    CANDIDATE_PAIRS,
    Candidate,
    Match,
    NoMatch,
    NotChecked,
    Unreachable,
    find_librenms_match,
)
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    ContainerStatus,
    ServerState,
    device_identity_owner,
    host_change_scope,
    host_entry,
    read_mapping,
    release_server,
    stored_entry_digest,
)
from netbox_librenms_plugin.server_selection import get_librenms_sync_device

# The stored forms on which a link can write an own ID; matching proposes nothing on any other.
_LINKABLE = frozenset({ContainerStatus.ABSENT, ContainerStatus.SCOPED})


class LinkState(StrEnum):
    """What the assessment found for one object on one server."""

    LINKED = "linked"
    REFUSED = "refused"
    MATCH = "match"
    OWNED = "owned"
    NO_MATCH = "no_match"
    UNREACHABLE = "unreachable"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class LinkIdentity:
    """The object, and the server whose own ID the link sets."""

    model: str
    pk: int
    server: str


@dataclass(frozen=True)
class LinkChoices:
    """The LibreNMS device that the operator confirms, and the lookup candidate that matched it."""

    librenms_id: int
    matched_by: str


@dataclass(frozen=True)
class LinkPrimaryIP:
    """A primary IP address that a probe sent."""

    pk: int
    address: str


@dataclass(frozen=True)
class LinkInputs:
    """Both primary IPs, which every lookup asks for, and the later inputs up to and including the matched one."""

    primary_ip4: LinkPrimaryIP | None
    primary_ip6: LinkPrimaryIP | None
    dns_name4: str | None
    dns_name6: str | None
    name: str | None


@dataclass(frozen=True)
class LinkChassis:
    """The virtual chassis of a Device, and whether the Device is its sync device on the server."""

    pk: int
    sync_device: bool


@dataclass(frozen=True)
class LinkAssumptions:
    """The stored mapping and ownership the link writes over, the chassis, and the inputs that made the match."""

    linkable: bool
    own_id: int | None
    migrated: bool
    owned: bool
    chassis: LinkChassis | None
    inputs: LinkInputs


@dataclass(frozen=True)
class LinkEffects:
    """The own LibreNMS ID that the link stores on the server."""

    own_id: int


@intent.register
@dataclass(frozen=True)
class Link:
    """Store the matched LibreNMS ID as the own ID of the object on the server."""

    RESOURCE = intent.Resource.MAPPING_LIFECYCLE
    ACTION = "link"

    identity: LinkIdentity
    choices: LinkChoices
    assumptions: LinkAssumptions
    effects: LinkEffects

    def claims(self):
        """Take the own-ID slot of the object on the server, and the LibreNMS ID in the device space."""
        identity = self.identity
        return (
            intent.Claim(f"mapping.own:{identity.model}:{identity.pk}:{identity.server}", True),
            intent.Claim(f"mapping.identity:{identity.server}:device:{self.effects.own_id}", True),
        )


def bound_inputs(inputs, matched_by):
    """Return the lookup inputs that a match by *matched_by* read: both primary IPs, and every later input to its pair."""
    order = list(Candidate)
    reached = max(order.index(candidate) for candidate in CANDIDATE_PAIRS[Candidate(matched_by)])

    def read(candidate, value):
        return value if reached >= order.index(candidate) else None

    ip4, ip6 = inputs.primary_ip4, inputs.primary_ip6
    return LinkInputs(
        primary_ip4=LinkPrimaryIP(ip4.pk, ip4.address) if ip4 is not None else None,
        primary_ip6=LinkPrimaryIP(ip6.pk, ip6.address) if ip6 is not None else None,
        dns_name4=read(Candidate.DNS_NAME4, ip4.dns_name if ip4 is not None else None),
        dns_name6=read(Candidate.DNS_NAME6, ip6.dns_name if ip6 is not None else None),
        name=read(Candidate.NAME, inputs.name),
    )


def _chassis(obj, server_key):
    """Return the chassis of *obj* and whether *obj* is its sync device on the server; None outside a chassis."""
    chassis_id = getattr(obj, "virtual_chassis_id", None)
    if chassis_id is None:
        return None
    sync_device = get_librenms_sync_device(obj, server_key=server_key)
    return LinkChassis(chassis_id, sync_device is not None and sync_device.pk == obj.pk)


def build_link(obj, server_key, *, choices, inputs, owned):
    """
    Return the link operation of *obj* for the confirmed *choices*, from the state that the caller read.

    The assessment and the rebuild both call this, so an operation compares equal only when the
    stored mapping, the ownership of the ID, the chassis sync device and the lookup inputs are the same.

    Args:
        obj (Device | VirtualMachine): The object, as the caller read it.
        server_key (str): The server.
        choices (LinkChoices): The matched LibreNMS ID and its candidate.
        inputs (LookupInputs): The lookup inputs of *obj*.
        owned (bool): Whether another object owns the ID on the server.

    """
    mapping = read_mapping(obj)
    return Link(
        identity=LinkIdentity(obj._meta.label_lower, obj.pk, server_key),
        choices=choices,
        assumptions=LinkAssumptions(
            linkable=mapping.container in _LINKABLE,
            own_id=mapping.own_id(server_key),
            migrated=mapping.migrated_to(server_key) is not None,
            owned=owned,
            chassis=_chassis(obj, server_key),
            inputs=bound_inputs(inputs, choices.matched_by),
        ),
        effects=LinkEffects(choices.librenms_id),
    )


@dataclass(frozen=True)
class LinkAssessment:
    """The link state of one object on one server, the match, the owner of a matched ID, and the offer."""

    state: LinkState
    server_key: str
    match: Match | None = None
    owner: object = None
    offer: intent.Offer | None = None


_UNMATCHED_STATES = {NoMatch: LinkState.NO_MATCH, Unreachable: LinkState.UNREACHABLE, NotChecked: LinkState.NOT_CHECKED}


def link_state_without_matching(obj, server_key):
    """Return LINKED or REFUSED when the stored mapping decides the state, else None; this sends no request."""
    mapping = read_mapping(obj)
    if mapping.own_id(server_key) is not None:
        return LinkState.LINKED
    if mapping.container not in _LINKABLE or mapping.migrated_to(server_key) is not None:
        return LinkState.REFUSED
    return None


def read_link_assessment(obj, server, context, *, deadline=None):
    """
    Match *obj* on the server of the client *server* and offer the link of a match that no object owns.

    It writes nothing. An object with an own ID, or whose stored value or merge marker refuses a link,
    gets no match.

    Args:
        obj (Device | VirtualMachine): The object.
        server (LibreNMSAPI): A client bound to the server.
        context (IntentContext | None): The context of the offer; None offers nothing.
        deadline (matching.Deadline | None): The deadline of the lookup.

    Returns:
        LinkAssessment: The state, and for a match its offer.

    """
    server_key = server.server_key
    if (state := link_state_without_matching(obj, server_key)) is not None:
        return LinkAssessment(state, server_key)
    result = find_librenms_match(obj, server, deadline=deadline)
    if not isinstance(result, Match):
        return LinkAssessment(_UNMATCHED_STATES[type(result)], server_key)
    try:
        owner = device_identity_owner(server_key, result.librenms_id, exclude_model=type(obj), exclude_pk=obj.pk)
    except AmbiguousLibreNMSIdError:
        # Several objects hold the ID already; none of them is named.
        return LinkAssessment(LinkState.OWNED, server_key, match=result)
    if owner is not None:
        return LinkAssessment(LinkState.OWNED, server_key, match=result, owner=owner)
    operation = build_link(
        obj,
        server_key,
        choices=LinkChoices(result.librenms_id, result.candidate.value),
        inputs=result.inputs,
        owned=False,
    )
    # Only the chassis sync device links a chassis; another member can get its link while the probe runs.
    chassis = operation.assumptions.chassis
    offered = context is not None and (chassis is None or chassis.sync_device)
    return LinkAssessment(
        LinkState.MATCH,
        server_key,
        match=result,
        offer=intent.offer(operation, context) if offered else None,
    )


# Host changes: unlink, relink and change_oob.


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
class UnlinkChoices:
    """Unlink has no operator choice: it removes the whole entry."""


@dataclass(frozen=True)
class UnlinkEffects:
    """The interface entries that the unlink clears, and whether it removes the preference for the server."""

    interfaces: SetBinding
    clears_preference: bool


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
class Unlink:
    """Remove the whole entry of the object on the server, and clear the server's port IDs in its scope."""

    RESOURCE = intent.Resource.MAPPING_LIFECYCLE
    ACTION = "unlink"

    identity: HostChangeIdentity
    choices: UnlinkChoices
    assumptions: HostChangeAssumptions
    effects: UnlinkEffects

    def claims(self):
        """Take the entry of the object on the server."""
        return (_entry_claim(self.identity),)

    @staticmethod
    def target(entry, choices):
        """Return the entry after the change: none."""
        return None

    @staticmethod
    def claimed_id(choices):
        """Return the ID that the change stores: none."""
        return None


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


HOST_CHANGES = {operation.ACTION: operation for operation in (Unlink, Relink, ChangeOOB)}


def _text(value):
    return value if value is None or isinstance(value, str) else str(value)


def build_host_change(cls, obj, server_key, choices, *, owned=False, lock=False):
    """
    Return the host change operation of *obj* for the signed *choices*, and its interface scope.

    The assessment and the rebuild both call this, so an operation compares equal only when the
    stored entry, the chassis membership, the refused interface values and the cascade are the
    same. With *lock*, the caller locked the owner rows, and the scope's interface rows are locked.

    Args:
        cls (type): ``Unlink``, ``Relink`` or ``ChangeOOB``.
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
    if cls is Unlink:
        # The write removes a preference that names the server, and a container with no server entry left.
        clears_preference = (
            release_server(obj, server_key, clear_preference=False).after.preference != mapping.preference
        )
        operation = Unlink(
            identity, choices, HostChangeAssumptions(**fields), UnlinkEffects(cascade, clears_preference)
        )
    elif cls is Relink:
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
    Return ``("mixed", None)`` when they replace or clear both IDs, or clear one while they change the
    other: each of those is its own confirmation.
    """
    own_changes = entry.own_id is not None and own_id != entry.own_id
    oob_changes = entry.oob_id is not None and oob_id != entry.oob_id
    if own_id is None and oob_id is None:
        return (Unlink.ACTION, UnlinkChoices()) if entry.own_id is not None or entry.oob_id is not None else None
    if own_changes and (oob_changes or oob_id != entry.oob_id):
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
        action (str): ``unlink``, ``relink`` or ``change_oob``.
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
