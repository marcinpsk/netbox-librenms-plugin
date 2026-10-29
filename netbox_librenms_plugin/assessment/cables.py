"""Assess cable source facts with the current serial name rules, without acquisition or writes."""

import re
from collections import defaultdict

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import (
    Blocker,
    NEIGHBOUR_PORTS_PREFIX,
    cable_source_state,
    cable_port_source_ids,
    neighbor_scope_key,
)
from netbox_librenms_plugin.constants import (
    INTERFACE_NAME_FIELDS,
    MAIN_INVENTORY_SOURCE,
    OOB_INVENTORY_SOURCE,
)
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links
from netbox_librenms_plugin.utils import assign_cable_row_ids

_SUB_UNIT_RE = re.compile(r"^(?P<physical>.+)\.\d+$")
_PROTOCOL_PREFERENCE = ("lldp", "cdp")


def _neighbour_group_key(row):
    """Identify a local port and its remote device across discovery protocols."""
    local_port_id = coerce_librenms_id(row.get("local_port_id"))
    if local_port_id is None:
        return None
    remote_device = coerce_librenms_id(row.get("remote_device_id"))
    hostname = row.get("remote_device")
    if remote_device is not None:
        remote_identity = ("id", remote_device)
    elif isinstance(hostname, str) and hostname.strip():
        # The hostname lookup in get_device_by_id_or_name is name__iexact, so fold case here too.
        remote_identity = ("hostname", hostname.strip().casefold())
    else:
        return None
    return local_port_id, remote_identity


def _remote_endpoint_identity(row):
    """Identify the far end by its LibreNMS port, then its resolved NetBox interface."""
    port_key = remote_port_ref(row)
    if port_key is not None:
        return ("port", port_key)
    if interface_id := row.get("netbox_remote_interface_id"):
        return ("interface", interface_id)
    return None


def _endpoint_group_key(row):
    """Identify one link within its source namespace when endpoint evidence exists."""
    neighbour = _neighbour_group_key(row)
    endpoint = _remote_endpoint_identity(row)
    if neighbour is None or endpoint is None:
        return None
    return (row.get("_source"), neighbour, endpoint)


def remote_port_ref(link):
    """Return the matched neighbour port ID, then the advertised port ID."""
    port_key = coerce_librenms_id(link.get("remote_port_key"))
    return port_key if port_key is not None else coerce_librenms_id(link.get("remote_port_id"))


def port_record(port):
    """Copy the complete source port record, including its owner and deletion evidence."""
    return dict(port)


def _drop_masked_sub_units(rows):
    """Drop a sub-unit only when its physical port names the same neighbour beside it."""
    physical_by_group = {}
    for row in rows:
        remote_port = row["remote_port"]
        key = _neighbour_group_key(row)
        if key is not None and isinstance(remote_port, str) and not _SUB_UNIT_RE.match(remote_port):
            physical_by_group.setdefault(key, set()).add(remote_port)

    kept = []
    for row in rows:
        remote_port = row["remote_port"]
        match = _SUB_UNIT_RE.match(remote_port) if isinstance(remote_port, str) else None
        if match and match.group("physical") in physical_by_group.get(_neighbour_group_key(row), ()):
            continue
        kept.append(row)
    return kept


def _build_cable_port_name_maps(ports, interface_name_field, alt_name_field):
    """Index obtained port names by their canonical LibreNMS identities."""
    names = {}
    alternates = {}
    for port in ports:
        port_id = coerce_librenms_id(port["port_id"])
        name = port.get(interface_name_field)
        names[port_id] = name
        alternate = port.get(alt_name_field)
        if alternate and alternate != name:
            alternates[port_id] = alternate
    return names, alternates


def _port_records_by_id(ports):
    """Index all obtained port records by their canonical LibreNMS identities."""
    return {coerce_librenms_id(port["port_id"]): port_record(port) for port in ports}


