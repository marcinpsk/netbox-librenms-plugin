from netbox.api.serializers import NetBoxModelSerializer

from netbox_librenms_plugin.models import InterfaceTypeMapping


class InterfaceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize InterfaceTypeMapping model for REST API."""

    class Meta:
        """Meta options for InterfaceTypeMappingSerializer."""

        model = InterfaceTypeMapping
        fields = ["id", "librenms_type", "librenms_speed", "netbox_type", "description"]
