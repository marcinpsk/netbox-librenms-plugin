"""Views backing the LibreNMS sync tabs on Device and VM detail pages."""

from .cache_status import NetBoxOnlyInterfacesView, SyncCacheFragmentView, SyncCacheStatusView, SyncTabFilterView
from .devices import (
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    DeviceModuleTableView,
    DeviceVLANTableView,
    SingleModuleVerifyView,
)
from .vms import (
    VMInterfaceTableView,
    VMIPAddressTableView,
    VMLibreNMSSyncView,
)
