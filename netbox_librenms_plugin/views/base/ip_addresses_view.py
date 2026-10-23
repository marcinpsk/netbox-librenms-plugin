import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from ipaddress import ip_interface

from dcim.models import Device, Interface
from django.contrib import messages
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views import View
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.coordinator import acquire
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes, port_vrf_id
from netbox_librenms_plugin.acquisition.snapshot import Outcome
from netbox_librenms_plugin.constants import LIBRENMS_GLOBAL_ROUTING_INSTANCE
from netbox_librenms_plugin.interface_rules import interface_rules_for_request, row_rule_block
from netbox_librenms_plugin.ip_addressing import parse_address_with_prefix, parse_librenms_ip_entry
from netbox_librenms_plugin.ip_rules import ip_rules_for_request, row_ip_rule_block
from netbox_librenms_plugin.librenms_ids import (
    coerce_librenms_id,
    normalize_librenms_port_id,
)
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
from netbox_librenms_plugin.sync_filters import filter_sync_table, parse_table_filters
from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable, management_row_vrfs
from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_virtual_chassis_members,
    identify_ip_sync_rows,
    index_ip_source_interfaces,
    index_ip_sync_rows,
    index_ip_port_records,
    ip_row_port_record,
    normalize_ip_sync_row_id,
    PortDisclosure,
    resolve_create_missing_interfaces,
    resolve_ip_source_interface,
    resolve_set_primary_ip,
    same_host,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

logger = logging.getLogger(__name__)


def _port_key(port_id):
    """Return the one key form the port map uses; LibreNMS types an id as int here and str there."""
    return str(port_id)


def ip_interface_scope(view, obj):
    """
    Return the owners and interfaces the IP tab resolves rows against, for the table and the sync alike.

    Both are view-scoped: the object and the chassis members the caller may view, and their
    interfaces the caller may view. The sync locks the same owners, so the two never disagree.

    Args:
        view: A view with ``restricted_queryset`` bound to the request.
        obj (Device | VirtualMachine): The object whose IP rows are resolved.

    Returns:
        tuple[list, list]: The owners, and their interfaces.

    """
    if isinstance(obj, Device):
        member_ids = [member.pk for member in get_virtual_chassis_members(obj)]
        owners = list(view.restricted_queryset(Device).filter(pk__in=member_ids))
        return owners, list(view.restricted_queryset(Interface).filter(device__in=owners))
    return [obj], list(view.restricted_queryset(VMInterface).filter(virtual_machine=obj))


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


def assess_ip_snapshot(request, obj, snapshot, server_key):
    """Assess *snapshot* as the request user, through the IP table's own view."""
    view = BaseIPAddressTableView()
    view.setup(request)
    return view.assess_snapshot(obj, snapshot, server_key)


class BaseIPAddressTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Base view for synchronizing IP address information from LibreNMS."""

    partial_template_name = "netbox_librenms_plugin/_ipaddress_sync_content.html"

    def get_object(self, pk):
        return self.restrict_object_or_404(self.model, pk=pk)

    def assess_snapshot(self, obj, snapshot, server_key):
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
        interface_name_field = get_interface_name_field(self.request, obj)
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
        vrf_create_url = reverse(
            "plugins:netbox_librenms_plugin:create_ip_row_vrf",
            kwargs={"object_type": obj._meta.model_name, "pk": obj.pk},
        )

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
                vrf_create_url,
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
        owners, all_interfaces = ip_interface_scope(self, obj)
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
        vrfs = list(self.restricted_queryset(VRF))

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
    def _add_vrf_suggestion(enriched_ip, port_key, vrf_identities, vrf_suggestions, vrf_create_url, visible_vrf_ids):
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
            enriched_ip["vrf_create_url"] = vrf_create_url
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
            "device_url": obj.get_absolute_url(),
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
        enriched_ip["ip_url"] = ip_address.get_absolute_url()
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
            enriched_ip["interface_url"] = interface.get_absolute_url()

    def _add_interface_info_to_ip(self, enriched_ip, port_id, librenms_interface_name, prefetched_data):
        """Add the same scoped interface that the sync writer will resolve, and return it."""
        interface = self._resolve_ip_row_interface(port_id, librenms_interface_name, prefetched_data)
        if interface is not None:
            enriched_ip["interface_name"] = interface.name
            enriched_ip["interface_url"] = interface.get_absolute_url()
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
        rules = interface_rules_for_request(self.request)
        if not rules.ignores_any_port:
            return
        disclose = PortDisclosure(self.request.user, server_key)
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
        rules = ip_rules_for_request(self.request)
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

    def _prepare_context(self, request, obj, server_key):
        """Return the tab context of the stored snapshot, or None when there is no snapshot to show."""
        snapshot = source_cache.read(obj, Resource.IP_ADDRESSES, server_key)
        if snapshot is None:
            return None
        return self._snapshot_context(request, obj, snapshot, server_key)

    def _snapshot_context(self, request, obj, snapshot, server_key):
        """Return the tab context of one snapshot, assessed for the request user."""
        ip_data = self.assess_snapshot(obj, snapshot, server_key).rows
        cache_ttl = source_cache.remaining_ttl(obj, Resource.IP_ADDRESSES, server_key)
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl else None

        # The management rows and the row identities describe the whole snapshot, so they come before the filter.
        mgmt_row_vrfs = management_row_vrfs(ip_data)
        table, filters, filtered_from = filter_sync_table(
            request, ip_data, lambda rows: IPAddressTable(rows, mgmt_row_vrfs=mgmt_row_vrfs)
        )
        table.configure(request)

        return {
            "table": table,
            "filters": filters,
            # The row count before the filter, shown only while a filter narrows the table.
            "filtered_from": filtered_from,
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
            "set_primary_ip": resolve_set_primary_ip(request),
            "create_missing_interfaces": resolve_create_missing_interfaces(request),
            # Donor "Move IP to winner" candidates (empty unless this device carries a
            # _migrated_to marker for server_key); drives the migrated-mode action card.
            "movable_ips": self._movable_ips_for_migration(obj, server_key),
        }

    def _empty_context(self, request, obj, server_key, migration_server_key):
        """
        Return the tab context without a table.

        The set-primary-IP and create-missing switches keep their state, and the donor's move card
        stays: its moves are NetBox operations that never read LibreNMS.
        """
        return {
            "table": None,
            "object": obj,
            "cache_expiry": None,
            "server_key": server_key,
            "set_primary_ip": resolve_set_primary_ip(request),
            "create_missing_interfaces": resolve_create_missing_interfaces(request),
            "movable_ips": self._movable_ips_for_migration(obj, migration_server_key),
        }

    @staticmethod
    def _movable_ips_for_migration(obj, server_key):
        """
        List the donor's interface-assigned IPs as Move-to-winner candidates (empty unless migrated).

        Only NetBox Device ``Interface`` assignments are listed: MoveIPAddressToWinnerView re-homes an
        IP from a donor interface to the winner's same-named interface and rejects non-Interface (e.g.
        VMInterface) assignments, so VM-owned IPs are intentionally excluded. Gated on the marker so a
        non-migrated device pays no extra query.

        Args:
            obj (Device | VirtualMachine): The candidate donor object.
            server_key (str | None): The LibreNMS server key for the migration marker.

        Returns:
            list[dict[str, int | str]]: The movable IP candidates, or an empty list.
        """
        from dcim.models import Device, Interface
        from django.contrib.contenttypes.models import ContentType

        if not isinstance(obj, Device) or not read_mapping(obj).migrated_to(server_key or "default"):
            return []
        name_by_id = {iface.pk: iface.name for iface in obj.interfaces.all()}
        if not name_by_id:
            return []
        iface_ct = ContentType.objects.get_for_model(Interface)
        return [
            {"id": ip.pk, "address": str(ip.address), "interface_name": name_by_id.get(ip.assigned_object_id, "")}
            for ip in IPAddress.objects.filter(
                assigned_object_type=iface_ct, assigned_object_id__in=list(name_by_id)
            ).order_by("address")
        ]

    def get_context_data(self, request, obj):
        """Get the context data for the IP address sync view."""
        # The filters show their errors also when no snapshot is cached.
        filters = parse_table_filters(request.GET, lambda rows: IPAddressTable(rows, mgmt_row_vrfs={}))
        # GET render: scope the snapshot read to ?server_key, so a non-default-server tab reads that
        # server's snapshot. A key that no longer resolves renders an empty table scoped to that key,
        # never the default server's snapshot.
        scoped, unresolved = self.resolve_get_render_server_key(request)
        server_key = None if unresolved else scoped or self._render_server_key()
        context = self._prepare_context(request, obj, server_key) if server_key is not None else None
        if context is None:
            context = self._empty_context(request, obj, scoped, scoped)
        context.setdefault("filters", filters)
        return context

    def post(self, request, pk):
        """Refresh the IP snapshot from LibreNMS and render the tab."""
        obj = self.get_object(pk)
        # Rebind the API to the POSTed server, so discovery and acquisition use the server of the tab.
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None to avoid building a missing/misconfigured
            # default client; reading the lazy `librenms_api` property here would reconstruct it
            # and can raise (a 500 on this HTMX error path). Use the already-cached client's key.
            active_server_key = self.active_server_key
            # render_sync_partial injects the migrated-donor context (resolved from the active
            # session key, since the POSTed key is now known-invalid) so a stale server_key can't
            # silently re-enable IP sync on a migrated donor.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {"ip_sync": self._empty_context(request, obj, None, active_server_key)},
            )

        # Discovery may find the LibreNMS device by IP or hostname and store the mapping; acquisition
        # then reads that mapping and never writes.
        librenms_id, lookup_error = self.resolve_librenms_id(obj)
        error_message = None
        if librenms_id is None:
            error_message = self.scoped_lookup_message(lookup_error) if lookup_error else "Device not found in LibreNMS"
        else:
            snapshot = acquire(obj, server_key=server_key, resources=[Resource.IP_ADDRESSES])
            if not source_cache.store(obj, Resource.IP_ADDRESSES, snapshot):
                error_message = _refresh_failure_message(snapshot)

        if error_message is not None:
            # A failed refresh must not leave an older snapshot to act on.
            source_cache.delete(obj, Resource.IP_ADDRESSES, server_key)
            messages.error(request, error_message)
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.IP_ADDRESSES,
                server_key,
                actor_id=request_actor_id(request),
            )
            return self.render_sync_partial(
                request, obj, server_key, {"ip_sync": self._empty_context(request, obj, server_key, server_key)}
            )

        if SyncCacheConsistency(obj).mark_refresh_outcome(
            SyncTab.IP_ADDRESSES,
            server_key,
            actor_id=request_actor_id(request),
        ):
            messages.success(request, "IP address data refreshed successfully.")
        else:
            # The rows below come from this refresh and are rendered, so the message must not
            # claim there is nothing to show. Only the snapshot write failed, and a sync posts
            # against that snapshot, so say what the user has to do before syncing.
            messages.error(
                request,
                "IP address data could not be cached. The rows shown come from this refresh only "
                "and will not survive a reload. Refresh again before syncing; see server logs for details.",
            )
        return self.render_sync_partial(
            request, obj, server_key, {"ip_sync": self._snapshot_context(request, obj, snapshot, server_key)}
        )


def _refresh_failure_message(snapshot):
    """Return the refresh error that names the first response the IP rows could not do without."""
    (records,) = blocked_scopes(snapshot, Resource.IP_ADDRESSES).values()
    record = records[0]
    return f"Failed to fetch IP addresses from LibreNMS: {record.request.path} was not obtained ({record.reason})"


class SingleIPAddressVerifyView(NetBoxObjectPermissionMixin, LibreNMSPermissionMixin, CacheMixin, View):
    """View for verifying single IP address data with different VRF."""

    # A fetch() caller reads the answer, so the middleware answers with a JSON error.
    answers_json = True

    # Read-only verify endpoint: require object-view permission (mirrors the interface/module/
    # cable verify views). Without it any user with mere plugin-view rights could POST an
    # arbitrary device id and read back that object's name/url/cached IP rows. The effective
    # requirement is narrowed per-request in post() to the model the request actually targets
    # (Device vs VirtualMachine); this class-level default (both) is the fail-closed fallback the
    # permission-wiring test asserts.
    required_object_permissions = {"POST": [("view", Device), ("view", VirtualMachine)]}

    def require_object_permissions_json(self, method, *, object_id=None, object_type=None):
        """
        Resolve the per-object view permission for this verify request, then run the base gate.

        Computing the requirement here (rather than inline in :meth:`post`) keeps the DB
        model-resolution behind the same method the post-flow tests mock out — so those tests
        exercise the request flow without a DB hit, while the real gate path resolves Device vs
        VirtualMachine and checks the matching view permission.

        Args:
            method: HTTP method to gate (``"POST"``).
            object_id: The posted object id, used to resolve the model when no type is given.
            object_type: The posted ``object_type`` (``"device"``/``"virtualmachine"``), if any.

        Returns:
            None if permitted, or a 403 JsonResponse if denied.
        """
        self.required_object_permissions = {method: self._required_perms_for_object(object_id, object_type)}
        return super().require_object_permissions_json(method)

    def _required_perms_for_object(self, object_id, object_type):
        """
        Object-view perms a verify POST must hold for the object it targets.

        Mirrors :meth:`_get_object`'s resolution so the gate matches the model whose name/url/cache
        the response would expose: an explicit ``object_type`` gates on exactly that model; with no
        type, resolve the id to its model (an existence check that reads no object data), Device
        first, then VirtualMachine. Fall back to requiring BOTH view perms (fail closed) when the id
        is unusable or resolves to neither, so a Device-only (or VM-only) caller can never read the
        other model's data through this endpoint.

        Args:
            object_id (int | str | None): The posted object ID.
            object_type (str | None): The posted object type, if provided.

        Returns:
            list[tuple[str, type]]: The required model permissions.
        """
        if object_type == "device":
            return [("view", Device)]
        if object_type == "virtualmachine":
            return [("view", VirtualMachine)]
        try:
            if Device.objects.filter(pk=object_id).exists():
                return [("view", Device)]
            if VirtualMachine.objects.filter(pk=object_id).exists():
                return [("view", VirtualMachine)]
        except (ValueError, TypeError):
            pass
        return [("view", Device), ("view", VirtualMachine)]

    def _get_object(self, object_id, object_type=None):
        """
        Retrieve the object (Device or VirtualMachine) based on ID and optional type.

        Resolves through permission-restricted querysets: the POST gate only checks model-level
        view perms, so a constrained grant must not resolve an out-of-scope id here. It returns a 404
        instead of exposing the object's cached IP verify payload.

        Args:
            object_id (int): The object ID.
            object_type (str | None): The object type, if provided.

        Returns:
            Device | VirtualMachine: The permitted object.

        Raises:
            Http404: If no permitted Device or VirtualMachine matches the request.
        """
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, pk=object_id)
        else:
            # Try to find object without knowing its type (scoped to the caller's viewable objects).
            obj = self.restricted_queryset(Device).filter(pk=object_id).first()
            if obj:
                return obj

            obj = self.restricted_queryset(VirtualMachine).filter(pk=object_id).first()
            if obj:
                return obj

            raise Http404(f"Object with ID {object_id} not found in Device or VirtualMachine models")

    def _parse_ip_address(self, ip_address):
        """
        Parse IP address string into address and prefix length.

        Works with both IPv4 and IPv6 addresses.

        Args:
            ip_address (str): The IP address with its prefix length.

        Returns:
            tuple[str, int]: The host address and prefix length.

        Raises:
            ValueError: If the IP address or prefix length is invalid.
        """
        parsed = parse_address_with_prefix(ip_address)
        return str(parsed.ip), parsed.network.prefixlen

    @staticmethod
    def _find_row(rows, address, prefix_len, row_id=None):
        """Resolve one assessed source row without guessing among equal addresses."""
        target = str(parse_address_with_prefix(address, prefix_len))
        key = normalize_ip_sync_row_id(row_id) if row_id is not None else target
        if key.partition("@")[0] != target:
            return None, None, None
        index, duplicates = index_ip_sync_rows(rows)
        row = index.get(key) if key not in duplicates else None
        if row is None:
            return None, None, None
        return row, row.get("vrf_id"), row.get("port_id")

    def _find_existing_ip(self, address_no_mask, prefix_len, vrf_id=None):
        """Find existing IP address in NetBox, optionally with specific VRF."""
        ip_with_mask = str(parse_address_with_prefix(address_no_mask, prefix_len))

        matches = list(IPAddress.objects.filter(address=ip_with_mask).order_by("pk"))
        if not matches:
            return False, False, None

        if vrf_id is not None:
            matching_vrf_rows = [row for row in matches if row.vrf_id == vrf_id]
        else:
            matching_vrf_rows = [row for row in matches if row.vrf_id is None]

        if len(matching_vrf_rows) == 1:
            return True, True, matching_vrf_rows[0].get_absolute_url()
        return True, False, matches[0].get_absolute_url() if len(matches) == 1 else None

    def _determine_status(self, exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id):
        """Determine the status of an IP address based on existence and VRF."""
        if exists_any_vrf:
            # IP exists in NetBox
            if exists_specific_vrf:
                return "matched"
            else:
                return "update"
        else:
            # IP doesn't exist in NetBox, check if restoring to original VRF
            if original_vrf_id is not None and original_vrf_id == vrf_id:
                return "matched"
            else:
                return "sync"

    def post(self, request):  # noqa: C901
        """POST request to return json response with formatted IP address status."""
        # Parse the (caller-supplied) body first — this reads no object data — so the permission
        # gate can target the model the request actually addresses. _get_object() resolves a Device
        # OR a VirtualMachine, so a static Device-only gate would let a user with only
        # dcim.view_device read VM name/url/cache data (and vice versa). Gate BELOW still runs
        # before any object is resolved or any cached row is read.
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"status": "error", "message": "Invalid JSON payload"}, status=400)
        # json.loads accepts a bare list/string/number; calling .get() on those raises before the
        # permission gate and the broad handler below, so 500s on a non-object body. Reject it.
        if not isinstance(data, dict):
            return JsonResponse({"status": "error", "message": "JSON payload must be an object"}, status=400)
        object_id = data.get("device_id")
        object_type = data.get("object_type")
        if error := self.require_object_permissions_json("POST", object_id=object_id, object_type=object_type):
            return error
        try:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            ip_address = data.get("ip_address")
            vrf_id = data.get("vrf_id")
            # Validate the requested server key before using it as a cache namespace: an
            # unconfigured/forged key would otherwise let a caller probe arbitrary server-key cache
            # namespaces via get_cache_key(). Mirror the sync/cable membership check; anything
            # unrecognized falls back to "default" (the prior missing-key behaviour), so a caller
            # can only ever address a configured key or the fixed default — never an arbitrary one.
            requested_server_key = data.get("server_key")
            if isinstance(requested_server_key, str) and requested_server_key in LibreNMSAPI.get_available_servers():
                server_key = requested_server_key
            else:
                server_key = "default"

            if not ip_address:
                return JsonResponse({"status": "error", "message": "No IP address provided"}, status=400)

            # Reject JSON booleans explicitly before the falsy check: bool is an int
            # subclass, so True/False would otherwise pass int() and validate as IDs 1/0,
            # and object_id=False would be misreported as "No object ID provided".
            if isinstance(object_id, bool):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)
            # Reject JSON floats too: int(1.9) silently truncates to 1, so a fractional id
            # would coerce to a different object instead of returning a clean 400.
            if isinstance(object_id, float):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)

            if not object_id:
                return JsonResponse({"status": "error", "message": "No object ID provided"}, status=400)

            # Validate the client-supplied numeric IDs up front so a bad value returns a
            # clean 400 instead of raising deep in the ORM and being caught as a generic 500.
            try:
                object_id = int(object_id)
            except (TypeError, ValueError):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)
            if vrf_id in (None, ""):
                vrf_id = None
            elif isinstance(vrf_id, bool):
                return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)
            elif isinstance(vrf_id, float):
                return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)
            else:
                try:
                    vrf_id = int(vrf_id)
                except (TypeError, ValueError):
                    return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)

            # Get the object (Device or VirtualMachine)
            try:
                obj = self._get_object(object_id, object_type)
            except Http404:
                return JsonResponse({"status": "error", "message": f"Object with ID {object_id} not found"}, status=404)

            # Validate both address and source selector before reading the snapshot.
            try:
                address_no_mask, prefix_len = self._parse_ip_address(ip_address)
                if data.get("row_id") is not None:
                    normalize_ip_sync_row_id(data["row_id"])
            except ValueError:
                return JsonResponse(
                    {"status": "error", "message": "Invalid IP address or source row identity"},
                    status=400,
                )

            snapshot = source_cache.read(obj, Resource.IP_ADDRESSES, server_key)
            rows = assess_ip_snapshot(request, obj, snapshot, server_key).rows if snapshot is not None else []

            # Basic record with default values
            updated_record = {
                "ip_address": address_no_mask,
                "prefix_length": prefix_len,
                "ip_with_mask": str(parse_address_with_prefix(address_no_mask, prefix_len)),
                "device": obj.name,
                "device_url": obj.get_absolute_url(),
                "vrf_id": vrf_id,
                "exists": False,
                "status": "sync",
            }

            # Try to find the IP in the assessed rows
            cache_entry, original_vrf_id, original_port_id = self._find_row(
                rows, address_no_mask, prefix_len, data.get("row_id")
            )

            if cache_entry is None and (
                data.get("row_id") is not None
                or sum(row["ip_with_mask"] == updated_record["ip_with_mask"] for row in rows) > 1
            ):
                return JsonResponse(
                    {"status": "error", "message": "Refresh the IP data and select a current source row."}, status=400
                )

            # Update record with the assessed row if found
            if cache_entry:
                # Update with all fields except the ones the chosen VRF decides
                for key, value in cache_entry.items():
                    if key not in ["vrf_id", "status", "ip_rule_block"]:
                        updated_record[key] = value

            # If no source row was found, use first device interface
            if original_port_id is None:
                interface = obj.interfaces.first()
                if interface:
                    updated_record["interface_name"] = interface.name
                    updated_record["interface_url"] = interface.get_absolute_url()

            # Check if IP exists in NetBox
            exists_any_vrf, exists_specific_vrf, ip_url = self._find_existing_ip(address_no_mask, prefix_len, vrf_id)

            if exists_any_vrf:
                updated_record["exists"] = True
                updated_record["ip_url"] = ip_url

            # Determine status based on existence and VRF
            updated_record["status"] = self._determine_status(
                exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id
            )

            ip_rule = ip_rules_for_request(request).decide(ip_interface(updated_record["ip_with_mask"]).ip, vrf_id)
            if ip_rule is not None:
                updated_record["ip_rule_block"] = row_ip_rule_block(ip_rule)

            # Render status HTML
            table = IPAddressTable([], mgmt_row_vrfs={})
            status_html = table.render_status(updated_record["status"], updated_record)

            return JsonResponse(
                {
                    "status": "success",
                    "ip_address": ip_address,
                    "formatted_row": {"status": status_html, "selectable": ip_rule is None},
                }
            )

        except Exception:
            logger.exception("Unexpected error in IP address sync")
            return JsonResponse(
                {"status": "error", "message": "An internal error occurred. Please check server logs."}, status=500
            )
