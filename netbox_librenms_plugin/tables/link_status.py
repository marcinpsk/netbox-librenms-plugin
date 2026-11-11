"""Render the link state of one status-list row; it imports no NetBox table, so a browser test can render it."""

from django.utils.html import format_html

# The text of each link state that needs no match; the match states render their LibreNMS ID too.
_LINK_STATE_TEXT = {
    "linked": ("text-success", "mdi-check-circle", "Linked"),
    "refused": ("text-warning", "mdi-alert-circle-outline", "Stored mapping needs repair"),
    "no_match": ("text-danger", "mdi-close-circle", "Not found"),
    "unreachable": ("text-secondary", "mdi-help-circle", "Not checked: LibreNMS could not be reached"),
    "not_checked": ("text-secondary", "mdi-help-circle", "Not checked"),
}


def render_link_status(table, value, record, sync_url):
    """
    Render the link state of one status-list row: the stored link, or the match that the page proposes.

    A match that the user may link carries a checkbox for "Link selected" and a Link button; both post
    the token of the row to the forms that the list template renders.
    """
    row = getattr(table, "link_rows", {}).get(record.pk)
    state = value or (row.state if row is not None else "not_checked")
    if state in _LINK_STATE_TEXT:
        css, icon, text = _LINK_STATE_TEXT[state]
        return format_html(
            '<a href="{}" data-link-state="{}"><span class="{}"><i class="mdi {}"></i> {}</span></a>',
            sync_url,
            state,
            css,
            icon,
            text,
        )
    if state == "owned":
        detail = format_html("Already linked to {}", row.owner_label or "another object")
    elif state == "ambiguous":
        detail = "Matches more than one object on this page"
    else:
        detail = format_html("Matched by {}", row.matched_by)
    checkbox = button = ""
    if row.token:
        label = f"Link {record.name} to LibreNMS ID {row.librenms_id}"
        checkbox = format_html(
            '<input type="checkbox" class="form-check-input mt-1" form="librenms-link-selected" '
            'name="confirmation_intent" value="{}" aria-label="Select: {}">',
            row.token,
            label,
        )
        button = format_html(
            '<button type="submit" class="btn btn-sm btn-primary text-nowrap" form="librenms-link-row" '
            'name="confirmation_intent" value="{}" title="{}"><i class="mdi mdi-link-variant"></i> Link</button>',
            row.token,
            label,
        )
    return format_html(
        '<div class="d-flex align-items-start gap-2" data-link-state="{}">{}<div class="flex-grow-1">'
        '<a href="{}" class="{}"><i class="mdi mdi-link-variant-plus"></i> LibreNMS ID {}</a>'
        '<div class="small text-muted">{}{}</div></div>{}</div>',
        state,
        checkbox,
        sync_url,
        "text-info" if state == "match" else "text-warning",
        row.librenms_id,
        format_html("{} &middot; ", row.hostname) if row.hostname else "",
        detail,
        button,
    )
