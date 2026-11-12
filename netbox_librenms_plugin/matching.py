"""
Find the LibreNMS device that a NetBox device or VM matches. Matching proposes a link and writes nothing.

The lookup always asks for both primary addresses, IPv4 first. Two addresses that name different devices
are no match. When no address names a device, the lookup asks for the DNS name of each primary IP (IPv4
first) and then the object name, and stops at the first that names a device. Each probe records its input
and its result, so a caller can tell "LibreNMS has no such device" from "LibreNMS did not answer".
"""

import time
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from netbox_librenms_plugin.ip_addressing import primary_ips
from netbox_librenms_plugin.librenms_api import DEFAULT_API_TIMEOUT, HTTP_NOT_FOUND
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id

# The clock of every match deadline; a test sets it.
_clock = time.monotonic


class Candidate(StrEnum):
    """One NetBox fact that the lookup asks LibreNMS for, in precedence order."""

    PRIMARY_IP4 = "primary_ip4"
    PRIMARY_IP6 = "primary_ip6"
    DNS_NAME4 = "dns_name4"
    DNS_NAME6 = "dns_name6"
    NAME = "name"


# Every lookup asks for each of these; a later candidate is asked only when none of them matched.
# The candidates of one pair are both asked; when they name two devices, the object matches none.
CANDIDATE_PAIRS = {
    Candidate.PRIMARY_IP4: frozenset({Candidate.PRIMARY_IP4, Candidate.PRIMARY_IP6}),
    Candidate.PRIMARY_IP6: frozenset({Candidate.PRIMARY_IP4, Candidate.PRIMARY_IP6}),
    Candidate.DNS_NAME4: frozenset({Candidate.DNS_NAME4, Candidate.DNS_NAME6}),
    Candidate.DNS_NAME6: frozenset({Candidate.DNS_NAME4, Candidate.DNS_NAME6}),
    Candidate.NAME: frozenset({Candidate.NAME}),
}

CANDIDATE_LABELS = {
    Candidate.PRIMARY_IP4: "primary IPv4",
    Candidate.PRIMARY_IP6: "primary IPv6",
    Candidate.DNS_NAME4: "DNS name",
    Candidate.DNS_NAME6: "DNS name",
    Candidate.NAME: "name",
}


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
class PrimaryIP:
    """One primary IP row that the lookup reads."""

    pk: int
    address: str
    dns_name: str | None


@dataclass(frozen=True)
class LookupInputs:
    """The NetBox facts that the lookup reads from one object."""

    primary_ip4: PrimaryIP | None
    primary_ip6: PrimaryIP | None
    name: str | None

    @classmethod
    def of(cls, obj, rows=None):
        """Return the lookup inputs of *obj*; *rows* replaces its primary IP rows with rows that the caller locked."""
        rows = primary_ips(obj) if rows is None else rows
        by_family = {row.address.version: PrimaryIP(row.pk, str(row.address.ip), row.dns_name or None) for row in rows}
        if len(by_family) != len(rows):
            raise ValueError(f"{obj} has two primary IPs of one address family.")
        return cls(primary_ip4=by_family.get(4), primary_ip6=by_family.get(6), name=obj.name or None)

    def values(self):
        """Return ``(candidate, value)`` for each input the object has, in precedence order."""
        ip4, ip6 = self.primary_ip4, self.primary_ip6
        dns_name4 = ip4.dns_name if ip4 is not None else None
        dns_name6 = ip6.dns_name if ip6 is not None else None
        values = (
            (Candidate.PRIMARY_IP4, ip4.address if ip4 is not None else None),
            (Candidate.PRIMARY_IP6, ip6.address if ip6 is not None else None),
            (Candidate.DNS_NAME4, dns_name4),
            # The same name gets the same answer, so it is asked once.
            (Candidate.DNS_NAME6, dns_name6 if dns_name6 != dns_name4 else None),
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
    """LibreNMS answered every probe, and no one device matched: none did, or the primary addresses disagree."""

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


def _decode(status, body):
    """Return the result of one probe answer, and the device record of a match."""
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
        deadline (Deadline | None): Send no probe after it, stop reading an answer at it, and use no answer
            that arrives after it.

    Returns:
        Match | NoMatch | Unreachable | NotChecked: The answer, with every probe it sent.

    """
    inputs = LookupInputs.of(obj)
    probes = []
    remaining = deadline.remaining if deadline is not None else lambda: DEFAULT_API_TIMEOUT
    found = None
    for candidate, value in inputs.values():
        if found is not None and candidate not in CANDIDATE_PAIRS[found[0]]:
            break
        answer = api.get_response_before(f"devices/{quote(value, safe='')}", remaining)
        # A late answer is never used: the request has already spent the time of the other rows.
        if answer is None or remaining() <= 0:
            return NotChecked(inputs, tuple(probes))
        result, device = _decode(*answer)
        probes.append(Probe(candidate, value, result))
        if result is ProbeResult.UNREACHABLE:
            return Unreachable(inputs, tuple(probes))
        if result is ProbeResult.MATCH:
            if found is None:
                found = candidate, device
            elif coerce_librenms_id(device.get("device_id")) != coerce_librenms_id(found[1].get("device_id")):
                return NoMatch(inputs, tuple(probes))
    if found is None:
        return NoMatch(inputs, tuple(probes))
    candidate, device = found
    return Match(
        librenms_id=coerce_librenms_id(device.get("device_id")),
        candidate=candidate,
        hostname=_text(device.get("hostname")),
        sysname=_text(device.get("sysName")),
        inputs=inputs,
        probes=tuple(probes),
    )
