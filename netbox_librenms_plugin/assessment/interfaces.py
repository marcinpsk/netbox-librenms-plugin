"""
Assess an interfaces source snapshot: the port rows and relationships that every interface reader shows.

The assessment reads the snapshot and the stored name rules only. It needs no request, cache or HTTP,
and it writes nothing, so the tab, the verify, the sync and the relationship actions read one result.
"""

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes, device_os
from netbox_librenms_plugin.constants import MAIN_INVENTORY_SOURCE, OOB_INVENTORY_SOURCE
from netbox_librenms_plugin.interface_relationships import resolve_port_relationships
from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.server_mappings import MappingRole
from netbox_librenms_plugin.utils import apply_lag_vlan_fill, normalize_relationship_maps


def parse_port_vlan_data(port_data: dict, interface_name_field: str = "ifName") -> dict:
    """
    Transform LibreNMS port VLAN data into normalized structure.

    Args:
        port_data: Raw port dict from LibreNMS API
        interface_name_field: Field to use for interface name ('ifName' or 'ifDescr')

    Returns:
        dict: Normalized structure with:
            - port_id: int
            - interface_name: str (value from interface_name_field)
            - ifName: str (always included for reference)
            - ifDescr: str (always included for reference)
            - mode: 'access' | 'tagged' | None
            - untagged_vlan: int | None
            - tagged_vlans: list[int]

    """
    port_id = port_data.get("port_id")
    if_name = port_data.get("ifName", "")
    if_descr = port_data.get("ifDescr", "")
    interface_name = port_data.get(interface_name_field, "") or if_name
    if_vlan = port_data.get("ifVlan", "")
    if_trunk = port_data.get("ifTrunk")

    # Determine 802.1Q mode
    if not if_vlan:
        mode = None
    elif if_trunk == "dot1Q":
        mode = "tagged"
    else:
        mode = "access"

    # Parse VLAN assignments from vlans array if present
    vlans_data = port_data.get("vlans", [])
    untagged_vlan = None
    tagged_vlans = []

    if isinstance(vlans_data, list) and vlans_data:
        # Parse from detailed vlans array
        for vlan_entry in vlans_data:
            if not isinstance(vlan_entry, dict):
                continue
            vlan_id = vlan_entry.get("vlan")
            if vlan_id is None:
                continue
            try:
                vlan_id = int(vlan_id)
            except (ValueError, TypeError):
                continue
            if vlan_entry.get("untagged") == 1:
                untagged_vlan = vlan_id
            else:
                tagged_vlans.append(vlan_id)
    elif if_vlan:
        # Fallback to ifVlan field for basic port info
        try:
            untagged_vlan = int(if_vlan)
        except (ValueError, TypeError):
            pass

    return {
        "port_id": port_id,
        "interface_name": interface_name,
        "ifName": if_name,
        "ifDescr": if_descr,
        "mode": mode,
        "untagged_vlan": untagged_vlan,
        "tagged_vlans": sorted(tagged_vlans),
    }


def stored_name_rules(librenms_os):
    """
    Return the stored LAG, SAP and bridge name rules of one LibreNMS OS, as the resolver takes them.

    Args:
        librenms_os (str): The device's LibreNMS OS; a blank OS selects no LAG or bridge rule.

    Returns:
        dict: The ``lag_patterns``, ``sap_patterns`` and ``bridge_patterns`` arguments of
            :func:`~netbox_librenms_plugin.interface_relationships.resolve_port_relationships`.

    """
    return {
        "lag_patterns": PortStackLagPattern.compiled_patterns_for_os(librenms_os),
        "sap_patterns": PortStackLagPattern.compiled_sap_patterns_for_os(librenms_os),
        "bridge_patterns": PortStackLagPattern.compiled_bridge_patterns_for_os(librenms_os),
    }


def _rows(ports, interface_name_field, source):
    """Add the parsed VLAN fields and the inventory source to each port row."""
    for port in ports:
        port.update(parse_port_vlan_data(port, interface_name_field))
        port["_source"] = source
    return ports


def _normalized_mac(port):
    """Return the port's MAC as 12 bare hex digits, or "" for a missing, malformed or placeholder MAC."""
    mac = port.get("ifPhysAddress")
    if not isinstance(mac, str):
        return ""
    normalized = "".join(ch for ch in mac.lower() if ch in "0123456789abcdef")
    # An all-zero filler on both sides is not a shared address.
    if len(normalized) != 12 or normalized == "0" * 12:
        return ""
    return normalized


def _flag_shared_macs(host_ports, oob_ports):
    """Flag each port whose MAC both the host and the OOB controller report: a shared LOM port."""
    host_pairs = [(port, _normalized_mac(port)) for port in host_ports]
    oob_pairs = [(port, _normalized_mac(port)) for port in oob_ports]
    # One set per source, so a MAC that one source reports twice is not a shared one.
    shared_macs = {mac for _port, mac in host_pairs if mac} & {mac for _port, mac in oob_pairs if mac}
    for port, mac in host_pairs + oob_pairs:
        if mac in shared_macs:
            port["_dedup_conflict"] = True


def assess_interfaces(snapshot, interface_name_field):
    """
    Return the interface rows of a source snapshot, as every interface reader shows them.

    Host rows come first, then the OOB controller's rows. The relationships come from the host rows
    only, with the name rules stored now for the device's OS. A blocked OOB scope leaves the OOB rows
    out and sets ``oob_incomplete``, so the shared-LOM state is unknown.

    Args:
        snapshot (SourceSnapshot): A stored snapshot that holds the interfaces resource.
        interface_name_field (str): The port field that names an interface.

    Returns:
        dict: ``ports``, ``port_stack_relationships`` and ``oob_incomplete``.

    Raises:
        ValueError: The snapshot's host scope is blocked, so it holds no host rows to assess.

    """
    resource = Resource.INTERFACES
    blocked = blocked_scopes(snapshot, resource)
    if "host" in blocked:
        raise ValueError(f"The interfaces snapshot has no usable host scope: {blocked['host'][0].message}")
    host_ports = _rows(snapshot.value(resource, "ports"), interface_name_field, MAIN_INVENTORY_SOURCE)
    oob_ports = []
    oob_incomplete = snapshot.resources[resource].oob is not None and "oob" in blocked
    if snapshot.resources[resource].oob is not None and not oob_incomplete:
        oob_ports = _rows(
            snapshot.value(resource, "ports", MappingRole.OOB), interface_name_field, OOB_INVENTORY_SOURCE
        )
        _flag_shared_macs(host_ports, oob_ports)

    device = snapshot.record(resource, "device")
    relationships = resolve_port_relationships(
        host_ports,
        snapshot.value(resource, "port_stack"),
        interface_name_field=interface_name_field,
        # Without port_stack pairs the device record is optional, and a missing OS selects no LAG rule.
        **stored_name_rules(device_os(device.value) if device.obtained else ""),
    )
    # Fill the VLAN holes of the host rows, so the table and the sync writer read one derivation.
    lag_members, _sub_interfaces, _bridge_members = normalize_relationship_maps(relationships)
    apply_lag_vlan_fill(host_ports, lag_members, interface_name_field=interface_name_field)
    return {
        "ports": host_ports + oob_ports,
        "port_stack_relationships": relationships,
        "oob_incomplete": oob_incomplete,
    }
