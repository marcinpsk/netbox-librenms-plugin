from html import unescape

import django_tables2 as tables
from django.utils.html import conditional_escape, format_html, strip_tags
from django.utils.safestring import mark_safe
from netbox.tables.columns import ToggleColumn

from netbox_librenms_plugin.constants import SERIAL_INVENTORY_SOURCE
from netbox_librenms_plugin.constants import UNCLASSIFIED
from netbox_librenms_plugin.sync_filters import FilterColumn, FilterSpec
from netbox_librenms_plugin.utils import (
    oob_badge_html,
    paginate_sync_table,
    remote_port_html,
    VC_MEMBER_PLACEHOLDER,
    render_vc_member_options,
    rule_block_html,
    rule_block_label,
    selected_vc_member,
)

# Static trusted markup for the "Serial" console-port badge. The leading space is intentional.
SERIAL_BADGE_HTML = ' <span class="badge bg-teal text-dark ms-1" title="Serial console port">Serial</span>'

# The values a plain column shows as the table default ("—") instead of calling its render method.
_EMPTY_VALUES = (None, "")

# The label of the row action that syncs one cable.
SYNC_CABLE_LABEL = "Sync Cable"


def _visible_text(markup):
    """Return the text a browser shows for the markup of a cell; a plain string is escaped first, as the table does."""
    return unescape(strip_tags(conditional_escape(markup)))


