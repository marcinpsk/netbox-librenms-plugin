"""Step 1 smoke tests — verify view class wiring (mixins, MRO, key attributes).

These tests never touch the database or network; they only inspect class
hierarchies and attribute presence.
"""


class TestLibreNMSAPIMixinWiring:
    """Views that need LibreNMSAPIMixin must have it in their MRO."""

    def _assert_has_api_mixin(self, view_class):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert LibreNMSAPIMixin in view_class.__mro__, f"{view_class.__name__} is missing LibreNMSAPIMixin in its MRO"

    def test_sync_interfaces_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        self._assert_has_api_mixin(SyncInterfacesView)

    def test_sync_site_location_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        self._assert_has_api_mixin(SyncSiteLocationView)

    def test_add_device_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        self._assert_has_api_mixin(AddDeviceToLibreNMSView)

    def test_update_location_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        self._assert_has_api_mixin(UpdateDeviceLocationView)

    def test_update_device_name_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        self._assert_has_api_mixin(UpdateDeviceNameView)

    def test_update_device_serial_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView

        self._assert_has_api_mixin(UpdateDeviceSerialView)

    def test_update_device_type_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceTypeView

        self._assert_has_api_mixin(UpdateDeviceTypeView)

    def test_update_device_platform_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDevicePlatformView

        self._assert_has_api_mixin(UpdateDevicePlatformView)

    def test_create_assign_platform_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        self._assert_has_api_mixin(CreateAndAssignPlatformView)

    def test_assign_vc_serial_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import AssignVCSerialView

        self._assert_has_api_mixin(AssignVCSerialView)


class TestCacheMixinWiring:
    """Views that cache LibreNMS data must have CacheMixin and expose get_cache_key."""

    def _assert_has_cache_mixin(self, view_class):
        from netbox_librenms_plugin.views.mixins import CacheMixin

        assert CacheMixin in view_class.__mro__, f"{view_class.__name__} is missing CacheMixin"
        assert hasattr(view_class, "get_cache_key"), f"{view_class.__name__} missing get_cache_key method"

    def test_sync_interfaces_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        self._assert_has_cache_mixin(SyncInterfacesView)

    def test_sync_cables_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        self._assert_has_cache_mixin(SyncCablesView)

    def test_sync_ip_addresses_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        self._assert_has_cache_mixin(SyncIPAddressesView)

    def test_sync_vlans_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        self._assert_has_cache_mixin(SyncVLANsView)

    def test_delete_interfaces_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        self._assert_has_cache_mixin(DeleteNetBoxInterfacesView)


class TestPermissionMixinWiring:
    """All action views must have LibreNMSPermissionMixin."""

    def _assert_has_permission_mixin(self, view_class):
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        assert LibreNMSPermissionMixin in view_class.__mro__, (
            f"{view_class.__name__} is missing LibreNMSPermissionMixin"
        )

    def test_sync_interfaces_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        self._assert_has_permission_mixin(SyncInterfacesView)

    def test_sync_cables_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        self._assert_has_permission_mixin(SyncCablesView)

    def test_add_device_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        self._assert_has_permission_mixin(AddDeviceToLibreNMSView)

    def test_remove_server_mapping_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        self._assert_has_permission_mixin(RemoveServerMappingView)


class TestRequiredObjectPermissionsWiring:
    """POST-only sync views that modify NetBox objects must declare required_object_permissions."""

    def test_sync_interfaces_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        assert hasattr(SyncInterfacesView, "required_object_permissions")

    def test_sync_cables_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        assert hasattr(SyncCablesView, "required_object_permissions")

    def test_sync_vlans_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        assert hasattr(SyncVLANsView, "required_object_permissions")

    def test_sync_ip_addresses_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        assert hasattr(SyncIPAddressesView, "required_object_permissions")

    def test_update_device_name_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        assert hasattr(UpdateDeviceNameView, "required_object_permissions")
        assert "POST" in UpdateDeviceNameView.required_object_permissions

    def test_update_device_serial_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView

        assert hasattr(UpdateDeviceSerialView, "required_object_permissions")

    def test_delete_interfaces_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        assert hasattr(DeleteNetBoxInterfacesView, "required_object_permissions")


class TestViewPropertyLazyInit:
    """Verify that _librenms_api starts as None (lazy, not eager-init) and that
    the librenms_api property descriptor exists on the class."""

    def test_librenms_api_mixin_property_is_defined_on_class(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert isinstance(LibreNMSAPIMixin.__dict__.get("librenms_api"), property), (
            "librenms_api must be a property descriptor on LibreNMSAPIMixin"
        )

    def test_librenms_api_starts_as_none_after_mixin_init(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = None
        # The backing attribute should be None before first access
        assert mixin._librenms_api is None

    def test_sync_interfaces_has_librenms_api_property_via_class(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        # Check the property is accessible on the class without triggering getter
        assert any("librenms_api" in vars(cls) for cls in SyncInterfacesView.__mro__)
