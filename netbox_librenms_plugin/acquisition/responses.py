"""One request builder and one contract per LibreNMS endpoint that acquisition reads."""

from dataclasses import dataclass
from typing import Callable

from netbox_librenms_plugin.acquisition.snapshot import Outcome, SourceRequest
from netbox_librenms_plugin.constants import LIBRENMS_NO_IPS_MESSAGE, LIBRENMS_NO_VRFS_MESSAGE, LIBRENMS_PORTS_COLUMNS
from netbox_librenms_plugin.ip_addressing import parse_librenms_ip_entry
from netbox_librenms_plugin.utils import is_list_of_dicts

HTTP_NOT_FOUND = 404


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
        return Decoded(Outcome.OBTAINED, value=devices[0])

    return Endpoint(SourceRequest(f"devices/{device_id}"), decode)


def ports(device_id, *, with_vlans):
    """Return the ports endpoint; a recording keys it on the path only."""
    params = (("columns", LIBRENMS_PORTS_COLUMNS),) + ((("with", "vlans"),) if with_vlans else ())
    return Endpoint(SourceRequest(f"devices/{device_id}/ports", params, key_params=False), _rows_contract("ports"))


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
