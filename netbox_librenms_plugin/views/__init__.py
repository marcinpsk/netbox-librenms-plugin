"""
Module for initializing views for the NetBox LibreNMS plugin.
"""

from .base.cables_view import BaseCableTableView, SingleCableVerifyView
from .base.interfaces_view import BaseInterfaceTableView
from .base.ip_addresses_view import BaseIPAddressTableView, SingleIPAddressVerifyView
from .base.librenms_sync_view import BaseLibreNMSSyncView
from .base.modules_view import InstallBranchView, InstallModuleView
from .base.vlan_table_view import BaseVLANTableView
from .imports import (
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
from .mapping_views import (
    DeviceTypeMappingBulkDeleteView,
    DeviceTypeMappingBulkImportView,
    DeviceTypeMappingChangeLogView,
    DeviceTypeMappingCreateView,
    DeviceTypeMappingDeleteView,
    DeviceTypeMappingEditView,
    DeviceTypeMappingListView,
    DeviceTypeMappingView,
    InterfaceNameRuleBulkDeleteView,
    InterfaceNameRuleBulkImportView,
    InterfaceNameRuleChangeLogView,
    InterfaceNameRuleCreateView,
    InterfaceNameRuleDeleteView,
    InterfaceNameRuleEditView,
    InterfaceNameRuleListView,
    InterfaceNameRuleView,
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
from .object_sync import (
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
from .settings_views import LibreNMSSettingsView, TestLibreNMSConnectionView
from .status_check import DeviceStatusListView, VMStatusListView
from .sync.cables import SyncCablesView
from .sync.device_fields import (
    AssignVCSerialView,
    CreateAndAssignPlatformView,
    UpdateDeviceNameView,
    UpdateDevicePlatformView,
    UpdateDeviceSerialView,
    UpdateDeviceTypeView,
)
from .sync.devices import AddDeviceToLibreNMSView, UpdateDeviceLocationView
from .sync.interfaces import DeleteNetBoxInterfacesView, SyncInterfacesView
from .sync.ip_addresses import SyncIPAddressesView
from .sync.locations import SyncSiteLocationView
from .sync.vlans import SyncVLANsView