def _collect_cable_links(links, name_map, alt_map, source):
    """Derive cable rows from validated links and mask sub-units within one source."""
    rows = []
    for link in links:
        port_id = coerce_librenms_id(link["local_port_id"])
        local_name = name_map.get(port_id)
        if local_name is None:
            local_name = link.get("local_port")
        rows.append(
            {
                "local_port": local_name,
                "local_port_alt": alt_map.get(port_id),
                "local_port_id": port_id,
                "link_id": coerce_librenms_id(link.get("id")),
                "protocol": link.get("protocol"),
                "remote_port": link.get("remote_port"),
                "remote_device": link.get("remote_hostname"),
                "remote_port_id": coerce_librenms_id(link.get("remote_port_id")),
                "remote_device_id": coerce_librenms_id(link.get("remote_device_id")),
                "_neighbour_ports_id": coerce_librenms_id(link.get("resolved_remote_device_id")),
                "_source": source,
            }
        )
    return _drop_masked_sub_units(rows)


def dedupe_protocol_duplicates(links_data):
    """Collapse proven endpoint duplicates after NetBox enrichment and preserve their protocols."""
    dropped = set()
    for positions in _endpoint_groups(links_data).values():
        if len(positions) < 2:
            continue
        survivor = _best_duplicate_row(links_data, positions)
        if absorbed := _absorbed_protocols(links_data, positions, survivor):
            links_data[survivor]["also_reported_by"] = absorbed
        dropped.update(position for position in positions if position != survivor)
    return [link for position, link in enumerate(links_data) if position not in dropped]


def _endpoint_groups(links_data):
    """Group row positions by the link they report (:func:`_endpoint_group_key`)."""
    endpoints = defaultdict(list)
    for position, link in enumerate(links_data):
        if (key := _endpoint_group_key(link)) is not None:
            endpoints[key].append(position)
    return endpoints


def _absorbed_protocols(links_data, positions, keeper):
    """Name the protocols the *keeper* row speaks for: its group's, minus its own."""
    # The endpoint contract permits a protocol string or an unknown protocol (None).
    keeper_protocol = links_data[keeper].get("protocol")
    return sorted(
        {
            protocol
            for position in positions
            if position != keeper and (protocol := links_data[position].get("protocol")) is not None
        }
        - ({keeper_protocol} if keeper_protocol is not None else set())
    )


def also_reported_by(links_data, row_id):
    """Return the other protocols for one source row without collapsing the rows."""
    position = next((index for index, link in enumerate(links_data) if link.get("row_id") == row_id), None)
    if position is None:
        return []
    key = _endpoint_group_key(links_data[position])
    if key is None:
        return []
    positions = _endpoint_groups(links_data).get(key, [])
    return _absorbed_protocols(links_data, positions, position)


def _best_duplicate_row(links_data, positions):
    """Pick the row to keep for one endpoint: resolved, then a usable port id, then LLDP."""

    def rank(position):
        link = links_data[position]
        protocol = link.get("protocol")
        return (
            not link.get("netbox_remote_interface_id"),
            coerce_librenms_id(link.get("remote_port_id")) is None,
            _PROTOCOL_PREFERENCE.index(protocol) if protocol in _PROTOCOL_PREFERENCE else len(_PROTOCOL_PREFERENCE),
            position,
        )

    return min(positions, key=rank)


def _remote_port_name_index(ports):
    """Index all neighbour ports by ID, and named ports by each unambiguous name."""
    by_port_id = {}
    by_name = {}
    ambiguous_names = set()
    for port in ports:
        port_id = coerce_librenms_id(port["port_id"])
        names = {field: port[field] for field in INTERFACE_NAME_FIELDS if port.get(field)}
        record = (port_id, names, port_record(port))
        by_port_id[port_id] = record
        for name in names.values():
            folded = name.casefold()
            if folded in ambiguous_names:
                continue
            previous = by_name.get(folded)
            if previous is not None and previous[0] != port_id:
                by_name.pop(folded)
                ambiguous_names.add(folded)
            else:
                by_name[folded] = record
    return by_port_id, by_name


def _attach_remote_port_aliases(links, ports_by_neighbour):
    """Attach a matched neighbour port's identity, rule inputs and alternate names."""
    indexes = {device_id: _remote_port_name_index(ports) for device_id, ports in ports_by_neighbour.items()}
    for link in links:
        device_id = coerce_librenms_id(link.get("remote_device_id"))
        if device_id is None:
            # A hostname-only link names the neighbour whose ports acquisition read for it, unless that scope is blocked.
            device_id = link.get("_neighbour_ports_id")
            if device_id not in indexes:
                continue
        by_port_id, by_name = indexes[device_id]
        remote_port = link.get("remote_port")
        matched = by_port_id.get(coerce_librenms_id(link.get("remote_port_id")))
        if matched is None and isinstance(remote_port, str) and remote_port:
            matched = by_name.get(remote_port.casefold())
        if matched is None:
            continue
        port_id, names, record = matched
        link["remote_port_key"] = port_id
        link["remote_port_record"] = record
        aliases = []
        for field in sorted(INTERFACE_NAME_FIELDS):
            name = names.get(field)
            if isinstance(name, str) and name and name != remote_port and name not in aliases:
                aliases.append(name)
        if aliases:
            link["remote_port_aliases"] = aliases


