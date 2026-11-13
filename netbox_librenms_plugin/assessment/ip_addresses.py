"""Assess IP source facts against the actor's visible NetBox state."""

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_interface

from django.contrib.contenttypes.models import ContentType
from ipam.models import VRF

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import port_vrf_id
from netbox_librenms_plugin.acquisition.snapshot import Outcome
from netbox_librenms_plugin.constants import LIBRENMS_GLOBAL_ROUTING_INSTANCE
from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher, row_rule_block
from netbox_librenms_plugin.ip_rules import IPRuleMatcher, row_ip_rule_block
from netbox_librenms_plugin.ip_addressing import parse_librenms_ip_entry
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id, normalize_librenms_port_id
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.sync_view_scope import WITHHELD, SyncViewScope, VisibleInterface
from netbox_librenms_plugin.utils import (
    identify_ip_sync_rows,
    index_ip_port_records,
    ip_row_port_record,
    PortDisclosure,
    same_host,
)


class ViewBlock(StrEnum):
    """Why an object that the actor cannot view blocks an IP row; the row then carries no NetBox identity."""

    ADDRESS = "address"
    HELD_ON_HIDDEN_INTERFACE = "held_on_hidden_interface"
    INTERFACE = "interface"


def shown_vrf_id(row):
    """Return the VRF that the row's select shows: its NetBox VRF, else its suggested VRF, when offered; None is Global."""
    offered = {vrf.pk for vrf in row.get("vrfs") or ()}
    # The assessment sets vrf_id or suggested_vrf_id, never both.
    return next(
        (pk for pk in (row.get("vrf_id"), row.get("suggested_vrf_id")) if pk is not None and pk in offered), None
    )


def apply_vrf_choices(rows, choices, scope):
    """
    Return the assessed rows, each row of *choices* decided in the VRF that the user chose for it.

    A choice of a VRF that the row no longer offers, or of the VRF that the row shows anyway, leaves
    the row as the assessment decided it. A row that a hidden object blocks keeps its block.

    Args:
        rows (list[dict]): The rows of :func:`assess`, before the web readers convert their objects to URLs.
        choices (dict[str, int | None]): The chosen VRF pk of each row ID; None is Global.
        scope (SyncViewScope): The actor's view scope on the subject.

    Returns:
        list[dict]: The rows; a decided row is a new dict.

    """
    rules = IPRuleMatcher.load()
    decided = []
    for row in rows:
        vrf_id = choices.get(row.get("row_id"), shown_vrf_id(row))
        offered = vrf_id is None or any(vrf.pk == vrf_id for vrf in row.get("vrfs") or ())
        if row.get("view_block") is not None or not offered or vrf_id == shown_vrf_id(row):
            decided.append(row)
        else:
            decided.append(_row_in_vrf(row, vrf_id, scope, rules))
    return decided


def vrf_address_ids(addresses, vrf_id):
    """
    Return the pks of the NetBox addresses equal to a row's address that are in *vrf_id*, in pk order.

    The row's evidence and the writer, under its row locks, both name the addresses that the row acts
    on here, so the writer can refuse a row whose addresses are not the ones the user approved.

    Args:
        addresses (Iterable[IPAddress]): The viewable NetBox addresses equal to the row's address.
        vrf_id (int | None): The VRF of the row; None is Global.

    """
    return sorted(ip.pk for ip in addresses if ip.vrf_id == vrf_id)


