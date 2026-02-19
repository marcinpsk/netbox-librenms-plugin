from netbox.api.serializers import NetBoxModelSerializer

from netbox_librenms_plugin.models import (
    DeviceTypeMapping,
    InterfaceNameRule,
    InterfaceTypeMapping,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
)


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


class InterfaceNameRuleSerializer(NetBoxModelSerializer):
    """Serialize InterfaceNameRule model for REST API."""

    class Meta:
        """Meta options for InterfaceNameRuleSerializer."""

        model = InterfaceNameRule
        fields = [
            "id",
            "module_type",
            "parent_module_type",
            "name_template",
            "channel_count",
            "channel_start",
            "description",
        ]


class NormalizationRuleSerializer(NetBoxModelSerializer):
    """Serialize NormalizationRule model for REST API."""

    class Meta:
        """Meta options for NormalizationRuleSerializer."""

        model = NormalizationRule
        fields = [
            "id",
            "scope",
            "manufacturer",
            "match_pattern",
            "replacement",
            "priority",
            "description",
        ]
