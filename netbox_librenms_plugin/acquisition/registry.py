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
    permits_oob_only: bool = False
    serial_sensors: bool = False
    inventory_probes: bool = False

    def acquisition_options(self, *, include_serial, inventory_probes=False):
        """Return the options that this resource's acquirer accepts."""
        options = {"include_serial": include_serial} if self.serial_sensors else {}
        if self.inventory_probes:
            options["inventory_probes"] = inventory_probes
        return options


RESOURCE_SPECS = {
    Resource.INTERFACES: ResourceSpec("ports", OwnerPolicy.VC_SYNC_OWNER, supports_virtual_machine=True),
    Resource.CABLES: ResourceSpec(
        "links", OwnerPolicy.VC_SYNC_OWNER, supports_virtual_machine=False, permits_oob_only=True, serial_sensors=True
    ),
    Resource.IP_ADDRESSES: ResourceSpec("ip_addresses", OwnerPolicy.SUBJECT, supports_virtual_machine=True),
    Resource.MODULES: ResourceSpec(
        "inventory", OwnerPolicy.VC_SYNC_OWNER, supports_virtual_machine=False, inventory_probes=True
    ),
    Resource.VLANS: ResourceSpec("vlans", OwnerPolicy.SUBJECT, supports_virtual_machine=False),
}