def _row_in_vrf(row, vrf_id, scope, rules):
    """
    Return *row* decided in *vrf_id*: the address exists when a viewable NetBox address equals it, and is Synced in *vrf_id*.

    ``netbox_ip`` becomes the one viewable address in *vrf_id*, else the only viewable address, else None.
    """
    key = row["ip_with_mask"]
    visible = sorted(scope.address_states([key])[key].visible, key=lambda ip: ip.pk)
    in_vrf = [ip for ip in visible if ip.vrf_id == vrf_id]
    decided = {
        name: value
        for name, value in row.items()
        if name not in ("vrf_id", "suggested_vrf_id", "status", "ip_rule_block", "exists")
    }
    decided.update(vrf_id=vrf_id, exists=bool(visible), netbox_addresses=visible)
    if visible:
        decided["status"] = "matched" if len(in_vrf) == 1 else "update"
        decided["netbox_ip"] = in_vrf[0] if len(in_vrf) == 1 else visible[0] if len(visible) == 1 else None
    else:
        # The address left NetBox; the VRF it had there still counts as its own.
        decided["status"] = "matched" if row["vrf_id"] is not None and row["vrf_id"] == vrf_id else "sync"
    rule, held = decide_ip_row_rule(rules, key, vrf_id, row.get("netbox_interface"), visible)
    if rule is not None:
        decided["ip_rule_block"] = row_ip_rule_block(rule)
        if held:
            decided["status"] = "matched"
        elif decided["status"] == "matched":
            decided["status"] = "update"
    return decided


def decide_ip_row_rule(rules, ip_with_mask, vrf_id, interface, netbox_addresses):
    """
    Return the IP mapping that ignores a row in *vrf_id*, and whether the ignored row shows Synced.

    The table and a VRF choice both decide a row here. An ignored row shows Synced when NetBox
    holds the address in *vrf_id* on the row's interface, also when another interface holds it too.

    Args:
        rules (IPRuleMatcher): The request's IP mappings.
        ip_with_mask (str): The row's address with its prefix length.
        vrf_id (int | None): The VRF the row's select shows; None is Global.
        interface (Interface | VMInterface | None): The interface the row resolves to.
        netbox_addresses (Iterable[IPAddress]): The viewable NetBox addresses equal to the row's address;
            a blocked address never reaches this rule.

    Returns:
        tuple[IPRule | None, bool]: The ignoring mapping, and whether the row shows Synced.

    """
    rule = rules.decide(ip_interface(ip_with_mask).ip, vrf_id)
    if rule is None or interface is None:
        return rule, False
    interface_type_id = ContentType.objects.get_for_model(interface).pk
    held = any(
        ip.vrf_id == vrf_id
        and ip.assigned_object_type_id == interface_type_id
        and ip.assigned_object_id == interface.pk
        for ip in netbox_addresses
    )
    return rule, held


def _port_key(port_id):
    """Return the one key form the port map uses; LibreNMS types an id as int here and str there."""
    return str(port_id)


def ip_assignment_ports(ports_by_id, bound_ports_by_id, source_port_id, interface, server_key, owner):
    """
    Return the ports an IP assignment to an existing interface touches, for the sync and the table alike.

    The assignment touches the IP row's source port and the port bound to the interface on this
    server. A name match can pick an interface bound to another port, so both are decided, each
    with the record the snapshot holds for it. Decide them with
    ``InterfaceRuleMatcher.first_blocked_port`` and a ``PortDisclosure``.

    Args:
        ports_by_id (dict): The snapshot's records of the ports the IP rows name.
        bound_ports_by_id (dict): The snapshot's records of the ports bound to interfaces in scope.
        source_port_id: The IP row's LibreNMS port id.
        interface (Interface | VMInterface): The target interface (the locked row in the sync).
        server_key (str): The LibreNMS server whose binding counts.
        owner (Device | VirtualMachine): The interface's owner (the locked row in the sync).

    Returns:
        list[tuple]: ``(port_id, record, platform_id, owner)`` for each port.

    """
    port_ids = {
        normalize_librenms_port_id(source_port_id),
        read_mapping(interface).own_id(server_key),
    } - {None}
    return [
        (
            port_id,
            ip_row_port_record(ports_by_id, port_id) or ip_row_port_record(bound_ports_by_id, port_id),
            owner.platform_id,
            owner,
        )
        for port_id in port_ids
    ]


def _management_ip(device_record):
    """Return the device record's management IP, stripped, or "" when it holds no string."""
    ip_value = device_record.get("ip")
    return ip_value.strip() if isinstance(ip_value, str) else ""


