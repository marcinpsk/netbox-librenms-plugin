"""Build cable source facts for tests that describe a displayed adjacency."""

import re

from dcim.models import Device

from netbox_librenms_plugin.acquisition.coordinator import resource_owner
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.models import SerialSensorTypePattern
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_cables_snapshot


def cable_rows(subject, server_key="default"):
    """Read the source snapshot through the same assessment as the cable views."""
    from netbox_librenms_plugin.views.base.cables_view import read_cable_assessment

    return read_cable_assessment(subject, server_key)


def seed_cable_rows(subject, rows, server_key="default"):
    """Store raw endpoint facts for adjacency fixtures and return the source snapshot.

    No assessed row enters the cache. Serial fixture identities follow the sensor
    identity, and their local names come from real serial type rules.
    """
    owner = resource_owner(subject, Resource.CABLES, server_key)
    if read_mapping(owner).own_id(server_key) is None:
        seed_mapping(owner, server_key, own=800000 + owner.pk)
    if any(row.get("_source") == "oob" for row in rows) and not read_mapping(owner).has_oob(server_key):
        seed_mapping(owner, server_key, oob=900000 + owner.pk)
    if owner is not subject and owner.pk == subject.pk and owner._meta.label_lower == subject._meta.label_lower:
        subject.refresh_from_db(fields=["custom_field_data"])
    host_id = read_mapping(owner).own_id(server_key)
    links = []
    ports = {}
    neighbours = {}
    sensors = []
    oob_links = []
    oob_ports = {}
    for row in rows:
        if row.get("_source") == "serial":
            number = re.search(r"(\d+)$", row["local_port"])
            assert number is not None, "A serial fixture needs a numbered local port."
            index = int(number.group())
            sensor_type = f"fixtureSerial{row['sensor_id']}"
            SerialSensorTypePattern.objects.update_or_create(
                sensor_type=sensor_type,
                defaults={"port_name_pattern": row["local_port"][: number.start()] + "{N}"},
            )
            row["local_port_id"] = f"serial:{row['sensor_id']}"
            row["sensor_index_int"] = index
            sensors.append(
                {
                    "device_id": host_id,
                    "sensor_id": row["sensor_id"],
                    "sensor_type": sensor_type,
                    "sensor_index": f"fixtureSerialStatus.{index}",
                    "sensor_descr": f"{row.get('remote_device') or ''} Status",
                    "sensor_deleted": 0,
                }
            )
            continue
        if row.get("remote_port_record") and not row.get("remote_device_id"):
            neighbour = Device.objects.filter(name=row.get("remote_device")).first()
            if neighbour is not None:
                if read_mapping(neighbour).own_id(server_key) is None:
                    seed_mapping(neighbour, server_key, own=700000 + neighbour.pk)
                row["remote_device_id"] = read_mapping(neighbour).own_id(server_key)
        link = {
            "id": row.get("link_id"),
            "local_port_id": row["local_port_id"],
            "local_port": row.get("local_port"),
            "remote_hostname": row.get("remote_device"),
            "remote_device_id": row.get("remote_device_id"),
            "remote_port": row.get("remote_port"),
            "remote_port_id": row.get("remote_port_id"),
            "protocol": row.get("protocol"),
        }
        port = {
            **(row.get("local_port_record") or {}),
            "port_id": row["local_port_id"],
            "ifName": row.get("local_port"),
            "ifDescr": row.get("local_port_alt") or row.get("local_port"),
        }
        if row.get("_source") == "oob":
            oob_links.append(link)
            if row.get("local_port_record", {}) is not None:
                oob_ports[port["port_id"]] = port
        else:
            links.append(link)
            if row.get("local_port_record", {}) is not None:
                ports[port["port_id"]] = port
        neighbour_id = row.get("remote_device_id")
        if neighbour_id:
            neighbour = neighbours.setdefault(neighbour_id, {})
            port_id = row.get("remote_port_key", row.get("remote_port_id"))
            if port_id:
                aliases = row.get("remote_port_aliases") or []
                neighbour[port_id] = {
                    **(row.get("remote_port_record") or {}),
                    "port_id": port_id,
                    "ifName": row.get("remote_port") or "",
                    "ifDescr": aliases[0] if aliases else row.get("remote_port") or "",
                }
    return seed_cables_snapshot(
        subject,
        links,
        list(ports.values()),
        neighbour_ports={device_id: list(values.values()) for device_id, values in neighbours.items()},
        oob_links=oob_links if read_mapping(owner).oob_id(server_key) is not None else None,
        oob_ports=list(oob_ports.values()) if read_mapping(owner).oob_id(server_key) is not None else None,
        sensors=sensors or None,
        server_key=server_key,
    )
