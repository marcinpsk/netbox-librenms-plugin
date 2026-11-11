from django.urls import reverse
from django_tables2 import Column
from virtualization.models import VirtualMachine
from virtualization.tables import VirtualMachineTable

from netbox_librenms_plugin.tables.device_status import render_link_status


class VMStatusTable(VirtualMachineTable):
    """Table for displaying virtual machine LibreNMS status."""

    librenms_status = Column(
        verbose_name="LibreNMS Status",
        empty_values=(),
        accessor="librenms_status",
        orderable=False,
    )

    def render_librenms_status(self, value, record):
        """Render the stored link, or the proposed match, with a link to the sync page."""
        sync_url = reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", kwargs={"pk": record.pk})
        return render_link_status(self, value, record, sync_url)

    class Meta(VirtualMachineTable.Meta):
        """Meta options for VMStatusTable."""

        model = VirtualMachine
        fields = (
            "pk",
            "name",
            "status",
            "cluster",
            "cluster_type",
            "cluster_group",
            "librenms_status",
        )
        default_columns = (
            "name",
            "status",
            "cluster",
            "cluster_type",
            "cluster_group",
            "librenms_status",
        )
