"""
The one row renderer of the converted sync tabs, and the selection draft each of their rows reads.

A converted tab builds its table from the whole assessed snapshot and the user's selection draft. The tab
render and the row read call the same builder, and both render the rows through ``ROWS_TEMPLATE``, so a
row read answers the row exactly as the table shows it. The eligibility, the checkbox state and the
evidence of each row are computed at every render from the draft and the current assessment, never stored.
"""

from dataclasses import replace

from django.template.loader import render_to_string
from django.urls import reverse

from netbox_librenms_plugin.assessment.ip_addresses import apply_vrf_choices, assess, shown_vrf_id
from netbox_librenms_plugin.selection_draft import (
    Assignment,
    DraftConflict,
    Intent,
    SelectionDraft,
    evidence_changed,
    sign_evidence,
)
from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.sync_filters import filter_sync_table, selection_scope
from netbox_librenms_plugin.sync_query import SyncQuery
from netbox_librenms_plugin.sync_view_scope import SyncViewScope, parse_vrf_id
from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable, offers_selection
from netbox_librenms_plugin.utils import get_interface_name_field, resolve_set_primary_ip

IP_TAB = SyncTab.IP_ADDRESSES
# A device and a virtual machine show one IP table; the draft key names the subject's model.
IP_TABLE_VARIANT = "ipaddresses"
# The draft choice that holds a row's VRF: "" is Global, else the VRF pk.
VRF_CHOICE = "vrf"
ROWS_TEMPLATE = "netbox_librenms_plugin/inc/sync_table_rows.html"
# The header of every draft command answer: the draft revision after the command.
REVISION_HEADER = "X-Draft-Revision"
# The HX-Trigger events that tell the page's draft writer to read one row, or the tab's results, again.
ROW_STALE_EVENT = "lnms-row-stale"
RESULTS_STALE_EVENT = "lnms-results-stale"


def read_ip_assessment(request, obj, snapshot, server_key, vrf_choices=None):
    """
    Assess the snapshot for the request user, decide each chosen row in its VRF, and add the URLs the web readers show.

    Args:
        request (HttpRequest): The request; its user and its name field decide the rows.
        obj (Device | VirtualMachine): The subject of the sync page.
        snapshot (SourceSnapshot): The stored IP snapshot.
        server_key (str): The LibreNMS server of the snapshot.
        vrf_choices (dict[str, int | None] | None): The chosen VRF pk of each row ID; None is Global.

    Returns:
        IPAssessment: The assessment; each row with its URLs and the pk of its NetBox interface.

    """
    result = assess(
        snapshot, subject=obj, actor=request.user, interface_name_field=get_interface_name_field(request, obj)
    )
    rows = result.rows
    if vrf_choices:
        rows = apply_vrf_choices(rows, vrf_choices, SyncViewScope.for_request(request.user, obj, server_key))
    create_vrf_url = reverse(
        "plugins:netbox_librenms_plugin:create_ip_row_vrf",
        kwargs={"object_type": obj._meta.model_name, "pk": obj.pk},
    )
    for row in rows:
        row["device_url"] = obj.get_absolute_url()
        if ip := row.pop("netbox_ip", None):
            row["ip_url"] = ip.get_absolute_url()
        if interface := row.pop("netbox_interface", None):
            row["interface_url"] = interface.get_absolute_url()
            row["interface_id"] = interface.pk
        if row.get("can_create_vrf"):
            row["vrf_create_url"] = create_vrf_url
    return replace(result, rows=rows)


def open_ip_draft(request, obj, server_key, snapshot):
    """Return the request user's IP draft for the snapshot, in the tab's selection scope."""
    scope = selection_scope(request, obj, server_key, IP_TAB.value)
    return SelectionDraft.open(request.user, obj, IP_TAB, IP_TABLE_VARIANT, server_key, scope, snapshot.snapshot_id)


def ip_evidence(obj, server_key, row):
    """
    Return the facts that selecting one IP row approves.

    The writer acts on the row, its VRF, its NetBox address and interface and its state, so a change of
    any of them asks the user to select the row again. The subject and the server bind the token to its tab.
    """
    return {
        "subject": [obj._meta.label_lower, obj.pk],
        "server": server_key,
        "row": row["row_id"],
        "vrf": shown_vrf_id(row),
        "status": row.get("status"),
        "ip": row.get("netbox_ip_id"),
        "interface": row.get("interface_id"),
        "selectable": offers_selection(row),
    }


def draft_vrf_choices(entries):
    """Return the chosen VRF pk of each row of the draft *entries*; None is Global."""
    return {
        row_key: parse_vrf_id(entry.choices[VRF_CHOICE])
        for row_key, entry in entries.items()
        if VRF_CHOICE in entry.choices
    }


def ip_rows(request, obj, server_key, snapshot, entries):
    """Return the assessment with the VRF choices of the draft *entries*, and the evidence payload of each row."""
    assessment = read_ip_assessment(request, obj, snapshot, server_key, draft_vrf_choices(entries))
    for row in assessment.rows:
        row["evidence"] = ip_evidence(obj, server_key, row)
    return assessment


