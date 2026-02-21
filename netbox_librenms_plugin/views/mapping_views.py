from netbox.views import generic
from utilities.views import register_model_view

from netbox_librenms_plugin.filters import (
    DeviceTypeMappingFilterSet,
    InterfaceTypeMappingFilterSet,
    ModuleBayMappingFilterSet,
    ModuleTypeMappingFilterSet,
)
from netbox_librenms_plugin.forms import (
    DeviceTypeMappingFilterForm,
    DeviceTypeMappingForm,
    DeviceTypeMappingImportForm,
    InterfaceTypeMappingFilterForm,
    InterfaceTypeMappingForm,
    InterfaceTypeMappingImportForm,
    ModuleBayMappingFilterForm,
    ModuleBayMappingForm,
    ModuleBayMappingImportForm,
    ModuleTypeMappingFilterForm,
    ModuleTypeMappingForm,
    ModuleTypeMappingImportForm,
)
from netbox_librenms_plugin.models import (
    DeviceTypeMapping,
    InterfaceTypeMapping,
    ModuleBayMapping,
    ModuleTypeMapping,
)
from netbox_librenms_plugin.tables.mappings import (
    DeviceTypeMappingTable,
    InterfaceTypeMappingTable,
    ModuleBayMappingTable,
    ModuleTypeMappingTable,
)
from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin


class InterfaceTypeMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """
    Provides a view for listing all `InterfaceTypeMapping` objects.
    """

    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable
    filterset = InterfaceTypeMappingFilterSet
    filterset_form = InterfaceTypeMappingFilterForm
    template_name = "netbox_librenms_plugin/interfacetypemapping_list.html"


class InterfaceTypeMappingCreateView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """
    Provides a view for creating a new `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


@register_model_view(InterfaceTypeMapping, "bulk_import", path="import", detail=False)
class InterfaceTypeMappingBulkImportView(LibreNMSPermissionMixin, generic.BulkImportView):
    """
    Provides a view for bulk importing `InterfaceTypeMapping` objects from CSV, JSON, or YAML.
    Supports three import methods: direct import, file upload, and data file.
    """

    queryset = InterfaceTypeMapping.objects.all()
    model_form = InterfaceTypeMappingImportForm


class InterfaceTypeMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """
    Provides a view for displaying details of a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingEditView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """
    Provides a view for editing a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


class InterfaceTypeMappingDeleteView(LibreNMSPermissionMixin, generic.ObjectDeleteView):
    """
    Provides a view for deleting a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingBulkDeleteView(LibreNMSPermissionMixin, generic.BulkDeleteView):
    """
    Provides a view for deleting multiple `InterfaceTypeMapping` objects.
    """

    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable


class InterfaceTypeMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """
    Provides a view for displaying the change log of a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


# --- DeviceTypeMapping views ---


class DeviceTypeMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all DeviceTypeMapping objects."""

    queryset = DeviceTypeMapping.objects.all()
    table = DeviceTypeMappingTable
    filterset = DeviceTypeMappingFilterSet
    filterset_form = DeviceTypeMappingFilterForm
    template_name = "netbox_librenms_plugin/devicetypemapping_list.html"


class DeviceTypeMappingCreateView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm


@register_model_view(DeviceTypeMapping, "bulk_import", path="import", detail=False)
class DeviceTypeMappingBulkImportView(LibreNMSPermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing DeviceTypeMapping objects."""

    queryset = DeviceTypeMapping.objects.all()
    model_form = DeviceTypeMappingImportForm


class DeviceTypeMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()


class DeviceTypeMappingEditView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm


class DeviceTypeMappingDeleteView(LibreNMSPermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()


class DeviceTypeMappingBulkDeleteView(LibreNMSPermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple DeviceTypeMapping objects."""

    queryset = DeviceTypeMapping.objects.all()
    table = DeviceTypeMappingTable


class DeviceTypeMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()


# --- ModuleTypeMapping views ---


class ModuleTypeMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all ModuleTypeMapping objects."""

    queryset = ModuleTypeMapping.objects.all()
    table = ModuleTypeMappingTable
    filterset = ModuleTypeMappingFilterSet
    filterset_form = ModuleTypeMappingFilterForm
    template_name = "netbox_librenms_plugin/moduletypemapping_list.html"


class ModuleTypeMappingCreateView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()
    form = ModuleTypeMappingForm


@register_model_view(ModuleTypeMapping, "bulk_import", path="import", detail=False)
class ModuleTypeMappingBulkImportView(LibreNMSPermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing ModuleTypeMapping objects."""

    queryset = ModuleTypeMapping.objects.all()
    model_form = ModuleTypeMappingImportForm


class ModuleTypeMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()


class ModuleTypeMappingEditView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()
    form = ModuleTypeMappingForm


class ModuleTypeMappingDeleteView(LibreNMSPermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()


class ModuleTypeMappingBulkDeleteView(LibreNMSPermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple ModuleTypeMapping objects."""

    queryset = ModuleTypeMapping.objects.all()
    table = ModuleTypeMappingTable


class ModuleTypeMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()


# --- ModuleBayMapping views ---


class ModuleBayMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all ModuleBayMapping objects."""

    queryset = ModuleBayMapping.objects.all()
    table = ModuleBayMappingTable
    filterset = ModuleBayMappingFilterSet
    filterset_form = ModuleBayMappingFilterForm
    template_name = "netbox_librenms_plugin/modulebaymapping_list.html"


class ModuleBayMappingCreateView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()
    form = ModuleBayMappingForm


@register_model_view(ModuleBayMapping, "bulk_import", path="import", detail=False)
class ModuleBayMappingBulkImportView(LibreNMSPermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing ModuleBayMapping objects."""

    queryset = ModuleBayMapping.objects.all()
    model_form = ModuleBayMappingImportForm


class ModuleBayMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()


class ModuleBayMappingEditView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()
    form = ModuleBayMappingForm


class ModuleBayMappingDeleteView(LibreNMSPermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()


class ModuleBayMappingBulkDeleteView(LibreNMSPermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple ModuleBayMapping objects."""

    queryset = ModuleBayMapping.objects.all()
    table = ModuleBayMappingTable


class ModuleBayMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()
