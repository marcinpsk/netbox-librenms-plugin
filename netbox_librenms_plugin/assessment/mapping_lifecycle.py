"""Assess the link of a NetBox device or VM to the LibreNMS device that it matches, and offer the link."""

from dataclasses import dataclass
from enum import StrEnum

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.matching import (
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
    device_identity_owner,
    read_mapping,
)

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
    """The primary IP address that the first probe sent."""

    pk: int
    address: str


@dataclass(frozen=True)
class LinkInputs:
    """The lookup inputs up to and including the matched candidate; a later input is not read (None)."""

    primary_ip: LinkPrimaryIP | None
    dns_name: str | None
    name: str | None


@dataclass(frozen=True)
class LinkAssumptions:
    """The stored mapping and ownership the link writes over, and the inputs that made the match."""

    linkable: bool
    own_id: int | None
    migrated: bool
    owned: bool
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
    """Return the lookup inputs that a match by *matched_by* read: every input up to and including it."""
    order = list(Candidate)
    reached = order.index(Candidate(matched_by))
    primary_ip = None
    if inputs.primary_ip is not None and inputs.primary_address is not None:
        primary_ip = LinkPrimaryIP(inputs.primary_ip, inputs.primary_address)
    return LinkInputs(
        primary_ip=primary_ip,
        dns_name=inputs.dns_name if reached >= order.index(Candidate.DNS_NAME) else None,
        name=inputs.name if reached >= order.index(Candidate.NAME) else None,
    )


def build_link(obj, server_key, *, choices, inputs, owned):
    """
    Return the link operation of *obj* for the confirmed *choices*, from the state that the caller read.

    The assessment and the rebuild both call this, so an operation compares equal only when the
    stored mapping, the ownership of the ID and the lookup inputs are the same.

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
    return LinkAssessment(
        LinkState.MATCH,
        server_key,
        match=result,
        offer=intent.offer(operation, context) if context is not None else None,
    )
