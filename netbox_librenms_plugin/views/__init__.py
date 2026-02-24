"""
Module for initializing views for the NetBox LibreNMS plugin.
"""

# These are intentional re-exports for consumers of this package.  # noqa: F401
from .base.cables_view import BaseCableTableView, SingleCableVerifyView  # noqa: F401
from .base.interfaces_view import BaseInterfaceTableView  # noqa: F401
from .base.ip_addresses_view import BaseIPAddressTableView, SingleIPAddressVerifyView  # noqa: F401
from .base.librenms_sync_view import BaseLibreNMSSyncView  # noqa: F401
from .base.modules_view import InstallBranchView, InstallModuleView  # noqa: F401
from .base.vlan_table_view import BaseVLANTableView  # noqa: F401
from .imports import (  # noqa: F401
    BulkImportConfirmView,
    BulkImportDevicesView,
    DeviceClusterUpdateView,
    DeviceConflictActionView,
    DeviceRackUpdateView,
    DeviceRoleUpdateView,
    DeviceValidationDetailsView,
    DeviceVCDetailsView,
    LibreNMSImportView,
    SaveUserPrefView,
)
from .mapping_views import (  # noqa: F401
    DeviceTypeMappingBulkDeleteView,
    DeviceTypeMappingBulkImportView,
    DeviceTypeMappingChangeLogView,
    DeviceTypeMappingCreateView,
    DeviceTypeMappingDeleteView,
    DeviceTypeMappingEditView,
    DeviceTypeMappingListView,
    DeviceTypeMappingView,
    InterfaceTypeMappingBulkDeleteView,
    InterfaceTypeMappingBulkImportView,
    InterfaceTypeMappingChangeLogView,
    InterfaceTypeMappingCreateView,
    InterfaceTypeMappingDeleteView,
    InterfaceTypeMappingEditView,
    InterfaceTypeMappingListView,
    InterfaceTypeMappingView,
    ModuleBayMappingBulkDeleteView,
    ModuleBayMappingBulkImportView,
    ModuleBayMappingChangeLogView,
    ModuleBayMappingCreateView,
    ModuleBayMappingDeleteView,
    ModuleBayMappingEditView,
    ModuleBayMappingListView,
    ModuleBayMappingView,
    ModuleTypeMappingBulkDeleteView,
    ModuleTypeMappingBulkImportView,
    ModuleTypeMappingChangeLogView,
    ModuleTypeMappingCreateView,
    ModuleTypeMappingDeleteView,
    ModuleTypeMappingEditView,
    ModuleTypeMappingListView,
    ModuleTypeMappingView,
    NormalizationRuleBulkDeleteView,
    NormalizationRuleBulkImportView,
    NormalizationRuleChangeLogView,
    NormalizationRuleCreateView,
    NormalizationRuleDeleteView,
    NormalizationRuleEditView,
    NormalizationRuleListView,
    NormalizationRuleView,
)
from .object_sync import (  # noqa: F401
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    DeviceModuleTableView,
    DeviceVLANTableView,
    SaveVlanGroupOverridesView,
    SingleInterfaceVerifyView,
    SingleVlanGroupVerifyView,
    VerifyVlanSyncGroupView,
    VMInterfaceTableView,
    VMIPAddressTableView,
    VMLibreNMSSyncView,
)
from .settings_views import LibreNMSSettingsView, TestLibreNMSConnectionView  # noqa: F401
from .status_check import DeviceStatusListView, VMStatusListView  # noqa: F401
from .sync.cables import SyncCablesView  # noqa: F401
from .sync.device_fields import (  # noqa: F401
    AssignVCSerialView,
    CreateAndAssignPlatformView,
    UpdateDeviceNameView,
    UpdateDevicePlatformView,
    UpdateDeviceSerialView,
    UpdateDeviceTypeView,
)
from .sync.devices import AddDeviceToLibreNMSView, UpdateDeviceLocationView  # noqa: F401
from .sync.interfaces import DeleteNetBoxInterfacesView, SyncInterfacesView  # noqa: F401
from .sync.ip_addresses import SyncIPAddressesView  # noqa: F401
from .sync.locations import SyncSiteLocationView  # noqa: F401
from .sync.vlans import SyncVLANsView  # noqa: F401
