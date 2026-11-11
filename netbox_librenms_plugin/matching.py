"""
Find the LibreNMS device that a NetBox device or VM matches. Matching proposes a link and writes nothing.

The lookup tries the primary IP address, the DNS name of the primary IP, and the object name, in that
order, and stops at the first probe that answers. Each probe records its input and its result, so a
caller can tell "LibreNMS has no such device" from "LibreNMS did not answer".
"""

import time
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from netbox_librenms_plugin.librenms_api import DEFAULT_API_TIMEOUT, HTTP_NOT_FOUND
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id

# The clock of every match deadline; a test sets it.
_clock = time.monotonic


class Candidate(StrEnum):
    """One NetBox fact that the lookup asks LibreNMS for, in precedence order."""

    PRIMARY_IP = "primary_ip"
    DNS_NAME = "dns_name"
    NAME = "name"


CANDIDATE_LABELS = {Candidate.PRIMARY_IP: "primary IP", Candidate.DNS_NAME: "DNS name", Candidate.NAME: "name"}


class ProbeResult(StrEnum):
    """What LibreNMS answered for one probe."""

    MATCH = "match"
    NO_MATCH = "no_match"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Probe:
    """One lookup request: the candidate, the value sent, and the answer."""

    candidate: Candidate
    value: str
    result: ProbeResult


@dataclass(frozen=True)
class LookupInputs:
    """The NetBox facts that the lookup reads from one object."""

    primary_ip: int | None
    primary_address: str | None
    dns_name: str | None
    name: str | None

    @classmethod
    def of(cls, obj, primary_ip=None):
        """Return the lookup inputs of *obj*; *primary_ip* replaces its primary IP with a row the caller locked."""
        primary_ip = primary_ip if primary_ip is not None else getattr(obj, "primary_ip", None)
        address = getattr(primary_ip, "address", None)
        return cls(
            primary_ip=primary_ip.pk if primary_ip is not None else None,
            primary_address=str(address.ip) if address is not None else None,
            dns_name=(primary_ip.dns_name or None) if primary_ip is not None else None,
            name=obj.name or None,
        )

    def values(self):
        """Return ``(candidate, value)`` for each input the object has, in precedence order."""
        values = (
            (Candidate.PRIMARY_IP, self.primary_address),
            (Candidate.DNS_NAME, self.dns_name),
            (Candidate.NAME, self.name),
        )
        return tuple((candidate, value) for candidate, value in values if value)


@dataclass(frozen=True)
class Match:
    """LibreNMS knows the object as device *librenms_id*; *candidate* found it."""

    librenms_id: int
    candidate: Candidate
    hostname: str | None
    sysname: str | None
    inputs: LookupInputs
    probes: tuple[Probe, ...]


@dataclass(frozen=True)
class NoMatch:
    """LibreNMS answered every probe, and no device matched."""

    inputs: LookupInputs
    probes: tuple[Probe, ...]


@dataclass(frozen=True)
class Unreachable:
    """A probe got no usable answer, so a later candidate cannot be trusted."""

    inputs: LookupInputs
    probes: tuple[Probe, ...]


@dataclass(frozen=True)
class NotChecked:
    """The deadline passed before the lookup finished."""

    inputs: LookupInputs
    probes: tuple[Probe, ...]


class Deadline:
    """A point in time after which matching sends no more probes."""

    def __init__(self, seconds):
        self._end = _clock() + seconds

    def remaining(self):
        """Return the seconds left, never less than zero."""
        return max(0.0, self._end - _clock())


def _text(value):
    return value if isinstance(value, str) and value else None


def _probe(api, value, timeout):
    """Ask LibreNMS for one device; return the result, and the device record of a match."""
    status, body = api.get_response(f"devices/{quote(value, safe='')}", timeout=timeout)
    if status == HTTP_NOT_FOUND:
        return ProbeResult.NO_MATCH, None
    if status is None or not 200 <= status < 300 or not isinstance(body, dict):
        return ProbeResult.UNREACHABLE, None
    devices = body.get("devices")
    if devices == []:
        return ProbeResult.NO_MATCH, None
    if not isinstance(devices, list) or not isinstance(devices[0], dict):
        return ProbeResult.UNREACHABLE, None
    if coerce_librenms_id(devices[0].get("device_id")) is None:
        return ProbeResult.UNREACHABLE, None
    return ProbeResult.MATCH, devices[0]


def find_librenms_match(obj, api, *, deadline=None):
    """
    Return the LibreNMS device that *obj* matches on the server of *api*. Read only.

    Args:
        obj (Device | VirtualMachine): The NetBox object.
        api (LibreNMSAPI): A client bound to the server.
        deadline (Deadline | None): Send no probe after it; a probe waits at most the time left.

    Returns:
        Match | NoMatch | Unreachable | NotChecked: The answer, with every probe it sent.

    """
    inputs = LookupInputs.of(obj)
    probes = []
    for candidate, value in inputs.values():
        timeout = DEFAULT_API_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, deadline.remaining())
            if timeout <= 0:
                return NotChecked(inputs, tuple(probes))
        result, device = _probe(api, value, timeout)
        probes.append(Probe(candidate, value, result))
        if result is ProbeResult.MATCH:
            return Match(
                librenms_id=coerce_librenms_id(device.get("device_id")),
                candidate=candidate,
                hostname=_text(device.get("hostname")),
                sysname=_text(device.get("sysName")),
                inputs=inputs,
                probes=tuple(probes),
            )
        if result is ProbeResult.UNREACHABLE:
            if deadline is not None and deadline.remaining() <= 0:
                return NotChecked(inputs, tuple(probes))
            return Unreachable(inputs, tuple(probes))
    return NoMatch(inputs, tuple(probes))