def selectable(row):
    """Return whether the user can select the row: it has an identity and the writer takes it."""
    return row["row_id"] is not None and offers_selection(row)


def selection_problem(entry, row):
    """
    Return why a selected draft entry cannot be submitted as approved, or None when it can.

    Returns:
        str | None: ``"pending"`` when the entry approves no evidence, ``"changed"`` when the row is gone,
            cannot be selected now, or its evidence is not the evidence the user approved.

    """
    if entry.evidence is None:
        return "pending"
    if row is None or not selectable(row) or evidence_changed(entry.evidence, row["evidence"]):
        return "changed"
    return None


def management_assignments(rows, selected):
    """Return the assignments that select, or clear, the management rows that Set Primary IP takes."""
    if not selected:
        return {row["row_id"]: Assignment(Intent.NONE) for row in rows if row.get("is_mgmt_ip") and row["row_id"]}
    return {
        row["row_id"]: Assignment(Intent.ON, row["evidence"])
        for row in rows
        if row.get("is_mgmt_ip") and selectable(row)
    }


def seed_management_rows(request, assessment, draft):
    """
    Select the management rows in a new draft while Set Primary IP is on, once per draft.

    Returns:
        bool: False when another request changed the new draft first; the caller opens the draft again.

    """
    if draft.revision or not resolve_set_primary_ip(request):
        return True
    try:
        draft.assign(draft.draft_id, 0, management_assignments(assessment.rows, True))
    except DraftConflict:
        return False
    return True


def _row_url(query, obj, server_key, row_key):
    path = reverse(
        "plugins:netbox_librenms_plugin:ip_row",
        kwargs={"object_type": obj._meta.model_name, "pk": obj.pk, "row_key": row_key},
    )
    return query.url(path, {"server_key": server_key})


def _show_selection(request, obj, server_key, rows, entries):
    """Add to each row its checkbox state, its evidence token, its row read URL and its Create VRF action."""
    query = SyncQuery.from_request(request)
    for row in rows:
        if row.get("vrf_create_url"):
            row["vrf_create_action"] = query.url(row["vrf_create_url"], {"server_key": server_key})
        entry = entries.get(row["row_id"])
        on = entry is not None and entry.intent == Intent.ON and selectable(row)
        changed = on and selection_problem(entry, row) is not None
        row["selected"] = on and not changed
        row["evidence_changed"] = changed
        row["evidence_token"] = sign_evidence(IP_TAB, row["evidence"]) if selectable(row) else ""
        row["row_read_url"] = _row_url(query, obj, server_key, row["row_id"]) if row["row_id"] else ""


def _bind_draft(table, request, obj, server_key, draft):
    """Give the table the draft it shows and the URLs of the draft commands, for the page's draft writer."""
    query = SyncQuery.from_request(request)
    kwargs = {"object_type": obj._meta.model_name, "pk": obj.pk}
    for attribute, name in (
        ("data-selection-url", "ip_selection"),
        ("data-choice-url", "ip_selection_choice"),
        ("data-management-url", "ip_selection_management"),
    ):
        path = reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs=kwargs)
        table.attrs[attribute] = query.url(path, {"server_key": server_key})
    table.attrs["data-draft-id"] = draft.draft_id
    table.attrs["data-draft-revision"] = str(draft.revision)


def ip_table(request, obj, server_key, assessment, draft):
    """
    Return the IP tab's table of the whole snapshot, as the draft selects it.

    Args:
        request (HttpRequest): The request; its query holds the filters, the page and the sort.
        obj (Device | VirtualMachine): The subject of the sync page.
        server_key (str): The LibreNMS server of the snapshot.
        assessment (IPAssessment): The rows of :func:`ip_rows` for the draft's entries.
        draft (SelectionDraft): The draft the rows show.

    Returns:
        tuple[IPAddressTable, SyncFilters, int | None]: The table, its filters, and the row count before them.

    """
    _show_selection(request, obj, server_key, assessment.rows, draft.entries())
    table, filters, filtered_from = filter_sync_table(request, assessment.rows, IPAddressTable)
    _bind_draft(table, request, obj, server_key, draft)
    table.configure(request)
    return table, filters, filtered_from


def ip_row_html(request, obj, server_key, snapshot, draft, row_key):
    """
    Return the ``<tr>`` of one IP row as the tab's table renders it, or None when no current row has *row_key*.

    The row is assessed with the whole snapshot and the draft, as in the tab.
    """
    entries = draft.entries()
    assessment = ip_rows(request, obj, server_key, snapshot, entries)
    row = next((row for row in assessment.rows if row["row_id"] == row_key), None)
    if row is None:
        return None
    _show_selection(request, obj, server_key, [row], entries)
    table = IPAddressTable([row])
    table.configure(request)
    return render_to_string(ROWS_TEMPLATE, {"rows": table.rows}, request=request)
