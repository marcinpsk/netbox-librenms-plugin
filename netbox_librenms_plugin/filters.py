import django_filters

from .models import InterfaceTypeMapping


class InterfaceTypeMappingFilterSet(django_filters.FilterSet):
    """Filter set for InterfaceTypeMapping model."""

    class Meta:
        """Meta options for InterfaceTypeMappingFilterSet."""

        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type", "description"]
