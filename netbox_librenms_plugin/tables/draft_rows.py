"""The row attributes and the checkbox marker of the Interfaces and VLANs tables, which keep their selection in the server draft."""

from django.utils.html import format_html

# The marker of a selected row whose evidence changed since the user selected it.
CHANGED_MARKER = format_html(
    '<i class="mdi mdi-alert-circle-outline text-warning ms-1" role="img" title="{0}" aria-label="{0}"></i>',
    "Changed since you selected it: select it again",
)


def _row_read_attribute(value):
    """Return a row read attribute for a row with a read URL, and leave a row without one bare."""
    return lambda record: value if record.get("row_read_url") else None


def draft_row_attrs(row_key):
    """
    Return the row attributes that the page's draft writer reads, for the ``row_attrs`` of a table.

    Args:
        row_key (Callable[[dict], str | None]): Returns the row key of a record.

    Returns:
        dict: The attribute functions by attribute name.

    """
    return {
        "data-row-key": row_key,
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


def with_changed_marker(checkbox, record):
    """Return the row checkbox markup, with the marker when the row changed after the user selected it."""
    return format_html("{}{}", checkbox, CHANGED_MARKER) if record.get("evidence_changed") else checkbox
