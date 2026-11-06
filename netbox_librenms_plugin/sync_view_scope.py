"""
Decide what one actor may see on the sync tabs and the sync page.

The source snapshots are shared by every user, so this module applies the actor's view scope after
the cache: assessment, the tab views, the verifies and the writers ask it, and tables and templates
render only the projections it returns. A NetBox object the actor cannot view leaves this module
only as :class:`Withheld`, with no pk and no name.
"""

from collections import defaultdict
from dataclasses import dataclass
from ipaddress import ip_interface

from dcim.models import Device, Interface
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import BadRequest
from django.db.models import Q
from django.http import Http404
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.librenms_ids import normalize_librenms_port_id
from netbox_librenms_plugin.server_mappings import MappingRole, identity_q
from netbox_librenms_plugin.utils import (
    get_virtual_chassis_members,
    index_ip_source_interfaces,
    resolve_ip_source_interface,
)

VRF_UNAVAILABLE = "Selected VRF is no longer available or you do not have permission to view it."


class ServerKeyRequired(Exception):
    """A binding-dependent operation ran on a scope that has no validated server key."""


@dataclass(frozen=True)
class Visible:
    """An object the actor may view."""

    obj: object


@dataclass(frozen=True)
class Withheld:
    """An object the actor may not view, or one that does not exist: the two look the same."""


WITHHELD = Withheld()


@dataclass(frozen=True)
class Unresolved:
    """No interface of the chassis stands for the LibreNMS port."""


UNRESOLVED = Unresolved()


@dataclass(frozen=True)
class VisibleInterface:
    """An interface that the actor may view, on an owner that the actor may view."""

    interface: object
    owner: object


@dataclass(frozen=True)
class AddressState:
    """
    The NetBox addresses that equal one address (or one host in one VRF), as one actor may use them.

    ``blocked`` is True when an equal address is hidden, sits in a hidden VRF, or is assigned to an
    interface or owner that the actor cannot view. A blocked address offers no write.
    """

    visible: tuple
    blocked: bool
    # True when the only block is a visible address on an interface or owner that the actor cannot view.
    held_on_hidden_interface: bool = False


@dataclass(frozen=True)
class VRFMatch:
    """What NetBox holds for one LibreNMS VRF identity, as one actor may use it."""

    # The one matching VRF, only when the actor may view it.
    suggested: object
    matched_by: str | None
    # A VRF holds the route distinguisher or the name; a hidden VRF counts too.
    blocks_create: bool


def match_vrf_identities(identities, netbox_vrfs):
    """
    Match each LibreNMS VRF identity to one of *netbox_vrfs*: by route distinguisher, then by name.

    A value that two NetBox VRFs claim matches neither. Any VRF with the identity's route
    distinguisher or name blocks a create. The result is not disclosed yet: see
    :meth:`SyncViewScope.vrf_matches`.

    Args:
        identities (dict): ``{"name", "rd"}`` identities keyed by the caller.
        netbox_vrfs (Iterable[VRF]): The NetBox VRFs to match against.

    Returns:
        dict[object, VRFMatch]: The match of each identity, with the caller's keys.

    """
    netbox_by_rd = defaultdict(list)
    netbox_by_name = defaultdict(list)
    for netbox_vrf in netbox_vrfs:
        if netbox_vrf.rd:
            netbox_by_rd[str(netbox_vrf.rd)].append(netbox_vrf)
        netbox_by_name[netbox_vrf.name].append(netbox_vrf)
    unique_by_rd = {rd: found[0] for rd, found in netbox_by_rd.items() if len(found) == 1}
    unique_by_name = {name: found[0] for name, found in netbox_by_name.items() if len(found) == 1}

    matches = {}
    for key, identity in identities.items():
        target = unique_by_rd.get(identity["rd"]) if isinstance(identity["rd"], str) else None
        matched_by = "route distinguisher"
        if target is None:
            target = unique_by_name.get(identity["name"])
            matched_by = "name"
            # A name match must not cross into another routing domain.
            if target is not None and identity["rd"] and target.rd and str(target.rd) != identity["rd"]:
                target = None
        matches[key] = VRFMatch(
            suggested=target,
            matched_by=matched_by if target is not None else None,
            # Any VRF with the RD or the name, an ambiguous one too, rules out a create.
            blocks_create=target is not None or identity["rd"] in netbox_by_rd or identity["name"] in netbox_by_name,
        )
    return matches


