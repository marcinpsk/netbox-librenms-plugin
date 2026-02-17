import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from netbox_librenms_plugin.models import DeviceTypeMapping, InterfaceTypeMapping


class InterfaceTypeMappingTable(NetBoxTable):
    """
    Table for displaying InterfaceTypeMapping data.
    """

    librenms_type = tables.Column(verbose_name="LibreNMS Type")
    librenms_speed = tables.Column(verbose_name="LibreNMS Speed (Kbps)")
    netbox_type = tables.Column(verbose_name="NetBox Type")
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for InterfaceTypeMappingTable."""

        model = InterfaceTypeMapping
        fields = (
            "id",
            "librenms_type",
            "librenms_speed",
            "netbox_type",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "librenms_type",
            "librenms_speed",
            "netbox_type",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class DeviceTypeMappingTable(NetBoxTable):
    """Table for displaying DeviceTypeMapping data."""

    librenms_hardware = tables.Column(verbose_name="LibreNMS Hardware", linkify=True)
    netbox_device_type = tables.Column(verbose_name="NetBox Device Type", linkify=True)
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for DeviceTypeMappingTable."""

        model = DeviceTypeMapping
        fields = (
            "id",
            "librenms_hardware",
            "netbox_device_type",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "librenms_hardware",
            "netbox_device_type",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}
