"""Assess IP source facts against the actor's visible NetBox state."""

from collections import defaultdict
from dataclasses import dataclass
from ipaddress import ip_interface
from dcim.models import Device, Interface
from django.db.models import Q
from ipam.models import IPAddress, VRF
from virtualization.models import VMInterface

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import port_vrf_id
from netbox_librenms_plugin.acquisition.snapshot import Outcome
from netbox_librenms_plugin.constants import LIBRENMS_GLOBAL_ROUTING_INSTANCE
from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher, row_rule_block
from netbox_librenms_plugin.ip_rules import IPRuleMatcher, row_ip_rule_block
from netbox_librenms_plugin.ip_addressing import parse_librenms_ip_entry
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id, normalize_librenms_port_id
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.utils import (
    get_virtual_chassis_members,
    identify_ip_sync_rows,
    index_ip_source_interfaces,
    index_ip_port_records,
    ip_row_port_record,
    PortDisclosure,
    resolve_ip_source_interface,
    same_host,
)


def interface_scope(subject, actor):
    """Return the visible owners and their visible interfaces."""
    if isinstance(subject, Device):
        member_ids = [member.pk for member in get_virtual_chassis_members(subject)]
        owners = list(Device.objects.restrict(actor, "view").filter(pk__in=member_ids))
        return owners, list(Interface.objects.restrict(actor, "view").filter(device__in=owners))
    return [subject], list(VMInterface.objects.restrict(actor, "view").filter(virtual_machine=subject))


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

        The table, the verify and the sync all read the rows through this method, so they agree.
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

        source_address_counts = defaultdict(int)
        for ip_entry in ip_data:
            source_address_counts[str(parse_librenms_ip_entry(ip_entry))] += 1
        # Only an address LibreNMS reported can match a row, so scope the NetBox IP scan to them.
        prefetched_data = self._prefetch_netbox_data(obj, set(source_address_counts), server_key)
        ports_by_id, bound_ports_by_id = _index_snapshot_ports(
            snapshot.value(resource, "ports"), ip_data, set(prefetched_data["interfaces_by_librenms_id"])
        )
        rows = self._enrich_rows(
            ip_data,
            obj,
            interface_name_field,
            prefetched_data,
            source_address_counts,
            ports_by_id,
            _vrf_identities(ports_by_id, ip_data, vrf_rows),
        )
        assignments = [(row, interface) for row, interface in rows if interface is not None]
        self._add_rule_blocks(assignments, (ports_by_id, bound_ports_by_id), server_key, prefetched_data)
        enriched_data = [row for row, _interface in rows]
        self._add_ip_rule_blocks(enriched_data, {vrf.pk for vrf in prefetched_data["vrfs"]})
        self._flag_management_ip(enriched_data, mgmt_ip)
        return IPAssessment(
            rows=identify_ip_sync_rows(enriched_data),
            ports_by_id=ports_by_id,
            bound_ports_by_id=bound_ports_by_id,
            mgmt_ip=mgmt_ip,
            interface_name_field=interface_name_field,
        )

    def _enrich_rows(
        self, ip_data, obj, interface_name_field, prefetched_data, source_address_counts, ports_by_id, vrf_identities
    ):
        """
        Enrich each IP row with its NetBox address, interface and VRF suggestion.

        Returns:
            list[tuple[dict, Interface | VMInterface | None]]: Each enriched row and its target interface.

        """
        # A matching VRF the user cannot view still blocks a create; only a viewable one
        # is ever suggested or listed.
        candidate_rds = {identity["rd"] for identity in vrf_identities.values() if identity["rd"]}
        candidate_names = {identity["name"] for identity in vrf_identities.values()}
        candidate_vrfs = VRF.objects.filter(Q(rd__in=candidate_rds) | Q(name__in=candidate_names))
        vrf_suggestions = self._load_vrf_suggestions(vrf_identities, candidate_vrfs)
        visible_vrf_ids = {vrf.pk for vrf in prefetched_data["vrfs"]}

        enriched = []
        for ip_entry in ip_data:
            port_info = ports_by_id.get(_port_key(ip_entry["port_id"]))
            enriched_ip = self._create_base_ip_entry(ip_entry, obj, prefetched_data["vrfs"])
            librenms_interface_name = None
            if port_info:
                librenms_interface_name = port_info.get(interface_name_field)
                enriched_ip["interface_name"] = librenms_interface_name

            ip_address = self._matching_netbox_ip(enriched_ip, source_address_counts, prefetched_data)
            if ip_address:
                self._enrich_existing_ip(
                    enriched_ip, ip_address, ip_entry["port_id"], librenms_interface_name, prefetched_data
                )

            # Add interface information (regardless of IP status)
            interface = self._add_interface_info_to_ip(
                enriched_ip, ip_entry["port_id"], librenms_interface_name, prefetched_data
            )
            self._add_vrf_suggestion(
                enriched_ip,
                _port_key(ip_entry["port_id"]),
                vrf_identities,
                vrf_suggestions,
                visible_vrf_ids,
            )
            enriched.append((enriched_ip, interface))
        return enriched

    def _matching_netbox_ip(self, enriched_ip, source_address_counts, prefetched_data):
        """
        Return the one NetBox address the row matches, or None, and set the row's status when none does.

        An address LibreNMS reports twice matches only on the row's own interface.
        """
        ip_with_mask = enriched_ip["ip_with_mask"]
        ip_matches = prefetched_data["ip_addresses_map"].get(ip_with_mask, [])
        if source_address_counts[ip_with_mask] > 1:
            source_interface = resolve_ip_source_interface(
                enriched_ip,
                prefetched_data["interfaces_by_librenms_id"],
                prefetched_data["interfaces_by_name"],
                server_key=prefetched_data["server_key"],
            )
            ip_matches = [
                ip for ip in ip_matches if source_interface is not None and ip.assigned_object == source_interface
            ]
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

    def _prefetch_netbox_data(self, obj, candidate_addresses, server_key):
        """
        Prefetch all necessary NetBox data to minimize database queries.

        Args:
            obj: The NetBox device or virtual machine the IP rows belong to.
            candidate_addresses: The ``address/prefix`` strings LibreNMS reported. Only these
                can match a row, so the IPAddress scan is restricted to them.
            server_key: The LibreNMS server key scoping per-server interface matching.

        Returns:
            dict: The interface, IP, and VRF lookup maps used during enrichment.
        """
        owners, all_interfaces = interface_scope(obj, self.actor)
        obj_device_id = obj.pk if isinstance(obj, Device) else None
        interfaces_by_librenms_id, interfaces_by_name = index_ip_source_interfaces(
            all_interfaces, server_key, obj_device_id
        )

        # Get the NetBox rows for the reported addresses only
        ip_addresses_map = defaultdict(list)
        if candidate_addresses:
            for ip in IPAddress.objects.filter(address__in=list(candidate_addresses)).select_related(
                "assigned_object_type", "vrf"
            ):
                # NetBox renders through netaddr ("::192.0.2.1/128") while the candidate keys come
                # from the ipaddress module ("::c000:201/128"). Normalise with the same parser so
                # an IPv4-compatible IPv6 row still matches.
                ip_addresses_map[str(ip_interface(str(ip.address)))].append(ip)

        # The dropdown lists only VRFs the user may view; identity matching is scoped separately.
        vrfs = list(VRF.objects.restrict(self.actor, "view"))

        return {
            "interfaces_by_librenms_id": interfaces_by_librenms_id,
            "interfaces_by_name": interfaces_by_name,
            "server_key": server_key,
            "all_interfaces": all_interfaces,
            "owner_by_id": {owner.pk: owner for owner in owners},
            "device": obj,
            "ip_addresses_map": ip_addresses_map,
            "vrfs": vrfs,
        }

    def _load_vrf_suggestions(self, vrf_identities, netbox_vrfs):
        """
        Match the snapshot's LibreNMS VRF identities to current NetBox VRFs.

        Args:
            vrf_identities: LibreNMS VRF identities keyed by :func:`_port_key`.
            netbox_vrfs: NetBox VRFs available in the dropdown.

        Returns:
            dict: Per port, the suggested NetBox VRF and its source data, or ``{"create": True}``
                when no NetBox VRF has the identity's route distinguisher or name. A value that
                two NetBox VRFs claim gets neither.

        """
        netbox_by_rd = defaultdict(list)
        netbox_by_name = defaultdict(list)
        for vrf in netbox_vrfs:
            if vrf.rd:
                netbox_by_rd[str(vrf.rd)].append(vrf)
            netbox_by_name[vrf.name].append(vrf)
        unique_netbox_by_rd = {rd: matches[0] for rd, matches in netbox_by_rd.items() if len(matches) == 1}
        unique_netbox_by_name = {name: matches[0] for name, matches in netbox_by_name.items() if len(matches) == 1}

        suggestions = {}
        for port_key, identity in vrf_identities.items():
            target = unique_netbox_by_rd.get(identity["rd"]) if isinstance(identity["rd"], str) else None
            matched_by = "route distinguisher"
            if target is None:
                target = unique_netbox_by_name.get(identity["name"])
                matched_by = "name"
                # A name match must not cross into another routing domain.
                if target is not None and identity["rd"] and target.rd and str(target.rd) != identity["rd"]:
                    target = None
            if target is None:
                # Any VRF with the RD or the name, an ambiguous one too, rules out a create.
                if identity["rd"] not in netbox_by_rd and identity["name"] not in netbox_by_name:
                    suggestions[port_key] = {"create": True}
                continue
            suggestions[port_key] = {
                "vrf_id": target.pk,
                "source": {"name": identity["name"], "matched_by": matched_by},
            }
        return suggestions

    @staticmethod
    def _add_vrf_suggestion(enriched_ip, port_key, vrf_identities, vrf_suggestions, visible_vrf_ids):
        """
        Carry the source identity, and suggest or offer a create only for an address NetBox does not hold.

        This is the one eligibility rule for the row's Create VRF action: the endpoint re-derives
        the row through it and refuses a row that does not carry ``vrf_create_url``.
        """
        librenms_vrf = vrf_identities.get(port_key)
        if librenms_vrf is not None:
            enriched_ip["librenms_vrf"] = librenms_vrf
        # `exists`, not just `vrf_id`: an address already in the Global table has no vrf_id either,
        # and preselecting a VRF on it contradicts the "Synced" status the row renders.
        if enriched_ip.get("vrf_id") is not None or enriched_ip.get("exists"):
            return
        suggestion = vrf_suggestions.get(port_key)
        if suggestion is not None and suggestion.get("create"):
            enriched_ip["can_create_vrf"] = True
        elif suggestion is not None and suggestion["vrf_id"] in visible_vrf_ids:
            # Its own key, never vrf_id: that one means "the VRF NetBox has for this row", and the
            # verify path reads it to decide whether an address is already synced.
            enriched_ip["suggested_vrf_id"] = suggestion["vrf_id"]
            enriched_ip["vrf_suggested_from"] = suggestion["source"]

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

    @staticmethod
    def _resolve_ip_row_interface(port_id, librenms_interface_name, prefetched_data):
        """Resolve one row's source interface, so both callers agree on the row shape."""
        return resolve_ip_source_interface(
            {"port_id": port_id, "interface_name": librenms_interface_name},
            prefetched_data["interfaces_by_librenms_id"],
            prefetched_data["interfaces_by_name"],
            server_key=prefetched_data["server_key"],
        )

    def _enrich_existing_ip(self, enriched_ip, ip_address, port_id, librenms_interface_name, prefetched_data):
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

        # Only proceed if IP is assigned to an object
        if not ip_address.assigned_object:
            return

        assigned_interface = ip_address.assigned_object

        interface = self._resolve_ip_row_interface(port_id, librenms_interface_name, prefetched_data)
        if interface is not None and assigned_interface == interface:
            enriched_ip["status"] = "matched"
            enriched_ip["interface_name"] = interface.name
            enriched_ip["netbox_interface"] = interface

    def _add_interface_info_to_ip(self, enriched_ip, port_id, librenms_interface_name, prefetched_data):
        """Add the same scoped interface that the sync writer will resolve, and return it."""
        interface = self._resolve_ip_row_interface(port_id, librenms_interface_name, prefetched_data)
        if interface is not None:
            enriched_ip["interface_name"] = interface.name
            enriched_ip["netbox_interface"] = interface
        return interface

    def _add_rule_blocks(self, assignments, port_caches, server_key, prefetched_data):
        """
        Stamp, on every row, the decision the IP sync makes before it assigns the address.

        One ``PortDisclosure`` is preloaded for every row, so the check costs a fixed number of
        queries per render.

        Args:
            assignments (list[tuple[dict, Interface | VMInterface]]): Each row and its target.
            port_caches (tuple[dict, dict]): The snapshot's row and bound port records.
            server_key (str): The LibreNMS server whose bindings count.
            prefetched_data (dict): The owners from :meth:`_prefetch_netbox_data`.

        """
        if not assignments:
            return
        port_indexes = tuple(index_ip_port_records(records) for records in port_caches)
        decided = [
            (
                enriched_ip,
                ip_assignment_ports(
                    *port_indexes,
                    enriched_ip["port_id"],
                    interface,
                    server_key,
                    prefetched_data["owner_by_id"][self._interface_owner_id(interface)],
                ),
            )
            for enriched_ip, interface in assignments
        ]
        rules = InterfaceRuleMatcher.load()
        if not rules.ignores_any_port:
            return
        disclose = PortDisclosure(self.actor, server_key)
        disclose.preload((port_id, owner) for _row, ports in decided for port_id, _record, _platform, owner in ports)
        for enriched_ip, ports in decided:
            if (blocked := rules.first_blocked_port(ports, disclose)) is not None:
                enriched_ip["rule_block"] = row_rule_block(*blocked)

    def _add_ip_rule_blocks(self, enriched_data, visible_vrf_ids):
        """
        Stamp each row whose address an IP mapping ignores in the VRF the row selects first.

        The VRF select shows the row's NetBox VRF or its suggested VRF when the user may view it,
        else Global. A VRF change re-decides the row in ``SingleIPAddressVerifyView``.

        Args:
            enriched_data (list[dict]): The enriched rows.
            visible_vrf_ids (set[int]): The VRFs the select lists.

        """
        rules = IPRuleMatcher.load()
        for enriched_ip in enriched_data:
            shown = (enriched_ip["vrf_id"], enriched_ip.get("suggested_vrf_id"))
            vrf_id = next((pk for pk in shown if pk is not None and pk in visible_vrf_ids), None)
            rule = rules.decide(ip_interface(enriched_ip["ip_with_mask"]).ip, vrf_id)
            if rule is not None:
                enriched_ip["ip_rule_block"] = row_ip_rule_block(rule)

    @staticmethod
    def _interface_owner_id(interface):
        """Return the pk of the Device or VirtualMachine that owns *interface*."""
        return interface.device_id if isinstance(interface, Interface) else interface.virtual_machine_id


def assess(snapshot, *, subject, actor, interface_name_field):
    """Return current IP rows and port indexes without HTTP, cache access or writes."""
    return _Assessment(actor, interface_name_field).assess(subject, snapshot, snapshot.server_key)