def _index_snapshot_ports(ports, ip_data, bound_port_keys):
    """
    Index the snapshot's ports for the IP rows and for the interface rules.

    Only the ports the rows name go into the row map. The sync scans that map to decide whether an
    interface name is ambiguous, so a port no row names must not change that verdict. A named port
    that the snapshot lacks maps to None, and its rows render unnamed.

    Args:
        ports (list[dict]): The snapshot's port rows.
        ip_data (list[dict]): The snapshot's IP rows.
        bound_port_keys (set[str]): The :func:`_port_key` of each port bound to an interface in scope.

    Returns:
        tuple[dict, dict]: The records of the ports the rows name, and the records of the ports bound
            to interfaces in scope, both keyed by :func:`_port_key`.

    """
    wanted = {_port_key(row["port_id"]) for row in ip_data}
    ports_by_id = {}
    bound_ports_by_id = {}
    for port in ports:
        if port.get("port_id") is None:
            continue
        key = _port_key(port["port_id"])
        if key in wanted:
            ports_by_id.setdefault(key, port)
        elif key in bound_port_keys:
            bound_ports_by_id.setdefault(key, port)
    for key in wanted:
        ports_by_id.setdefault(key, None)
    return ports_by_id, bound_ports_by_id


def _vrf_identities(ports_by_id, ip_data, vrf_rows):
    """
    Return the LibreNMS VRF identity of each port the rows name and that LibreNMS tags with a VRF.

    Args:
        ports_by_id (dict): The records of the ports the rows name, keyed by :func:`_port_key`.
        ip_data (list[dict]): The snapshot's IP rows.
        vrf_rows (list[dict] | None): The snapshot's VRF rows; None when no port names a VRF.

    Returns:
        dict: ``{"name", "rd"}`` keyed by :func:`_port_key`.

    """
    tagged_ports = {}
    for row in ip_data:
        port_key = _port_key(row["port_id"])
        port = ports_by_id.get(port_key)
        if port is None:
            continue
        # Acquisition reads the VRFs by the same rule, so a tagged port never meets a skipped VRF read.
        if_vrf = port_vrf_id(port)
        if if_vrf is not None:
            tagged_ports[port_key] = if_vrf
    if not tagged_ports or vrf_rows is None:
        return {}

    librenms_by_id = {}
    for vrf in vrf_rows:
        vrf_id = coerce_librenms_id(vrf.get("vrf_id"))
        vrf_name = vrf.get("vrf_name")
        if vrf_id is None or not isinstance(vrf_name, str) or not vrf_name:
            continue
        if vrf_name == LIBRENMS_GLOBAL_ROUTING_INSTANCE:
            continue
        source_rd = vrf.get("mplsVpnVrfRouteDistinguisher")
        librenms_by_id[vrf_id] = {"name": vrf_name, "rd": source_rd if isinstance(source_rd, str) else None}
    return {port_key: librenms_by_id[vrf_id] for port_key, vrf_id in tagged_ports.items() if vrf_id in librenms_by_id}


@dataclass(frozen=True)
class IPAssessment:
    """The IP rows of one source snapshot, assessed against NetBox for one user."""

    rows: list
    # The records of the ports the rows name; None for a port the snapshot lacks.
    ports_by_id: dict
    # The records of the ports bound to interfaces in scope: evidence for the interface rules only.
    bound_ports_by_id: dict
    mgmt_ip: str
    interface_name_field: str


