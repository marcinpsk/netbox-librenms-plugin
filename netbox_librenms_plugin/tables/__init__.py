from .cables import LibreNMSCableTable
from .device_status import DeviceStatusTable
from .interfaces import LibreNMSInterfaceTable, LibreNMSVMInterfaceTable, VCInterfaceTable
from .ipaddresses import IPAddressTable
from .locations import SiteLocationSyncTable
from .mappings import InterfaceTypeMappingTable
from .vlans import LibreNMSVLANTable
from .VM_status import VMStatusTable

__all__ = [
    "DeviceStatusTable",
    "InterfaceTypeMappingTable",
    "IPAddressTable",
    "LibreNMSCableTable",
    "LibreNMSInterfaceTable",
    "LibreNMSVLANTable",
    "LibreNMSVMInterfaceTable",
    "SiteLocationSyncTable",
    "VCInterfaceTable",
    "VMStatusTable",
]
