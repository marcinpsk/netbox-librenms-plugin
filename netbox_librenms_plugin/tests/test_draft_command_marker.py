"""
Guard: every element of the IP tab and its dialogs that posts carries ``data-draft-command``.

The draft writer of the sync page (``sync_draft.js``) keys on that marker alone. A post that it does not see
as a command neither waits for the draft writes nor makes the READs sent before it stale, so an older READ
can show the tab from before the change. The scan reads the templates that the IP views name and every
plugin template that they include. Limit: an include whose name is a variable is not followed.
"""

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
TEMPLATES = PACKAGE / "templates"
IP_VIEWS = (PACKAGE / "views/base/ip_addresses_view.py", PACKAGE / "views/sync/ip_addresses.py")
PLUGIN_TEMPLATE = re.compile(r"""["'](netbox_librenms_plugin/[\w/.-]+\.html)["']""")
INCLUDE = re.compile(r"""\{%\s*include\s+["'](netbox_librenms_plugin/[\w/.-]+\.html)["']""")
# One opening tag; a template tag inside it can hold ">".
OPENING_TAG = re.compile(r"<[a-zA-Z][\w-]*((?:[^>{]|\{[^}]*\})*)>")
POSTS = re.compile(r"""\bmethod\s*=\s*["']post["']|\bhx-(?:post|put|patch|delete)\s*=""", re.IGNORECASE)
MARKER = re.compile(r"""\bdata-draft-command\s*=\s*["'](?:draft|action|refresh)["']""")


def ip_tab_templates():
    """Return the plugin templates that the IP views render, with every plugin template that they include."""
    pending = {name for view in IP_VIEWS for name in PLUGIN_TEMPLATE.findall(view.read_text(encoding="utf-8"))}
    found = set()
    while pending:
        name = pending.pop()
        if name not in found:
            found.add(name)
            pending.update(INCLUDE.findall((TEMPLATES / name).read_text(encoding="utf-8")))
    return found


def unmarked_posting_tags(text):
    """Return each opening tag in *text* that posts and carries no draft command marker."""
    return [
        tag.group(0)
        for tag in OPENING_TAG.finditer(text)
        if POSTS.search(tag.group(1)) and not MARKER.search(tag.group(1))
    ]


def test_the_scan_reads_the_ip_tab_and_its_dialogs():
    assert {
        "netbox_librenms_plugin/_ipaddress_sync_content.html",
        "netbox_librenms_plugin/inc/_ipaddress_sync_refresh.html",
        "netbox_librenms_plugin/htmx/ip_address_conflicts.html",
        "netbox_librenms_plugin/ip_address_conflicts_page.html",
    } <= ip_tab_templates()


def test_every_element_that_posts_in_the_ip_tab_and_its_dialogs_is_a_draft_command():
    unmarked = {
        name: tags
        for name in sorted(ip_tab_templates())
        if (tags := unmarked_posting_tags((TEMPLATES / name).read_text(encoding="utf-8")))
    }

    assert not unmarked, f"give each of these a data-draft-command marker (see sync_draft.js): {unmarked}"


@pytest.mark.parametrize(
    ("markup", "unmarked"),
    [
        ('<form method="post" action="/sync/">', True),
        ('<form method="POST">', True),
        ('<button type="button" {% if key %}hx-post="{% url \'x\' %}"{% endif %}>', True),
        ('<div hx-delete="/row/">', True),
        ('<form method="post" data-draft-command="action">', False),
        ('<button {% if a > b %}hx-post="/x/"{% endif %} data-draft-command="draft">', False),
        ('<form method="get" hx-get="/filter/">', False),
        ('<form method="post" data-draft-command="other">', True),
    ],
)
def test_the_check_finds_an_element_that_posts_without_a_marker(markup, unmarked):
    assert bool(unmarked_posting_tags(markup)) is unmarked
