from netbox.api.serializers import NetBoxModelSerializer

from netbox_librenms_plugin.models import DeviceTypeMapping, InterfaceTypeMapping


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
