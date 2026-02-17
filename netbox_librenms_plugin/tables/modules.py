import django_tables2 as tables
from django.utils.html import format_html
from utilities.paginator import EnhancedPaginator

from netbox_librenms_plugin.utils import get_table_paginate_count


class LibreNMSModuleTable(tables.Table):
    """Table for displaying LibreNMS inventory items mapped to NetBox modules."""

    name = tables.Column(verbose_name="Name", attrs={"td": {"data-col": "name"}})
    model = tables.Column(verbose_name="Model", attrs={"td": {"data-col": "model"}})
    serial = tables.Column(verbose_name="Serial", attrs={"td": {"data-col": "serial"}})
    description = tables.Column(verbose_name="Description", attrs={"td": {"data-col": "description"}})
    item_class = tables.Column(verbose_name="Class", attrs={"td": {"data-col": "item_class"}})
    module_bay = tables.Column(verbose_name="Module Bay", attrs={"td": {"data-col": "module_bay"}})
    module_type = tables.Column(verbose_name="Module Type", attrs={"td": {"data-col": "module_type"}})
    status = tables.Column(verbose_name="Status", attrs={"td": {"data-col": "status"}})

    class Meta:
        attrs = {"class": "table table-hover object-list", "id": "librenms-module-table"}
        row_attrs = {"class": lambda record: record.get("row_class", "")}

    def __init__(self, *args, device=None, **kwargs):
        """Initialize table with optional device context."""
        self.device = device
        super().__init__(*args, **kwargs)
        self.tab = "modules"
        self.htmx_url = None
        self.prefix = "modules_"

    def configure(self, request):
        """Configure pagination settings."""
        paginate = {"paginator_class": EnhancedPaginator, "per_page": get_table_paginate_count(request, self.prefix)}
        tables.RequestConfig(request, paginate).configure(self)

    def render_name(self, value, record):
        """Render inventory item name."""
        return value or "-"

    def render_model(self, value, record):
        """Render model with link to module type if matched."""
        if not value or value == "-":
            return "-"
        if url := record.get("module_type_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_serial(self, value, record):
        """Render serial number."""
        return value or "-"

    def render_description(self, value, record):
        """Render description, truncated for display."""
        if not value:
            return "-"
        if len(value) > 60:
            return format_html('<span title="{}">{}&hellip;</span>', value, value[:57])
        return value

    def render_item_class(self, value, record):
        """Render the entPhysicalClass with an icon."""
        icons = {
            "module": "mdi-expansion-card",
            "powerSupply": "mdi-power-plug",
            "fan": "mdi-fan",
        }
        icon = icons.get(value, "mdi-card-outline")
        return format_html('<i class="mdi {} me-1"></i> {}', icon, value)

    def render_module_bay(self, value, record):
        """Render module bay with link if found in NetBox."""
        if not value or value == "-":
            return format_html('<span class="text-danger">No matching bay</span>')
        if url := record.get("module_bay_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_module_type(self, value, record):
        """Render module type match status."""
        if not value or value == "-":
            return format_html('<span class="text-warning">No matching type</span>')
        if url := record.get("module_type_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_status(self, value, record):
        """Render sync status with badge."""
        badge_classes = {
            "Installed": "bg-success",
            "Matched": "bg-info",
            "No Bay": "bg-warning",
            "No Type": "bg-warning",
            "Unmatched": "bg-secondary",
            "Serial Mismatch": "bg-danger",
        }
        badge_class = badge_classes.get(value, "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', badge_class, value)
