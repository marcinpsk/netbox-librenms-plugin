"""Views backing the LibreNMS sync tabs on Device and VM detail pages."""

from .devices import (  # noqa: F401
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    DeviceVLANTableView,
    SaveVlanGroupOverridesView,
    SingleInterfaceVerifyView,
    SingleVlanGroupVerifyView,
    VerifyVlanSyncGroupView,
)
from .vms import (  # noqa: F401
    VMInterfaceTableView,
    VMIPAddressTableView,
    VMLibreNMSSyncView,
)
