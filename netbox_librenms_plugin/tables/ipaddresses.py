from itertools import repeat
from typing import NamedTuple

import django_tables2 as tables
from django.utils.html import format_html, mark_safe
from netbox.tables.columns import ToggleColumn

from netbox_librenms_plugin.assessment.ip_addresses import ViewBlock, shown_vrf_id
from netbox_librenms_plugin.constants import MATCHING, MISMATCHED, NOT_IN_NETBOX
from netbox_librenms_plugin.sync_filters import FilterColumn, FilterSpec
from netbox_librenms_plugin.utils import paginate_sync_table, rule_block_html, rule_block_label

# The sync-state filter value of each row status; every other status is unclassified.
_FILTER_SYNC_STATE = {"matched": MATCHING, "update": MISMATCHED, "sync": NOT_IN_NETBOX}

# The visible label of the button that creates the LibreNMS VRF of a row.
CREATE_VRF_LABEL = "VRF"

# The values a plain column shows as the table default ("—") instead of calling its render method.
_EMPTY_VALUES = (None, "")

# The status of a row that an object the actor cannot view blocks.
_VIEW_BLOCK_STATUS = {
    ViewBlock.ADDRESS: "An IP address you cannot view holds this address",
    ViewBlock.HELD_ON_HIDDEN_INTERFACE: "An IP address on an interface you cannot view holds this address",
    ViewBlock.INTERFACE: "Assigned on an interface you cannot view",
}


def offers_selection(record):
    """Return whether the writer can take the row: an IP mapping or a hidden object refuses it."""
    return not (record.get("ip_rule_block") or record.get("view_block"))


class _StatusCell(NamedTuple):
    """The status cell of one row: its sync-state filter value, the text it shows, and its markup."""

    sync_state: str | None
    label: str
    markup: str


# The marker of a selected row whose evidence changed since the user selected it.
_CHANGED_MARKER = format_html(
    '<i class="mdi mdi-alert-circle-outline text-warning ms-1" role="img" title="{0}" aria-label="{0}"></i>',
    "Changed since you selected it: select it again",
)


def _row_read_attribute(value):
    """Return a row read attribute for a row with a read URL, and leave a row without one bare."""
    return lambda record: value if record.get("row_read_url") else None