class _Assessment:
    """Hold the actor and name option for one assessment."""

    def __init__(self, actor, interface_name_field):
        self.actor = actor
        self.interface_name_field = interface_name_field

    def assess(self, obj, snapshot, server_key):
        """
        Assess the snapshot's IP rows against current NetBox state, as the request user may see it.

        The table, the row read and the sync all read the rows through this method, so they agree.
        It reads the snapshot and NetBox only.

        Args:
            obj (Device | VirtualMachine): The object the rows belong to.
            snapshot (SourceSnapshot): A stored snapshot that holds the IP address resource.
            server_key (str): The LibreNMS server key that scopes interface matching.

        Returns:
            IPAssessment: The enriched rows, the port evidence and the management IP.

        """
        resource = Resource.IP_ADDRESSES
        ip_data = snapshot.value(resource, "addresses")
        vrfs_record = snapshot.record(resource, "vrfs")
        vrf_rows = None if vrfs_record.outcome is Outcome.NOT_APPLICABLE else snapshot.value(resource, "vrfs")
        interface_name_field = self.interface_name_field
        mgmt_ip = _management_ip(snapshot.value(resource, "device"))

        scope = SyncViewScope.for_request(self.actor, obj, server_key)
        source_address_counts = defaultdict(int)
        for ip_entry in ip_data:
            source_address_counts[str(parse_librenms_ip_entry(ip_entry))] += 1
        # Only an address LibreNMS reported can match a row, so scope the NetBox IP scan to them.
        address_states = scope.address_states(source_address_counts)
        bound_port_keys = {
            _port_key(port_id)
            for interface in scope.interfaces()
            if (port_id := read_mapping(interface).own_id(server_key)) is not None
        }
        ports_by_id, bound_ports_by_id = _index_snapshot_ports(
            snapshot.value(resource, "ports"), ip_data, bound_port_keys
        )
        # The dropdown lists only VRFs the user may view; identity matching is scoped separately.
        vrfs = list(VRF.objects.restrict(self.actor, "view"))
        rows = self._enrich_rows(
            ip_data,
            obj,
            interface_name_field,
            scope,
            address_states,
            vrfs,
            source_address_counts,
            ports_by_id,
            _vrf_identities(ports_by_id, ip_data, vrf_rows),
        )
        assignments = [(row, target) for row, target in rows if target is not None]
        self._add_rule_blocks(assignments, (ports_by_id, bound_ports_by_id), server_key)
        enriched_data = [row for row, _target in rows]
        self._add_ip_rule_blocks(rows, address_states)
        self._flag_management_ip(enriched_data, mgmt_ip)
        return IPAssessment(
            rows=identify_ip_sync_rows(enriched_data),
            ports_by_id=ports_by_id,
            bound_ports_by_id=bound_ports_by_id,
            mgmt_ip=mgmt_ip,
            interface_name_field=interface_name_field,
        )

    def _enrich_rows(
        self,
        ip_data,
        obj,
        interface_name_field,
        scope,
        address_states,
        vrfs,
        source_address_counts,
        ports_by_id,
        vrf_identities,
    ):
        """
        Enrich each IP row with its NetBox address, interface and VRF suggestion.

        A row that an object the actor cannot view blocks gets :class:`ViewBlock` and no NetBox
        identity: no address, no interface, no VRF suggestion and no create.

        Returns:
            list[tuple[dict, VisibleInterface | None]]: Each enriched row and its target interface.

        """
        vrf_matches = scope.vrf_matches(vrf_identities)
        bases = []
        for ip_entry in ip_data:
            port_info = ports_by_id.get(_port_key(ip_entry["port_id"]))
            enriched_ip = self._create_base_ip_entry(ip_entry, obj, vrfs)
            if port_info:
                enriched_ip["interface_name"] = port_info.get(interface_name_field)
            bases.append(enriched_ip)
        targets = scope.resolve_ip_interfaces(
            {"port_id": row["port_id"], "interface_name": row.get("interface_name")} for row in bases
        )

        enriched = []
        for enriched_ip, target in zip(bases, targets, strict=True):
            state = address_states[enriched_ip["ip_with_mask"]]
            view_block = self._view_block(state, target)
            if view_block is not None:
                enriched_ip["view_block"] = view_block
                enriched_ip["status"] = "blocked"
                enriched.append((enriched_ip, None))
                continue
            interface = target.interface if isinstance(target, VisibleInterface) else None
            enriched_ip["netbox_addresses"] = state.visible
            ip_address = self._matching_netbox_ip(enriched_ip, source_address_counts, state.visible, interface)
            if ip_address:
                self._enrich_existing_ip(enriched_ip, ip_address, interface)
            if interface is not None:
                enriched_ip["interface_name"] = interface.name
                enriched_ip["netbox_interface"] = interface
            self._add_vrf_suggestion(enriched_ip, vrf_identities.get(_port_key(enriched_ip["port_id"])), vrf_matches)
            enriched.append((enriched_ip, target if interface is not None else None))
        return enriched

    @staticmethod
    def _view_block(state, target):
        """Return why a hidden object blocks the row, or None; an address block wins over an interface block."""
        if state.blocked:
            return ViewBlock.HELD_ON_HIDDEN_INTERFACE if state.held_on_hidden_interface else ViewBlock.ADDRESS
        return ViewBlock.INTERFACE if target is WITHHELD else None

    def _matching_netbox_ip(self, enriched_ip, source_address_counts, ip_matches, interface):
        """
        Return the one NetBox address the row matches, or None, and set the row's status when none does.

        An address LibreNMS reports twice matches only on the row's own interface.
        """
        ip_with_mask = enriched_ip["ip_with_mask"]
        if source_address_counts[ip_with_mask] > 1:
            ip_matches = [ip for ip in ip_matches if interface is not None and ip.assigned_object == interface]
        global_matches = [ip for ip in ip_matches if ip.vrf_id is None]
        ip_address = global_matches[0] if len(global_matches) == 1 else None
        if ip_address is None and len(ip_matches) == 1:
            ip_address = ip_matches[0]
        if ip_address is None:
            # New IP that doesn't exist in NetBox
            enriched_ip["exists"] = bool(ip_matches)
            enriched_ip["status"] = "update" if ip_matches else "sync"
        return ip_address

    def _flag_management_ip(self, enriched_data, mgmt_ip):
        """
        Mark the entry whose IP equals the device's LibreNMS management IP.

        Used by the "Set Primary IP" toggle on the IP-sync tab to auto-select the
        right row. *mgmt_ip* comes from the snapshot's device record; an empty
        *mgmt_ip* flags nothing.

        Args:
            enriched_data: The enriched IP entries to scan; the matching entry is
                mutated in place (``is_mgmt_ip = True``).
            mgmt_ip (str): The device's LibreNMS management IP.

        Returns:
            None
        """
        if not mgmt_ip:
            return
        for entry in enriched_data:
            if same_host(entry.get("ip_address", ""), mgmt_ip):
                entry["is_mgmt_ip"] = True
                break

    @staticmethod
    def _add_vrf_suggestion(enriched_ip, librenms_vrf, vrf_matches):
        """
        Carry the source identity, and suggest or offer a create only for an address NetBox does not hold.

        This is the one eligibility rule for the row's Create VRF action: the endpoint re-derives
        the row through it and refuses a row that does not carry ``vrf_create_url``.
        """
        if librenms_vrf is None:
            return
        enriched_ip["librenms_vrf"] = librenms_vrf
        # `exists`, not just `vrf_id`: an address already in the Global table has no vrf_id either,
        # and preselecting a VRF on it contradicts the "Synced" status the row renders.
        if enriched_ip.get("vrf_id") is not None or enriched_ip.get("exists"):
            return
        match = vrf_matches[_port_key(enriched_ip["port_id"])]
        if not match.blocks_create:
            enriched_ip["can_create_vrf"] = True
        elif match.suggested is not None:
            # Its own key, never vrf_id: that one means "the VRF NetBox has for this row", and a
            # VRF choice reads it to decide whether an address is already synced.
            enriched_ip["suggested_vrf_id"] = match.suggested.pk
            enriched_ip["vrf_suggested_from"] = {"name": librenms_vrf["name"], "matched_by": match.matched_by}

    def _create_base_ip_entry(self, ip_entry, obj, vrfs):
        """Create the base data structure for an IP entry."""
        parsed = parse_librenms_ip_entry(ip_entry)
        ip_address = str(parsed.ip)
        prefix_length = parsed.network.prefixlen
        ip_with_mask = str(parsed)

        entry = {
            "ip_address": ip_address,
            "prefix_length": prefix_length,
            "ip_with_mask": ip_with_mask,
            "port_id": ip_entry["port_id"],
            "device": obj.name,
            "vrf_id": None,
            "vrfs": vrfs,
        }
        return entry

    def _enrich_existing_ip(self, enriched_ip, ip_address, interface):
        """Add information for IP addresses that exist in NetBox."""
        enriched_ip["netbox_ip"] = ip_address
        enriched_ip["netbox_ip_id"] = ip_address.pk
        enriched_ip["original_vrf_id"] = ip_address.vrf_id
        enriched_ip["exists"] = True

        # Add VRF info if available
        if ip_address.vrf:
            enriched_ip["vrf_id"] = ip_address.vrf.pk
            enriched_ip["vrf"] = ip_address.vrf.name

        # Set initial status to update (will change to matched if criteria met)
        enriched_ip["status"] = "update"

        if interface is not None and ip_address.assigned_object == interface:
            enriched_ip["status"] = "matched"

    def _add_rule_blocks(self, assignments, port_caches, server_key):
        """
        Stamp, on every row, the decision the IP sync makes before it assigns the address.

        One ``PortDisclosure`` is preloaded for every row, so the check costs a fixed number of
        queries per render.

        Args:
            assignments (list[tuple[dict, VisibleInterface]]): Each row and its target.
            port_caches (tuple[dict, dict]): The snapshot's row and bound port records.
            server_key (str): The LibreNMS server whose bindings count.

        """
        if not assignments:
            return
        port_indexes = tuple(index_ip_port_records(records) for records in port_caches)
        decided = [
            (
                enriched_ip,
                ip_assignment_ports(*port_indexes, enriched_ip["port_id"], target.interface, server_key, target.owner),
            )
            for enriched_ip, target in assignments
        ]
        rules = InterfaceRuleMatcher.load()
        if not rules.ignores_any_port:
            return
        disclose = PortDisclosure(self.actor, server_key)
        disclose.preload((port_id, owner) for _row, ports in decided for port_id, _record, _platform, owner in ports)
        for enriched_ip, ports in decided:
            if (blocked := rules.first_blocked_port(ports, disclose)) is not None:
                enriched_ip["rule_block"] = row_rule_block(*blocked)

    def _add_ip_rule_blocks(self, row_targets, address_states):
        """
        Stamp each row whose address an IP mapping ignores in the VRF the row selects first.

        The VRF select shows :func:`shown_vrf_id`. A VRF choice decides the row again in
        :func:`apply_vrf_choices`, with the same :func:`decide_ip_row_rule`. A view-blocked row is not
        decided: the block wins.

        Args:
            row_targets (list[tuple[dict, VisibleInterface | None]]): Each row and its interface.
            address_states (dict): The :class:`AddressState` of each ``address/prefix``.

        """
        rules = IPRuleMatcher.load()
        for enriched_ip, target in row_targets:
            if enriched_ip.get("view_block") is not None:
                continue
            vrf_id = shown_vrf_id(enriched_ip)
            ip_with_mask = enriched_ip["ip_with_mask"]
            interface = target.interface if target is not None else None
            rule, held = decide_ip_row_rule(
                rules, ip_with_mask, vrf_id, interface, address_states[ip_with_mask].visible
            )
            if rule is None:
                continue
            enriched_ip["ip_rule_block"] = row_ip_rule_block(rule)
            if held:
                enriched_ip["status"] = "matched"
            elif enriched_ip["status"] == "matched":
                # The row's interface holds the address only in a VRF that the select does not show.
                enriched_ip["status"] = "update"


def assess(snapshot, *, subject, actor, interface_name_field):
    """Return current IP rows and port indexes without HTTP, cache access or writes."""
    return _Assessment(actor, interface_name_field).assess(subject, snapshot, snapshot.server_key)
