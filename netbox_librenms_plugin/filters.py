import django_filters

from .models import (
    DeviceTypeMapping,
    InterfaceNameRule,
    InterfaceTypeMapping,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
)


class InterfaceTypeMappingFilterSet(django_filters.FilterSet):
    """Filter set for InterfaceTypeMapping model."""

    class Meta:
        """Meta options for InterfaceTypeMappingFilterSet."""

        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type", "description"]


class DeviceTypeMappingFilterSet(django_filters.FilterSet):
    """Filter set for DeviceTypeMapping model."""

    class Meta:
        """Meta options for DeviceTypeMappingFilterSet."""

        model = DeviceTypeMapping
        fields = ["librenms_hardware", "description"]


class ModuleTypeMappingFilterSet(django_filters.FilterSet):
    """Filter set for ModuleTypeMapping model."""

    class Meta:
        """Meta options for ModuleTypeMappingFilterSet."""

        model = ModuleTypeMapping
        fields = ["librenms_model", "description"]


class ModuleBayMappingFilterSet(django_filters.FilterSet):
    """Filter set for ModuleBayMapping model."""

    class Meta:
        """Meta options for ModuleBayMappingFilterSet."""

        model = ModuleBayMapping
        fields = ["librenms_name", "librenms_class", "netbox_bay_name"]


class InterfaceNameRuleFilterSet(django_filters.FilterSet):
    """Filter set for InterfaceNameRule model."""

    class Meta:
        """Meta options for InterfaceNameRuleFilterSet."""

        model = InterfaceNameRule
        fields = ["module_type", "parent_module_type"]


class NormalizationRuleFilterSet(django_filters.FilterSet):
    """Filter set for NormalizationRule model."""

    class Meta:
        """Meta options for NormalizationRuleFilterSet."""

        model = NormalizationRule
        fields = ["scope"]