def parse_vrf_id(vrf_id):
    """
    Return the VRF pk that a posted or signed *vrf_id* names, or None for Global (None or "").

    Raises:
        BadRequest: The id is not an integer or a string of one; a bool or a float is malformed too.

    """
    if vrf_id is None or vrf_id == "":
        return None
    if isinstance(vrf_id, bool) or not isinstance(vrf_id, (int, str)):
        raise BadRequest("Invalid VRF ID")
    try:
        return int(vrf_id)
    except ValueError:
        raise BadRequest("Invalid VRF ID") from None


def address_key(address):
    """Return the one key form of an address with its prefix length, as the IP rows use it."""
    return str(ip_interface(str(address)))


@dataclass(frozen=True)
class ChassisScope:
    """
    The members of the subject's virtual chassis, split by what the actor may view.

    ``attribution_members`` holds every member, for owner attribution and matching only: a hidden
    owner must stay an owner, or its rows would fall back to a visible member. Never render it.
    """

    attribution_members: tuple
    visible: tuple
    visible_ids: frozenset

    def owner(self, pk):
        """Return the member *pk* as :class:`Visible`, or :data:`WITHHELD`."""
        for member in self.visible:
            if member.pk == pk:
                return Visible(member)
        return WITHHELD


class SyncViewScope:
    """The view scope of one actor on one sync subject (a Device or a VirtualMachine)."""

    def __init__(self, actor, subject, server_key=None):
        if not isinstance(subject, (Device, VirtualMachine)):
            raise TypeError(f"A sync subject is a Device or a VirtualMachine, not {type(subject).__name__}.")
        self.actor = actor
        self.subject = subject
        self._server_key = server_key
        self._chassis = None

    @classmethod
    def for_request(cls, actor, subject, server_key=None):
        """Return the scope of *actor* on *subject*, which the caller resolved through its view scope."""
        return cls(actor, subject, server_key)

    @property
    def server_key(self):
        """Return the validated server key, for the operations that resolve LibreNMS bindings."""
        if self._server_key is None:
            raise ServerKeyRequired("This operation needs the validated LibreNMS server key.")
        return self._server_key

    @property
    def chassis(self):
        """Return the subject's :class:`ChassisScope`; a VM or a standalone device is its one member."""
        if self._chassis is None:
            if isinstance(self.subject, VirtualMachine):
                members = (self.subject,)
                visible = members
            else:
                members = tuple(get_virtual_chassis_members(self.subject))
                visible_ids = set(
                    Device.objects.restrict(self.actor, "view")
                    .filter(pk__in=[member.pk for member in members])
                    .values_list("pk", flat=True)
                )
                visible = tuple(member for member in members if member.pk in visible_ids)
            self._chassis = ChassisScope(
                attribution_members=members,
                visible=visible,
                visible_ids=frozenset(member.pk for member in visible),
            )
        return self._chassis

    def remote_members(self, chassis_ids):
        """
        Return ``{chassis id: ChassisScope}`` for the chassis at the far ends of the subject's cables.

        A far-end port is attributed to any member of its chassis, so each scope holds every member
        for attribution; a far-end member identity is disclosed only through its ``owner(pk)``.
        """
        members_by_chassis = {chassis_id: [] for chassis_id in chassis_ids}
        for member in Device.objects.filter(virtual_chassis_id__in=members_by_chassis):
            members_by_chassis[member.virtual_chassis_id].append(member)
        visible_ids = set(
            Device.objects.restrict(self.actor, "view")
            .filter(pk__in=[member.pk for members in members_by_chassis.values() for member in members])
            .values_list("pk", flat=True)
        )
        scopes = {}
        for chassis_id, members in members_by_chassis.items():
            visible = tuple(member for member in members if member.pk in visible_ids)
            scopes[chassis_id] = ChassisScope(
                attribution_members=tuple(members),
                visible=visible,
                visible_ids=frozenset(member.pk for member in visible),
            )
        return scopes

    def owner(self, model, pk):
        """
        Return the *model* object *pk* as :class:`Visible`, or :data:`WITHHELD` when the actor may not view it.

        The lookup stays in *model*: a Device pk never resolves a VirtualMachine.
        """
        if model not in (Device, VirtualMachine):
            raise TypeError(f"An owner is a Device or a VirtualMachine, not {getattr(model, '__name__', model)}.")
        if pk is None:
            return WITHHELD
        obj = model.objects.restrict(self.actor, "view").filter(pk=pk).first()
        return Visible(obj) if obj is not None else WITHHELD

    def interfaces(self):
        """
        Return the view-restricted interfaces on the visible members, for lists and indexes that are shown.

        Resolution of a LibreNMS port never uses this list: a port bound on a hidden interface must
        resolve to that interface and then be withheld, not fall back by name to a visible one.
        """
        if isinstance(self.subject, VirtualMachine):
            return list(VMInterface.objects.restrict(self.actor, "view").filter(virtual_machine=self.subject))
        return list(Interface.objects.restrict(self.actor, "view").filter(device__in=self.chassis.visible))

    def resolve_ip_interfaces(self, rows):
        """
        Resolve the LibreNMS port of each row to an interface of the chassis, then disclose it.

        The binding and the name rule run over the interfaces of every member, so a port bound on a
        hidden interface or owner gives :data:`WITHHELD`, never a visible namesake.

        Args:
            rows (Iterable[dict]): Rows with a ``port_id`` and the LibreNMS ``interface_name``.

        Returns:
            list: One :class:`VisibleInterface`, :data:`WITHHELD` or :data:`UNRESOLVED` per row.

        Raises:
            ServerKeyRequired: The scope has no validated server key.

        """
        server_key = self.server_key
        rows = list(rows)
        if isinstance(self.subject, VirtualMachine):
            model, owner_field, subject_device_id = VMInterface, "virtual_machine_id", None
        else:
            model, owner_field, subject_device_id = Interface, "device_id", self.subject.pk
        port_ids = {normalize_librenms_port_id(row.get("port_id")) for row in rows} - {None}
        names = {name for row in rows if isinstance(name := row.get("interface_name"), str)}
        candidates = model.objects.filter(
            **{f"{owner_field}__in": [member.pk for member in self.chassis.attribution_members]}
        ).filter(
            identity_q(model, server=server_key, identities=port_ids, roles=(MappingRole.OWN,)) | Q(name__in=names)
        )
        by_port_id, by_name = index_ip_source_interfaces(candidates, server_key, subject_device_id)
        resolved = [resolve_ip_source_interface(row, by_port_id, by_name, server_key=server_key) for row in rows]
        viewable = set(
            model.objects.restrict(self.actor, "view")
            .filter(pk__in={interface.pk for interface in resolved if interface is not None})
            .values_list("pk", flat=True)
        )
        owners = {member.pk: member for member in self.chassis.visible}
        disclosed = []
        for interface in resolved:
            if interface is None:
                disclosed.append(UNRESOLVED)
            elif interface.pk in viewable and getattr(interface, owner_field) in owners:
                disclosed.append(VisibleInterface(interface, owners[getattr(interface, owner_field)]))
            else:
                disclosed.append(WITHHELD)
        return disclosed

    def resolve_ip_interface(self, row):
        """Return :meth:`resolve_ip_interfaces` for one row."""
        return self.resolve_ip_interfaces([row])[0]

    def address_states(self, addresses):
        """
        Return the :class:`AddressState` of each address, keyed by :func:`address_key`.

        Args:
            addresses (Iterable): Addresses with their prefix length.

        Returns:
            dict[str, AddressState]: The state of each address.

        """
        keys = {address_key(address) for address in addresses}
        if not keys:
            return {}
        every_pk = defaultdict(set)
        # The unscoped read gives only pks: a hidden row adds the block and nothing else.
        for pk, address in IPAddress.objects.filter(address__in=list(keys)).values_list("pk", "address"):
            every_pk[address_key(address)].add(pk)
        visible = defaultdict(list)
        for ip in (
            IPAddress.objects.restrict(self.actor, "view")
            .filter(address__in=list(keys))
            .select_related("vrf")
            .order_by("pk")
        ):
            visible[address_key(ip.address)].append(ip)
        return self._states({key: (visible[key], every_pk[key]) for key in keys})

    def host_conflicts(self, host, vrf):
        """Return the :class:`AddressState` of every NetBox address of *host* in *vrf* (None is Global)."""
        rows = IPAddress.objects.filter(address__net_host=str(host), vrf=vrf)
        every_pk = set(rows.values_list("pk", flat=True))
        visible = list(
            IPAddress.objects.restrict(self.actor, "view")
            .filter(address__net_host=str(host), vrf=vrf)
            .select_related("vrf")
            .order_by("pk")
        )
        return self._states({None: (visible, every_pk)})[None]

    def _states(self, rows_by_key):
        """Return an :class:`AddressState` for each ``(visible, every_pk)`` pair."""
        visible_ips = [ip for visible, _every_pk in rows_by_key.values() for ip in visible]
        in_hidden_vrf = self._in_hidden_vrf(visible_ips)
        on_hidden_holder = self._on_hidden_holder(visible_ips)
        states = {}
        for key, (visible, every_pk) in rows_by_key.items():
            hidden = bool(every_pk - {ip.pk for ip in visible}) or any(ip.pk in in_hidden_vrf for ip in visible)
            held = any(ip.pk in on_hidden_holder for ip in visible)
            states[key] = AddressState(
                visible=tuple(visible), blocked=hidden or held, held_on_hidden_interface=held and not hidden
            )
        return states

    def _in_hidden_vrf(self, ips):
        """Return the pks of the addresses in a VRF that the actor cannot view."""
        vrf_ids = {ip.vrf_id for ip in ips if ip.vrf_id is not None}
        if not vrf_ids:
            return set()
        viewable = set(VRF.objects.restrict(self.actor, "view").filter(pk__in=vrf_ids).values_list("pk", flat=True))
        return {ip.pk for ip in ips if ip.vrf_id is not None and ip.vrf_id not in viewable}

    def _on_hidden_holder(self, ips):
        """Return the pks of the addresses assigned to an object, or an interface owner, that the actor cannot view."""
        holders = defaultdict(set)
        for ip in ips:
            if ip.assigned_object_id is not None:
                holders[ip.assigned_object_type_id].add(ip.assigned_object_id)
        viewable = set()
        for type_id, pks in holders.items():
            model = ContentType.objects.get_for_id(type_id).model_class()
            if model is not None:
                viewable.update((type_id, pk) for pk in self._viewable_holders(model, pks))
        return {
            ip.pk
            for ip in ips
            if ip.assigned_object_id is not None and (ip.assigned_object_type_id, ip.assigned_object_id) not in viewable
        }

    def _viewable_holders(self, model, pks):
        """Return the pks of the *model* objects the actor may view; an interface also needs a viewable owner."""
        queryset = model.objects.restrict(self.actor, "view").filter(pk__in=pks)
        owner = {Interface: (Device, "device_id"), VMInterface: (VirtualMachine, "virtual_machine_id")}.get(model)
        if owner is None:
            return set(queryset.values_list("pk", flat=True))
        owner_model, owner_field = owner
        rows = list(queryset.values_list("pk", owner_field))
        viewable_owners = set(
            owner_model.objects.restrict(self.actor, "view")
            .filter(pk__in={owner_pk for _pk, owner_pk in rows})
            .values_list("pk", flat=True)
        )
        return {pk for pk, owner_pk in rows if owner_pk in viewable_owners}

    def vrf(self, vrf_id):
        """
        Return the VRF *vrf_id* names, or None for Global (an empty id).

        Raises:
            BadRequest: The id is malformed (see :func:`parse_vrf_id`).
            Http404: The VRF does not exist or the actor may not view it; both give the same answer.

        """
        pk = parse_vrf_id(vrf_id)
        if pk is None:
            return None
        vrf = VRF.objects.restrict(self.actor, "view").filter(pk=pk).first()
        if vrf is None:
            raise Http404(VRF_UNAVAILABLE)
        return vrf

    def vrf_matches(self, identities):
        """
        Match LibreNMS VRF identities to NetBox VRFs with :func:`match_vrf_identities`, then disclose the matches.

        This is the one unscoped VRF read: every VRF decides the match and the create block, so a
        hidden VRF is never passed over for a visible one. Only a viewable match leaves this method.

        Args:
            identities (dict): ``{"name", "rd"}`` identities keyed by the caller.

        Returns:
            dict[object, VRFMatch]: The match of each identity, with the caller's keys.

        """
        if not identities:
            return {}
        rds = {identity["rd"] for identity in identities.values() if identity["rd"]}
        names = {identity["name"] for identity in identities.values()}
        matches = match_vrf_identities(identities, VRF.objects.filter(Q(rd__in=rds) | Q(name__in=names)))
        viewable = set(
            VRF.objects.restrict(self.actor, "view")
            .filter(pk__in={match.suggested.pk for match in matches.values() if match.suggested is not None})
            .values_list("pk", flat=True)
        )
        return {
            key: match
            if match.suggested is None or match.suggested.pk in viewable
            else VRFMatch(suggested=None, matched_by=None, blocks_create=True)
            for key, match in matches.items()
        }

    def movable_ips(self):
        """
        Return the migration move candidates: viewable addresses on the subject's own viewable interfaces.

        Only Device interfaces are listed, as the move endpoint re-homes an address to the winner's
        interface of the same name.
        """
        if not isinstance(self.subject, Device):
            return []
        names = {
            interface.pk: interface.name
            for interface in Interface.objects.restrict(self.actor, "view").filter(device=self.subject)
        }
        if not names:
            return []
        addresses = (
            IPAddress.objects.restrict(self.actor, "view")
            .filter(
                assigned_object_type=ContentType.objects.get_for_model(Interface), assigned_object_id__in=list(names)
            )
            .order_by("address")
        )
        return [
            {"id": ip.pk, "address": str(ip.address), "interface_name": names[ip.assigned_object_id]}
            for ip in addresses
        ]
