from netbox.api.serializers import NetBoxModelSerializer

from netbox_librenms_plugin.models import DeviceTypeMapping, InterfaceTypeMapping, ModuleBayMapping, ModuleTypeMapping


class InterfaceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize InterfaceTypeMapping model for REST API."""

    class Meta:
        """Meta options for InterfaceTypeMappingSerializer."""

        model = InterfaceTypeMapping
        fields = ["id", "librenms_type", "librenms_speed", "netbox_type", "description"]


class DeviceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize DeviceTypeMapping model for REST API."""

    class Meta:
        """Meta options for DeviceTypeMappingSerializer."""

        model = DeviceTypeMapping
        fields = ["id", "librenms_hardware", "netbox_device_type", "description"]


class ModuleTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize ModuleTypeMapping model for REST API."""

    class Meta:
        """Meta options for ModuleTypeMappingSerializer."""

        model = ModuleTypeMapping
        fields = ["id", "librenms_model", "netbox_module_type", "description"]


class ModuleBayMappingSerializer(NetBoxModelSerializer):
    """Serialize ModuleBayMapping model for REST API."""

    class Meta:
        """Meta options for ModuleBayMappingSerializer."""

        model = ModuleBayMapping
        fields = ["id", "librenms_name", "librenms_class", "netbox_bay_name", "description"]
