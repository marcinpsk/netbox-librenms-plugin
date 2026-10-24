"""Assess raw module sources without HTTP, shared cache or writes."""

from dcim.models import Device

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes
from netbox_librenms_plugin.constants import MAIN_INVENTORY_SOURCE, OOB_INVENTORY_SOURCE, is_librenms_placeholder
from netbox_librenms_plugin.inventory_identity import vc_serial_key
from netbox_librenms_plugin.inventory_tree import repair_dangling_parents
from netbox_librenms_plugin.server_mappings import MappingRole
from netbox_librenms_plugin.utils import normalize_serial

_SKIP_TRANSCEIVER_TYPES = {"Port Container", "Port", ""}
TRANSCEIVER_API_MATCH = "_transceiver_api_match"


def assess(snapshot, *, subject):
    """Return the assessed inventory and its blocked scopes for the synchronization subject."""
    resource = Resource.MODULES
    blocked = blocked_scopes(snapshot, resource)
    if "host" in blocked:
        return {"inventory": [], "blocked": blocked}
    owner = Device.objects.select_related("device_type__manufacturer").get(pk=snapshot.resources[resource].owner.pk)
    inventory = snapshot.value(resource, "inventory")
    repair_dangling_parents(inventory)
    ports = snapshot.value(resource, "ports")
    _merge_transceiver_data(
        inventory, snapshot.value(resource, "transceivers"), ports, manufacturer=owner.device_type.manufacturer
    )
    for item in inventory:
        item["_source"] = MAIN_INVENTORY_SOURCE
    _enrich_inventory_port_identity(inventory, ports)
    if snapshot.resources[resource].oob is not None and "oob" not in blocked:
        oob = snapshot.value(resource, "inventory", MappingRole.OOB)
        maximum = max(
            (item["entPhysicalIndex"] for item in inventory if item.get("entPhysicalIndex") is not None), default=0
        )
        offset = ((maximum // 1000) + 1) * 1000
        repair_dangling_parents(oob)
        for item in oob:
            item["_source"] = OOB_INVENTORY_SOURCE
            if item.get("entPhysicalIndex") is not None:
                item["entPhysicalIndex"] += offset
            if item.get("entPhysicalContainedIn") not in (None, 0):
                item["entPhysicalContainedIn"] += offset
        inventory.extend(oob)
    for item in inventory:
        name = item.get("entPhysicalName")
        if name is not None and not isinstance(name, str):
            item["entPhysicalName"] = normalize_serial(name)
    return {"inventory": inventory, "blocked": blocked}


def clean_librenms_value(value) -> str:
    """Return trimmed text with LibreNMS placeholders removed."""
    text = normalize_serial(value)
    return "" if is_librenms_placeholder(text) else text


def _names_another_part(item: dict, transceiver: dict, serial_key) -> bool:
    """Return whether an entity and a transceiver both have a real serial and the serials differ."""
    item_serial = serial_key(item.get("entPhysicalSerialNum"))
    transceiver_serial = serial_key(transceiver.get("serial"))
    return bool(item_serial and transceiver_serial and item_serial != transceiver_serial)


def _ifindex_entity_is_the_optic(item: dict, transceiver: dict, serial_key) -> bool:
    """Return whether an entity matched only by port ifIndex has the optic's serial, or neither model nor serial."""
    if _names_another_part(item, transceiver, serial_key):
        return False
    item_serial = serial_key(item.get("entPhysicalSerialNum"))
    if item_serial:
        return item_serial == serial_key(transceiver.get("serial"))
    return not clean_librenms_value(item.get("entPhysicalModelName"))


def _merge_transceiver_data(inventory_data, transceivers, ports, *, manufacturer):  # noqa: C901
    """Merge transceiver facts into copied entity inventory."""
    if not transceivers:
        return inventory_data

    inv_by_index = {idx: item for item in inventory_data if (idx := item.get("entPhysicalIndex")) is not None}
    serial_key = vc_serial_key(manufacturer)
    inv_serials = {s for item in inventory_data if (s := serial_key(item.get("entPhysicalSerialNum")))}
    inv_by_ifindex = {}
    for item in inventory_data:
        if (if_index := int(item["ifIndex"]) if item.get("ifIndex") is not None else None) is not None:
            inv_by_ifindex[if_index] = None if if_index in inv_by_ifindex else item

    port_name_map = _build_port_name_map(transceivers, ports)

    for txr in transceivers:
        ent_idx = int(txr["entity_physical_index"])
        port_id = int(txr["port_id"]) if txr.get("port_id") not in (None, 0, "0") else None
        port_meta = port_name_map.get(port_id, {}) if port_id else {}
        ifname = (port_meta.get("ifName") or "").strip() or None
        ifdescr = (port_meta.get("ifDescr") or "").strip() or None
        port_ifindex = int(port_meta["ifIndex"]) if port_meta.get("ifIndex") is not None else None

        existing = inv_by_index.get(ent_idx)
        index_taken = existing is not None
        if existing is not None and (
            _names_another_part(existing, txr, serial_key)
            or (
                port_ifindex is not None
                and (int(existing["ifIndex"]) if existing.get("ifIndex") is not None else None)
                not in {None, port_ifindex}
            )
        ):
            existing = None
        if existing is None and port_ifindex is not None:
            by_ifindex = inv_by_ifindex.get(port_ifindex)
            if by_ifindex is not None and _ifindex_entity_is_the_optic(by_ifindex, txr, serial_key):
                existing = by_ifindex
        if existing is None and index_taken:
            continue

        model = clean_librenms_value(txr.get("model"))
        serial = clean_librenms_value(txr.get("serial"))
        serial_identity = serial_key(serial)
        txr_type = clean_librenms_value(txr.get("type"))

        if txr_type in _SKIP_TRANSCEIVER_TYPES and not model and not serial:
            continue

        display_model = model or (txr_type if txr_type not in _SKIP_TRANSCEIVER_TYPES else "")

        if existing is not None:
            existing[TRANSCEIVER_API_MATCH] = True
            existing_model = clean_librenms_value(existing.get("entPhysicalModelName"))
            if not existing_model and display_model:
                existing["entPhysicalModelName"] = display_model
            existing_serial = clean_librenms_value(existing.get("entPhysicalSerialNum"))
            if not existing_serial and serial:
                existing["entPhysicalSerialNum"] = serial
                if serial_identity:
                    inv_serials.add(serial_identity)
            if port_id:
                existing["_librenms_port_id"] = port_id
            if ifname:
                existing["_librenms_ifname"] = ifname
            if ifdescr:
                existing["_librenms_ifdescr"] = ifdescr
        else:
            if serial_identity and serial_identity in inv_serials:
                continue
            if ifname:
                name = ifname
            elif ifdescr:
                name = ifdescr
            elif port_id:
                name = f"Transceiver (port {port_id})"
            else:
                name = f"Transceiver {ent_idx}"

            synthetic = {
                "entPhysicalIndex": ent_idx,
                "entPhysicalName": name,
                "entPhysicalClass": "port",
                "entPhysicalModelName": display_model,
                "entPhysicalSerialNum": serial,
                "entPhysicalDescr": txr_type,
                "entPhysicalContainedIn": 0,
                "_from_transceiver_api": True,
                "_librenms_port_id": port_id,
                "_librenms_ifname": ifname,
                "_librenms_ifdescr": ifdescr,
            }
            inventory_data.append(synthetic)
            inv_by_index[ent_idx] = synthetic
            if serial_identity:
                inv_serials.add(serial_identity)

    _nest_synthetic_transceivers(inventory_data)

    return inventory_data


def _nest_synthetic_transceivers(inventory_data):
    """Set synthetic transceiver parents from their interface path."""
    name_to_index = {}
    for it in inventory_data:
        nm = normalize_serial(it.get("entPhysicalName"))
        if nm:
            name_to_index.setdefault(nm, it.get("entPhysicalIndex"))

    for it in inventory_data:
        if not it.get("_from_transceiver_api"):
            continue
        if it.get("entPhysicalContainedIn"):
            continue
        name = normalize_serial(it.get("entPhysicalName"))
        if not name or "/" not in name:
            continue
        parts = name.split("/")
        for i in range(len(parts) - 1, 0, -1):
            prefix = "/".join(parts[:i])
            parent_idx = None
            for cand_name, cand_idx in name_to_index.items():
                if cand_name == prefix or cand_name.endswith(" " + prefix) or cand_name.endswith("/" + prefix):
                    parent_idx = cand_idx
                    break
            if parent_idx and parent_idx != it.get("entPhysicalIndex"):
                it["entPhysicalContainedIn"] = parent_idx
                break


def _enrich_inventory_port_identity(inventory_data, ports):
    """Fill port identities from the obtained ports."""
    label_to_port_id = {}
    ambiguous_labels = set()
    port_meta_by_id = {}

    for port in ports:
        port_id = int(port["port_id"])

        ifname = (port.get("ifName") or "").strip()
        ifdescr = (port.get("ifDescr") or "").strip()
        port_meta_by_id[port_id] = {"ifName": ifname, "ifDescr": ifdescr}

        for label in [ifname, ifdescr]:
            if not label:
                continue
            existing = label_to_port_id.get(label)
            if existing is None:
                label_to_port_id[label] = port_id
            elif existing != port_id:
                ambiguous_labels.add(label)

    for label in ambiguous_labels:
        label_to_port_id.pop(label, None)

    for item in inventory_data:
        if normalize_serial(item.get("entPhysicalClass")).lower() != "port":
            continue

        port_id = item.get("_librenms_port_id")

        if port_id is None or port_id <= 0:
            candidates = interface_name_candidates(item)

            matched_ids = [label_to_port_id[label] for label in candidates if label in label_to_port_id]
            matched_ids = sorted(set(matched_ids))
            if len(matched_ids) != 1:
                continue
            port_id = matched_ids[0]
            item["_librenms_port_id"] = port_id

        meta = port_meta_by_id.get(port_id)
        if not meta:
            continue
        if meta.get("ifName") and not item.get("_librenms_ifname"):
            item["_librenms_ifname"] = meta["ifName"]
        if meta.get("ifDescr") and not item.get("_librenms_ifdescr"):
            item["_librenms_ifdescr"] = meta["ifDescr"]


def _build_port_name_map(transceivers, ports):
    """Return interface labels of transceiver ports."""
    port_ids = {int(row["port_id"]) for row in transceivers if row.get("port_id") not in (None, 0, "0")}
    return {
        int(port["port_id"]): {
            "ifName": port.get("ifName"),
            "ifDescr": port.get("ifDescr"),
            "ifIndex": port.get("ifIndex"),
        }
        for port in ports
        if int(port["port_id"]) in port_ids
    }


def interface_name_candidates(item):
    """Return unique interface label candidates for bay matching."""
    candidates = []
    for value in [
        item.get("_librenms_ifname"),
        item.get("_librenms_ifdescr"),
        item.get("entPhysicalName"),
        item.get("entPhysicalDescr"),
    ]:
        label = normalize_serial(value)
        if label and label not in candidates:
            candidates.append(label)
    return candidates
