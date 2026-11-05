"""One request builder and one contract per LibreNMS endpoint that acquisition reads."""

from dataclasses import dataclass
from typing import Callable

from netbox_librenms_plugin.acquisition.snapshot import Outcome, SourceRequest
from netbox_librenms_plugin.constants import (
    LIBRENMS_NO_IPS_MESSAGE,
    LIBRENMS_NO_LINKS_MESSAGES,
    LIBRENMS_NO_SENSORS_MESSAGE,
    LIBRENMS_NO_VRFS_MESSAGE,
    LIBRENMS_PORTS_COLUMNS,
)
from netbox_librenms_plugin.ip_addressing import parse_librenms_ip_entry
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
from netbox_librenms_plugin.utils import is_list_of_dicts

HTTP_NOT_FOUND = 404
_DEVICE_FIELDS = ("device_id", "ip", "os")


@dataclass(frozen=True)
class Decoded:
    """The outcome a contract gives one response, and the body and value to keep."""

    outcome: Outcome
    value: object = None
    reason: str = ""
    # The body to keep instead of the received one: an instance-wide route keeps this device's rows only.
    body: object = None


@dataclass(frozen=True)
class Endpoint:
    """A request and the contract that decides what its response means."""

    request: SourceRequest
    decode: Callable[[int | None, object], Decoded]
    extended_timeout: bool = False


def _is_success(status):
    return status is not None and 200 <= status < 300


def _message(body):
    message = body.get("message") if isinstance(body, dict) else None
    return message if isinstance(message, str) else ""


def _failed_status(status, body):
    if status is None:
        return Decoded(Outcome.FAILED, reason="The LibreNMS server could not be reached.")
    if _is_success(status):
        return Decoded(Outcome.FAILED, reason=_message(body) or "LibreNMS returned a malformed response.")
    return Decoded(Outcome.FAILED, reason=_message(body) or f"LibreNMS answered with HTTP {status}.")


def _rows_contract(field, *, empty_404_message=None, device_id=None):
    """Return a contract for an envelope whose ``field`` is a list of objects."""

    def decode(status, body):
        if not _is_success(status):
            if status == HTTP_NOT_FOUND and empty_404_message and empty_404_message in _message(body).lower():
                return Decoded(Outcome.OBTAINED, value=[])
            return _failed_status(status, body)
        rows = body.get(field) if isinstance(body, dict) else None
        if not is_list_of_dicts(rows) or body.get("status", "ok") != "ok":
            return _failed_status(status, body)
        if device_id is None:
            return Decoded(Outcome.OBTAINED, value=rows)
        # Keep only this device's rows: the route serves the whole LibreNMS instance.
        own_rows = [row for row in rows if str(row.get("device_id")) == str(device_id)]
        return Decoded(Outcome.OBTAINED, value=own_rows, body={"status": "ok", field: own_rows})

    return decode


def device(device_id):
    """Return the device record endpoint."""

    def decode(status, body):
        if not _is_success(status):
            return _failed_status(status, body)
        devices = body.get("devices") if isinstance(body, dict) else None
        if not isinstance(devices, list) or not devices or not isinstance(devices[0], dict):
            return _failed_status(status, body)
        kept = {key: devices[0][key] for key in _DEVICE_FIELDS if key in devices[0]}
        envelope = {"devices": [kept]}
        if "status" in body:
            envelope["status"] = body["status"]
        return Decoded(Outcome.OBTAINED, value=kept, body=envelope)

    return Endpoint(SourceRequest(f"devices/{device_id}"), decode)


def _is_vlan_id(value):
    """Return whether a value names a VLAN id: an int, or a string of ASCII digits."""
    if isinstance(value, str):
        return value.isascii() and value.isdigit()
    return isinstance(value, int) and not isinstance(value, bool)


def _has_usable_vlans(port):
    """Return whether every VLAN field of a port row names a VLAN id, so the assessment parses it."""
    if_vlan = port.get("ifVlan")
    if if_vlan not in (None, "") and not _is_vlan_id(if_vlan):
        return False
    vlans = port.get("vlans")
    return vlans is None or (
        isinstance(vlans, list) and all(isinstance(row, dict) and _is_vlan_id(row.get("vlan")) for row in vlans)
    )


def _optional_text(row, fields):
    return all(row.get(field) is None or isinstance(row[field], str) for field in fields)


def _is_usable_port_row(port):
    return coerce_librenms_id(port.get("port_id")) is not None and _optional_text(
        port, ("ifName", "ifDescr", "ifAlias")
    )


def ports(device_id, *, with_vlans):
    """Return the ports endpoint; a recording keys it on the path only."""
    contract = _rows_contract("ports")

    def decode(status, body):
        decoded = contract(status, body)
        if decoded.outcome is Outcome.OBTAINED and not all(_is_usable_port_row(port) for port in decoded.value):
            return Decoded(
                Outcome.FAILED, reason="LibreNMS returned a port row without a valid identity or name field."
            )
        # A VLAN reader never drops a VLAN silently: one unusable row makes its answer malformed.
        if (
            with_vlans
            and decoded.outcome is Outcome.OBTAINED
            and not all(_has_usable_vlans(port) for port in decoded.value)
        ):
            return Decoded(Outcome.FAILED, reason="LibreNMS returned a port row whose VLAN fields name no VLAN id.")
        return decoded

    params = (("columns", LIBRENMS_PORTS_COLUMNS),) + ((("with", "vlans"),) if with_vlans else ())
    return Endpoint(SourceRequest(f"devices/{device_id}/ports", params, key_params=False), decode)


