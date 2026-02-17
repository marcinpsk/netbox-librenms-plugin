import django_filters

from .models import DeviceTypeMapping, InterfaceTypeMapping


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