class IPAddressTable(tables.Table):
    """
    Table for displaying LibreNMS IP address data.

    Each row reads its checkbox state, its evidence token and its row read URL from the record, which
    ``sync_rows`` prepares from the selection draft.
    """

    def __init__(self, data, **kwargs):
        """
        Initialize IP address table.

        Args:
            data (list[dict]): The rows, which ``assess_snapshot`` identified over the whole snapshot.
            **kwargs: Other ``tables.Table`` options.

        """
        super().__init__(data, **kwargs)
        # A row renders the same on every page and in a row read: no position-dependent even/odd class.
        self._counter = repeat(0)
        # The paginator links state table.tab, so paging keeps the user on the IP Addresses tab.
        self.tab = "ipaddresses"
        # Give this table its own pagination namespace. configure() passes self.prefix to
        # paginate_sync_table() and get_table_paginate_count(), which key per_page on
        # "{prefix}per_page"; left empty, the IP table shares the generic per-page param with
        # other tabs instead of reading "ipaddresses_per_page". Orthogonal to self.tab. Matches
        # the cables/modules/interfaces/vlans tables; preserve an explicit caller override.
        if not self.prefix:
            self.prefix = "ipaddresses_"

    class Meta:
        """Meta options for IPAddressTable."""

        sequence = [
            "selection",
            "address",
            "prefix_length",
            "device",
            "interface_name",
            "vrf",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-ipaddress-table",
        }
        row_attrs = {
            "data-interface": lambda record: record["row_id"],
            "data-name": lambda record: record["ip_with_mask"],
            "data-mgmt-ip": lambda record: "true" if record.get("is_mgmt_ip") else "",
            "data-row-key": lambda record: record["row_id"],
            "data-evidence": lambda record: record.get("evidence_token") or None,
            "data-evidence-changed": lambda record: "true" if record.get("evidence_changed") else None,
            # sync_draft.js reads the row again with this request; the attributes stop the form's hx-* from applying.
            "hx-get": lambda record: record.get("row_read_url") or None,
            "hx-trigger": _row_read_attribute("librenms:row-read"),
            "hx-target": _row_read_attribute("this"),
            "hx-swap": _row_read_attribute("outerHTML"),
            "hx-sync": _row_read_attribute("this:replace"),
            "hx-params": _row_read_attribute("none"),
        }

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        attrs={
            "td": {"data-col": "selection"},
            "input": {"name": "select", "disabled": lambda record: None if offers_selection(record) else "disabled"},
        },
        accessor="row_id",
        checked=lambda value, record: record.get("selected"),
    )

    address = tables.Column(
        accessor="ip_address",
        verbose_name="IP Address",
        linkify=lambda record: record.get("ip_url"),
        attrs={"td": {"data-col": "address"}},
    )
    prefix_length = tables.Column(
        accessor="prefix_length",
        verbose_name="Prefix Length",
        attrs={"td": {"data-col": "prefix"}},
    )
    device = tables.Column(
        linkify=lambda record: record.get("device_url"),
        attrs={"td": {"data-col": "device"}},
    )
    interface_name = tables.Column(
        accessor="interface_name",
        verbose_name="Interface",
        linkify=lambda record: record.get("interface_url"),
        attrs={"td": {"data-col": "interface"}},
    )
    vrf = tables.TemplateColumn(
        template_code="""
        <div class="d-flex align-items-center gap-1">
            <select id="vrf_select_{{ record.row_id }}" class="form-select vrf-select" data-row-id="{{ record.row_id }}"{% if record.view_block %} disabled{% endif %}>
                <option value="">Global</option>
                {% for vrf in record.vrfs %}
                    <option value="{{ vrf.pk }}" {% if record.shown_vrf_id == vrf.pk %}selected{% endif %}>
                        {{ vrf.name }}
                    </option>
                {% endfor %}
            </select>
            {% if record.vrf_suggested_from %}
                <i class="mdi mdi-lightbulb-on-outline text-muted" role="img"
                   title="Suggested from LibreNMS VRF {{ record.vrf_suggested_from.name }} by {{ record.vrf_suggested_from.matched_by }} match"
                   aria-label="Suggested from LibreNMS VRF {{ record.vrf_suggested_from.name }} by {{ record.vrf_suggested_from.matched_by }} match"></i>
            {% endif %}
            {% if record.offers_vrf_create %}
                <button type="submit" class="btn btn-sm btn-outline-primary text-nowrap" name="create_vrf" value="{{ record.row_id }}"
                        formaction="{{ record.vrf_create_action }}"
                        data-confirm="Create NetBox VRF '{{ record.librenms_vrf.name }}' with {% if record.librenms_vrf.rd %}route distinguisher {{ record.librenms_vrf.rd }}{% else %}no route distinguisher{% endif %}? The IP address is not synced."
                        title="LibreNMS VRF {{ record.librenms_vrf.name }} is not in NetBox. Create it."
                        aria-label="Create NetBox VRF {{ record.librenms_vrf.name }}"><i class="mdi mdi-plus-thick" aria-hidden="true"></i> {{ create_vrf_label }}</button>
            {% endif %}
        </div>
        """,
        extra_context={"create_vrf_label": CREATE_VRF_LABEL},
        attrs={"td": {"data-col": "vrf"}},
        verbose_name="VRF",
    )
    status = tables.Column(
        verbose_name="Status",
        attrs={"td": {"data-col": "status"}},
    )

    def filter_spec(self):
        """Return the filter columns of this table, each with the text its cell shows."""
        columns = self.columns
        return FilterSpec(
            prefix=self.prefix,
            columns=(
                FilterColumn("address", str(columns["address"].header), self.text_address),
                FilterColumn("prefix", str(columns["prefix_length"].header), self.text_prefix),
                FilterColumn("device", str(columns["device"].header), self.text_device),
                FilterColumn("interface", str(columns["interface_name"].header), self.text_interface),
                FilterColumn("vrf", str(columns["vrf"].header), self.text_vrf, has_field=False),
                FilterColumn("status", str(columns["status"].header), self.text_status, has_field=False),
            ),
            sync_state=self.filter_sync_state,
        )

    def filter_sync_state(self, record):
        """Return the row's sync-state filter value, or None for a row no sync-state filter shows."""
        value = record.get("status")
        return None if value in _EMPTY_VALUES else self._status_cell(value, record).sync_state

    def _cell_text(self, value):
        """Return the text a plain cell shows for *value*: the table default for an empty value."""
        return self.default if value in _EMPTY_VALUES else str(value)

    def text_address(self, record):
        """Return the visible text of the address cell."""
        return self._cell_text(record.get("ip_address"))

    def text_prefix(self, record):
        """Return the visible text of the prefix length cell."""
        return self._cell_text(record.get("prefix_length"))

    def text_device(self, record):
        """Return the visible text of the device cell."""
        return self._cell_text(record.get("device"))

    def text_interface(self, record):
        """Return the visible text of the interface cell."""
        return self._cell_text(record.get("interface_name"))

    def text_vrf(self, record):
        """Return the visible text of the VRF cell: the option its select shows, and the create button label."""
        shown = shown_vrf_id(record)
        selected = next((vrf.name for vrf in record.get("vrfs") or () if vrf.pk == shown), "Global")
        return f"{selected} {CREATE_VRF_LABEL}" if self.offers_vrf_create(record) else selected

    @staticmethod
    def offers_vrf_create(record):
        """Return whether the VRF cell shows the button that creates the LibreNMS VRF."""
        return bool(record.get("vrf_create_url")) and not record.get("ip_rule_block")

    def render_vrf(self, column, record, table, value, bound_column, bound_row):
        """Render the VRF cell; the create button follows the rule that the text function reads."""
        offers = self.offers_vrf_create(record)
        # sync_rows builds the action with the page query that the row read carries, so both render one URL.
        action = record["vrf_create_action"] if offers else ""
        return column.render(
            record={
                **record,
                "offers_vrf_create": offers,
                "vrf_create_action": action,
                "shown_vrf_id": shown_vrf_id(record),
            },
            table=table,
            value=value,
            bound_column=bound_column,
            bound_row=bound_row,
        )

    def render_selection(self, value, bound_column, record):
        """Render the row checkbox, with a marker when the row changed after the user selected it."""
        checkbox = bound_column.column.render(value, bound_column, record)
        return format_html("{}{}", checkbox, _CHANGED_MARKER) if record.get("evidence_changed") else checkbox

    def text_status(self, record):
        """Return the visible text of the status cell: the action, the rule pill or the row state."""
        value = record.get("status")
        return self.default if value in _EMPTY_VALUES else self._status_cell(value, record).label

    @staticmethod
    def _status_cell(value, record):
        """Return the status cell of one row, which the render method and the filters both read."""
        if (view_block := record.get("view_block")) is not None:
            # Wins over every other state: the row carries no NetBox identity.
            text = _VIEW_BLOCK_STATUS[view_block]
            return _StatusCell(None, text, format_html('<span class="text-muted">{}</span>', text))
        state = _FILTER_SYNC_STATE.get(value)
        row_id = record.get("row_id", record.get("ip_with_mask"))
        if row_id is None:
            return _StatusCell(None, "Ambiguous source row", "Ambiguous source row")
        rule_block = record.get("ip_rule_block") or record.get("rule_block")
        if rule_block and value != "matched":
            return _StatusCell(None, rule_block_label(rule_block), rule_block_html(rule_block))
        if value == "update":
            markup = format_html(
                '<button type="submit" class="btn btn-sm btn-warning" name="sync_one" value="{}">'
                '<i class="mdi mdi-pencil" aria-hidden="true"></i> Update</button>',
                row_id,
            )
            return _StatusCell(state, "Update", markup)
        if value == "matched":
            return _StatusCell(
                state,
                "Synced",
                mark_safe('<span class="text-success"><i class="mdi mdi-check-circle"></i> Synced</span>'),
            )
        if record.get("interface_url"):
            markup = format_html(
                '<button type="submit" class="btn btn-sm btn-primary" name="sync_one" value="{}">'
                '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Create</button>',
                row_id,
            )
            return _StatusCell(state, "Create", markup)
        return _StatusCell(
            state, "Missing NetBox Object", mark_safe('<span class="text-muted">Missing NetBox Object</span>')
        )

    def render_status(self, value, record):
        """Render the status column with appropriate buttons or text styling."""
        return self._status_cell(value, record).markup

    def render_device(self, value, record):
        """Render the device column with a link if available."""
        if url := record.get("device_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_interface_name(self, value, record):
        """Render the interface column with a link if available."""
        if url := record.get("interface_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def configure(self, request):
        """Configure the table."""
        paginate_sync_table(self, request)
