"""The cache identity and the owner of each LibreNMS source resource."""

from dataclasses import dataclass
from enum import StrEnum


class Resource(StrEnum):
    """A LibreNMS source that one acquisition obtains."""

    INTERFACES = "interfaces"
    CABLES = "cables"
    IP_ADDRESSES = "ipaddresses"
    MODULES = "modules"
    VLANS = "vlans"


class OwnerPolicy(StrEnum):
    """Which object owns a resource's snapshot."""

    # The Virtual Chassis member that synchronization uses, so every member shares one snapshot.
    VC_SYNC_OWNER = "vc_sync_owner"
    SUBJECT = "subject"


@dataclass(frozen=True)
class ResourceSpec:
    """Describe one resource's cache identity, owner and supported subjects."""

    data_type: str
    owner_policy: OwnerPolicy
    supports_virtual_machine: bool


RESOURCE_SPECS = {
    Resource.INTERFACES: ResourceSpec("ports", OwnerPolicy.VC_SYNC_OWNER, supports_virtual_machine=True),
    Resource.CABLES: ResourceSpec("links", OwnerPolicy.VC_SYNC_OWNER, supports_virtual_machine=False),
    Resource.IP_ADDRESSES: ResourceSpec("ip_addresses", OwnerPolicy.SUBJECT, supports_virtual_machine=True),
    Resource.MODULES: ResourceSpec("inventory", OwnerPolicy.VC_SYNC_OWNER, supports_virtual_machine=False),
    Resource.VLANS: ResourceSpec("vlans", OwnerPolicy.SUBJECT, supports_virtual_machine=False),
}