class LibreNMSCableTable(tables.Table):
    """Table for displaying LibreNMS cable data."""

    selection = ToggleColumn(
        accessor="row_id",
        orderable=False,
        visible=True,
        attrs={
            "td": {"data-col": "selection"},
            "input": {
                "name": "select",
                "disabled": lambda record: None if record.get("can_create_cable") else "disabled",
            },
        },
    )

    local_port = tables.Column(verbose_name="Local Port", attrs={"td": {"data-col": "local_port"}})
    remote_port = tables.Column(
        accessor="remote_port_name",
        verbose_name="Remote Port",
        attrs={"td": {"data-col": "remote_port"}},
    )
    remote_device = tables.Column(verbose_name="Remote Device", attrs={"td": {"data-col": "remote_device"}})
    cable_status = tables.Column(verbose_name="Cable Status", attrs={"td": {"data-col": "cable_status"}})
    actions = tables.TemplateColumn(
        template_code="""
        {% if record.netbox_local_interface_id %}
            <input type="hidden"
                   name="expected_local_id_{{ record.row_id }}"
                   value="{{ record.netbox_local_interface_id }}">
        {% endif %}
        {% if record.netbox_local_device_id %}
            <input type="hidden"
                   name="expected_local_device_id_{{ record.row_id }}"
                   value="{{ record.netbox_local_device_id }}">
        {% endif %}
        {% if record.netbox_remote_interface_id %}
            <input type="hidden"
                   name="expected_remote_id_{{ record.row_id }}"
                   value="{{ record.netbox_remote_interface_id }}">
        {% endif %}
        {% if record.netbox_remote_device_id %}
            <input type="hidden"
                   name="expected_remote_device_id_{{ record.row_id }}"
                   value="{{ record.netbox_remote_device_id }}">
        {% endif %}
        {% if record.can_create_cable %}
            <button type="submit"
                    class="btn btn-sm btn-primary"
                    name="sync_one"
                    value="{{ record.row_id }}">
                {{ sync_cable_label }}
            </button>
        {% endif %}
        {% if record.remote_create_url %}
            {# Shares the cable tab's modal opener with the remote picker; the attribute names #}
            {# that loader, not the picker specifically. #}
            <button type="button"
                    class="btn btn-sm btn-outline-primary"
                    title="Create the remote interface and the cable"
                    aria-label="Create the remote interface and the cable"
                    data-cable-picker-url="{{ record.remote_create_url }}">
                <i class="mdi mdi-plus-network"></i>
            </button>
        {% endif %}
        {% if record.picker_url %}
            <button type="button"
                    class="btn btn-sm btn-outline-secondary"
                    title="Pick remote end"
                    aria-label="Pick remote end"
                    data-cable-picker-url="{{ record.picker_url }}">
                <i class="mdi mdi-connection"></i>
            </button>
        {% endif %}
        """,
        extra_context={"sync_cable_label": SYNC_CABLE_LABEL},
        verbose_name="",
        orderable=False,
        attrs={"td": {"data-col": "actions"}},
    )

    def __init__(self, *args, device=None, **kwargs):
        """
        Initialize table with optional device context.

        Rows arrive with the ``row_id`` the cable view assigned to the snapshot. Re-deriving it
        here would compute uniqueness over a list the view may already have filtered, so a row
        could be rendered under one identity and submitted under another.

        Args:
            *args: Positional table arguments (the row data).
            device: The page device, used for the virtual-chassis member selector.
            **kwargs: Keyword table arguments.

        """
        self.device = device
        super().__init__(*args, **kwargs)
        self.tab = "cables"
        self.prefix = "cables_"

    def filter_spec(self):
        """Return the filter columns of this table, each with the text its cell shows."""
        columns = self.columns
        return FilterSpec(
            prefix=self.prefix,
            columns=(
                *self._member_filter_columns(),
                FilterColumn("local_port", str(columns["local_port"].header), self.text_local_port),
                FilterColumn("remote_port", str(columns["remote_port"].header), self.text_remote_port),
                FilterColumn("remote_device", str(columns["remote_device"].header), self.text_remote_device),
                FilterColumn("cable_status", str(columns["cable_status"].header), self.text_cable_status, False),
                FilterColumn("actions", "Actions", self.text_actions, has_field=False),
            ),
            sync_state=self.filter_sync_state,
        )

    def _member_filter_columns(self):
        """Return the filter columns that only the virtual chassis variant shows."""
        return ()

    @staticmethod
    def filter_sync_state(record):
        """Return the row's comparison with the desired connection, or None for a row in no sync state."""
        comparison = record.get("cable_comparison")
        return None if comparison == UNCLASSIFIED else comparison

    def _rendered_text(self, record, accessor, render):
        """Return the visible text of a plain cell: the table default for an empty value, else its render."""
        value = record.get(accessor)
        return self.default if value in _EMPTY_VALUES else _visible_text(render(value, record))

    def text_local_port(self, record):
        """Return the visible text of the local port cell: the port name and its badges."""
        return self._rendered_text(record, "local_port", self.render_local_port)

    def text_remote_port(self, record):
        """Return the visible text of the remote port cell; its notes are icons with no visible text."""
        return self._rendered_text(record, "remote_port_name", self.render_remote_port)

    def text_remote_device(self, record):
        """Return the visible text of the remote device cell: the derived display name or the LibreNMS label."""
        return self._rendered_text(record, "remote_device", self.render_remote_device)

    def text_cable_status(self, record):
        """Return the visible text of the cable status cell."""
        return self._rendered_text(record, "cable_status", self.render_cable_status)

    def text_actions(self, record):
        """Return the visible text of the actions cell: the rule pill and the sync action; the icon buttons show none."""
        parts = []
        if record.get("rule_block"):
            parts.append(rule_block_label(record["rule_block"]))
        if record.get("can_create_cable"):
            parts.append(SYNC_CABLE_LABEL)
        return " ".join(parts)

    def render_remote_device(self, value, record):
        """Render the remote device (preferring the derived display name) as a link if available."""
        # remote_device_display carries the cable's real far end / a manual pick's device name;
        # the raw remote_device (LibreNMS label) stays pristine in the cache for re-enrichment.
        # Normalize None to "" like render_local_port — an unconfigured serial row with no
        # remote device name would otherwise render the literal "None" through format_html.
        display = record.get("remote_device_display") or value or ""
        if url := record.get("remote_device_url"):
            return format_html('<a href="{}">{}</a>', url, display)
        # Serial rows: dim unconfigured ports (label was never customised)
        if record.get("_source") == SERIAL_INVENTORY_SOURCE and not record.get("is_configured"):
            return format_html('<span class="text-muted fst-italic">{}</span>', display)
        return display

    def render_local_port(self, value, record):
        """Render local port name as a link if URL is available."""
        # Leading space: the badge follows the port name.
        oob_badge = oob_badge_html(record, leading_space=True)
        # Both badges are static trusted markup with no interpolation.
        serial_badge = mark_safe(SERIAL_BADGE_HTML) if record.get("_source") == SERIAL_INVENTORY_SOURCE else ""
        # Normalize None to "" in both branches; otherwise the linked branch
        # renders the literal "None" as the link text when value is missing.
        display_value = value or ""
        if url := record.get("local_port_url"):
            return format_html('<a href="{}">{}</a>{}{}', url, display_value, oob_badge, serial_badge)
        return format_html("{}{}{}", display_value, oob_badge, serial_badge)

    def render_remote_port(self, value, record):
        """Render remote port name as a link if URL is available; flag a manually picked remote."""
        # The one definition of this cell: the cable-verify formatter renders it too.
        return remote_port_html(value, record)

    def render_actions(self, column, record, table, value, bound_column, bound_row):
        """Render the row actions, led by the rule badge when the interface rules refuse the cable."""
        actions = column.render(record=record, table=table, value=value, bound_column=bound_column, bound_row=bound_row)
        if not record.get("rule_block"):
            return actions
        return format_html("{}{}", rule_block_html(record["rule_block"]), actions)

    def render_cable_status(self, value, record):
        """Render cable status as a link if cable URL is available."""
        if url := record.get("cable_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def configure(self, request):
        """Configure pagination for the table using the current request."""
        paginate_sync_table(self, request)

    class Meta:
        """Define column sequence, row attributes, and table styling."""

        sequence = [
            "selection",
            "local_port",
            "remote_port",
            "remote_device",
            "cable_status",
            "actions",
        ]
        row_attrs = {
            "data-interface": lambda record: record["row_id"],
            "data-device": lambda record: record["device_id"],
            "data-name": lambda record: record["local_port"],
        }
        attrs = {"class": "table table-hover object-list", "id": "librenms-cable-table"}


class VCCableTable(LibreNMSCableTable):
    """Table for displaying LibreNMS cable data for Virtual Chassis devices."""

    device_selection = tables.Column(
        verbose_name="Virtual Chassis Member",
        accessor="row_id",
        attrs={"td": {"class": "device-selection-col", "data-col": "device_selection"}},
    )

    def __init__(self, *args, device=None, chassis, **kwargs):
        """Initialize the VC cable table with device context and its chassis scope."""
        super().__init__(*args, device=device, **kwargs)
        # The member dropdowns offer only the members the user may view.
        self._vc_members = list(chassis.visible)
        # Done here, not in a render method: the selection checkbox reads can_create_cable and
        # its column is sequenced BEFORE device_selection, so a render-time flip would arrive
        # after the checkbox had already been drawn enabled.
        self._disable_serial_rows_owned_outside_the_chassis(args[0] if args else kwargs.get("data") or [])

    def _disable_serial_rows_owned_outside_the_chassis(self, rows):
        """
        Take serial rows out of play when their owning device is not selectable.

        The sync view matches the submitted device against the ConsoleServerPort's own device and
        rejects a mismatch, so a row whose owner is missing from the member set can never be
        synced from here. Offering it would fail after the click.
        """
        member_ids = {str(member.pk) for member in self._vc_members}
        for row in rows:
            if not isinstance(row, dict) or row.get("_source") != SERIAL_INVENTORY_SOURCE:
                continue
            owner_id = row.get("device_id")
            if owner_id and str(owner_id) not in member_ids:
                row["can_create_cable"] = False

    def _member_filter_columns(self):
        """Return the member column, whose text is the option its select shows."""
        return (FilterColumn("vc_member", str(self.columns["device_selection"].header), self.text_vc_member),)

    def _row_member_id(self, record):
        """
        Select the member that owns the row's local port, as the view attributed it.

        A row owned by a member the user may not view selects none; a row without an owner the
        user may view selects the table device.
        """
        if record.get("member_withheld"):
            return None
        owner_id = (
            record.get("device_id")
            if record.get("_source") == SERIAL_INVENTORY_SOURCE
            else record.get("local_owner_id")
        )
        if owner_id and selected_vc_member(self._vc_members, owner_id) is not None:
            return owner_id
        return self.device.id

    def text_vc_member(self, record):
        """Return the visible text of the member cell: the selected option, or the empty choice."""
        selected = selected_vc_member(self._vc_members, self._row_member_id(record))
        return selected.name if selected is not None else VC_MEMBER_PLACEHOLDER

    def render_device_selection(self, value, record):
        """Render a dropdown to select the virtual chassis member for a port."""
        serial_owner_id = record.get("device_id") if record.get("_source") == SERIAL_INVENTORY_SOURCE else None
        selected_member_id = self._row_member_id(record)
        row_id = record["row_id"]

        if serial_owner_id:
            return format_html(
                '<select id="device_selection_{0}" class="form-select" data-interface="{0}" data-row-id="{0}" disabled aria-disabled="true">{1}</select>'
                '<input type="hidden" name="device_selection_{0}" value="{2}">',
                row_id,
                render_vc_member_options(self._vc_members, selected_member_id),
                # The port's own device, never a substitute member: the sync view compares this
                # against the ConsoleServerPort's device and rejects anything else.
                serial_owner_id,
            )

        # A row on a member the user may not view cannot be moved to another member from here.
        disabled = mark_safe(' disabled aria-disabled="true"') if record.get("member_withheld") else ""
        return format_html(
            '<select name="device_selection_{0}" id="device_selection_{0}" class="form-select" data-interface="{0}" data-row-id="{0}"{2}>{1}</select>',
            row_id,
            render_vc_member_options(self._vc_members, selected_member_id),
            disabled,
        )

    class Meta(LibreNMSCableTable.Meta):
        """Define column sequence and attributes for the VC cable table."""

        sequence = [
            "selection",
            "device_selection",
            "local_port",
            "remote_port",
            "remote_device",
            "cable_status",
            "actions",
        ]
        row_attrs = {
            "data-interface": lambda record: record["row_id"],
            "data-device": lambda record: record["device_id"],
            "data-name": lambda record: record["local_port"],
            "id": lambda record: record["row_id"],
        }
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-cable-table-vc",
        }