def _is_port_pair(row):
    return isinstance(row, dict) and "high_port_id" in row and "low_port_id" in row


def port_stack(device_id):
    """Return the port_stack endpoint: only an absent or "ok" status with a list of port pairs is an answer."""

    def decode(status, body):
        if not _is_success(status) or not isinstance(body, dict):
            return _failed_status(status, body)
        reported = body.get("status")
        if reported is not None and (not isinstance(reported, str) or reported.lower() != "ok"):
            return _failed_status(status, body)
        mappings = body.get("mappings")
        if not isinstance(mappings, list) or not all(_is_port_pair(row) for row in mappings):
            return Decoded(Outcome.FAILED, reason="LibreNMS returned port_stack mappings that are not port pairs.")
        return Decoded(Outcome.OBTAINED, value=mappings)

    return Endpoint(SourceRequest(f"devices/{device_id}/port_stack"), decode)


def _is_usable_ip_row(row):
    """Return whether an IP row names its port by a plain id and holds an address that parses."""
    port_id = row.get("port_id")
    if isinstance(port_id, bool) or not isinstance(port_id, (int, str)):
        return False
    try:
        parse_librenms_ip_entry(row)
    except ValueError:
        return False
    return True


def ip_addresses(device_id):
    """Return the IP address endpoint; LibreNMS answers a device with no address with a 404."""
    contract = _rows_contract("addresses", empty_404_message=LIBRENMS_NO_IPS_MESSAGE)

    def decode(status, body):
        decoded = contract(status, body)
        # One unusable row makes the answer malformed, so a reader never drops a row silently.
        if decoded.outcome is Outcome.OBTAINED and not all(_is_usable_ip_row(row) for row in decoded.value):
            return Decoded(
                Outcome.FAILED, reason="LibreNMS returned an IP address row without a usable port or address."
            )
        return decoded

    return Endpoint(SourceRequest(f"devices/{device_id}/ip"), decode)


def vrfs(device_id):
    """Return the VRF endpoint, filtered to this device even when the server ignores its filter."""
    contract = _rows_contract("vrfs", empty_404_message=LIBRENMS_NO_VRFS_MESSAGE, device_id=device_id)

    def decode(status, body):
        decoded = contract(status, body)
        rows = body.get("vrfs") if isinstance(body, dict) else None
        if decoded.outcome is Outcome.OBTAINED and rows and any(row.get("device_id") is None for row in rows):
            return Decoded(Outcome.FAILED, reason="LibreNMS returned VRF rows without a device_id.")
        return decoded

    return Endpoint(
        SourceRequest("routing/vrf", (("hostname", str(device_id)),), key_params=False),
        decode,
    )


def vlans(device_id):
    """Return the instance-wide VLAN endpoint, kept to this device's rows."""
    return Endpoint(SourceRequest("resources/vlans"), _rows_contract("vlans", device_id=device_id))


def links(device_id):
    """Return the links endpoint, including LibreNMS's two empty-table 404 answers."""
    contract = _rows_contract("links")

    def decode(status, body):
        if status == HTTP_NOT_FOUND and any(
            message in _message(body).lower() for message in LIBRENMS_NO_LINKS_MESSAGES
        ):
            return Decoded(Outcome.OBTAINED, value=[])
        decoded = contract(status, body)
        if decoded.outcome is Outcome.OBTAINED and not all(_is_usable_link_row(link) for link in decoded.value):
            return Decoded(
                Outcome.FAILED, reason="LibreNMS returned a link row with an invalid identity or text field."
            )
        return decoded

    return Endpoint(SourceRequest(f"devices/{device_id}/links"), decode)


def _optional_link_id(value):
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return False
    return value in ("", 0, "0") or coerce_librenms_id(value) is not None


def _is_usable_link_row(link):
    return (
        coerce_librenms_id(link.get("local_port_id")) is not None
        and all(_optional_link_id(link.get(field)) for field in ("id", "remote_device_id", "remote_port_id"))
        and _optional_text(link, ("local_port", "remote_port", "remote_hostname", "protocol"))
    )


def sensors(device_id):
    """Return raw sensors for one device, before serial type recognition or projection."""

    def decode(status, body):
        if status is None:
            return Decoded(Outcome.FAILED, reason="Error connecting to LibreNMS: the server could not be reached.")
        if status == HTTP_NOT_FOUND:
            if LIBRENMS_NO_SENSORS_MESSAGE in _message(body).lower():
                return Decoded(Outcome.OBTAINED, value=[])
            return Decoded(Outcome.FAILED, reason="Sensors resource endpoint not found")
        if _is_success(status) and body is None:
            return Decoded(Outcome.FAILED, reason="Invalid JSON sensor envelope from LibreNMS.")
        if not _is_success(status) or not isinstance(body, dict) or body.get("status") != "ok":
            return _failed_status(status, body)
        field = "sensors" if "sensors" in body else "resources"
        rows = body.get(field)
        if not isinstance(rows, list):
            return Decoded(Outcome.FAILED, reason=_message(body) or "Unexpected response format: missing sensor list")
        if not is_list_of_dicts(rows):
            return Decoded(Outcome.FAILED, reason=_message(body) or "Unexpected response format: invalid sensor item")
        return _rows_contract(field, device_id=device_id)(status, body)

    return Endpoint(SourceRequest("resources/sensors"), decode, extended_timeout=True)