def _link_neighbour_id(row):
    """Return the neighbour a link row names: its LibreNMS ID, or the ID acquisition resolved from its hostname."""
    return row["remote_device_id"] if row["remote_device_id"] is not None else row.get("_neighbour_ports_id")


def _mapping_rows(snapshot, mapping, scope, blocked, interface_name_field):
    """Assess one ready mapping and omit the rows of each blocked neighbour."""
    resource = Resource.CABLES
    if mapping is None or scope in blocked:
        return []
    ports = snapshot.value(resource, "ports", mapping.role)
    alternate = "ifDescr" if interface_name_field == "ifName" else "ifName"
    name_map, alt_map = _build_cable_port_name_maps(ports, interface_name_field, alternate)
    source = MAIN_INVENTORY_SOURCE if scope == "host" else OOB_INVENTORY_SOURCE
    rows = _collect_cable_links(snapshot.value(resource, "links", mapping.role), name_map, alt_map, source)
    rows = [row for row in rows if neighbor_scope_key(scope, _link_neighbour_id(row)) not in blocked]
    local_records = _port_records_by_id(ports)
    for row in rows:
        record = local_records.get(row["local_port_id"])
        if record is not None:
            row["local_port_record"] = record
    neighbours = {
        int(part.removeprefix(NEIGHBOUR_PORTS_PREFIX)): snapshot.value(resource, part, mapping.role)
        for part, _key in mapping.parts
        if part.startswith(NEIGHBOUR_PORTS_PREFIX)
        and neighbor_scope_key(scope, int(part.removeprefix(NEIGHBOUR_PORTS_PREFIX))) not in blocked
    }
    _attach_remote_port_aliases(rows, neighbours)
    return rows


def assessed_port_records(snapshot):
    """Index every obtained host, OOB and neighbour port, including ports without links."""
    records = {}
    source = snapshot.resources[Resource.CABLES]
    for mapping in (source.host, source.oob):
        if mapping is None:
            continue
        for part, key in mapping.parts:
            if part != "ports" and not part.startswith(NEIGHBOUR_PORTS_PREFIX):
                continue
            response = snapshot.responses[key]
            if response.obtained:
                records.update(_port_records_by_id(response.value))
    return records


def assess(snapshot, *, subject, interface_name_field="ifName", include_serial=True):
    """Assess source facts once, with serial rows limited by the caller's visibility."""
    resource = Resource.CABLES
    source = snapshot.resources[resource]
    state = cable_source_state(snapshot)
    blocked = state.blocked_scopes
    if not include_serial:
        blocked.pop("serial", None)
    links = _mapping_rows(snapshot, source.host, "host", blocked, interface_name_field)
    links.extend(_mapping_rows(snapshot, source.oob, "oob", blocked, interface_name_field))
    if include_serial and state.serial is not None and state.serial.ready:
        links.extend(
            map_sensors_to_serial_links(
                state.serial.rows, device_id=source.owner.pk, sensor_types=state.serial.type_map
            )
        )
    blockers = tuple(dict.fromkeys(blocker for scope in blocked.values() for blocker in scope))
    rows = assign_cable_row_ids(links)
    identity_error = rows is None
    if identity_error:
        mapping = source.host if source.host is not None else source.oob
        path = snapshot.record(resource, "links", mapping.role).request.path
        blockers += (Blocker(path, f"{path} has cable rows with duplicate or invalid action identities"),)
    return {
        "links": [] if identity_error else rows,
        "blockers": blockers,
        "blocked_scopes": blocked,
        "identity_error": identity_error,
        "port_records": assessed_port_records(snapshot),
        "port_source_ids": cable_port_source_ids(snapshot),
    }
