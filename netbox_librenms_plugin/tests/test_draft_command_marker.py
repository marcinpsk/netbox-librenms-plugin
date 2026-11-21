"""
Guard: every element of a tab on the draft (IP, Interfaces, VLANs) and its dialogs that posts carries ``data-draft-command``.

The draft writer of the sync page (``sync_draft.js``) keys on that marker alone. A post that it does not see
as a command neither waits for the draft writes nor makes the READs sent before it stale, so an older READ
can show the tab from before the change. The scan reads the templates that the views of each tab name and
every plugin template that they include, and the markup that the tables of the tabs render in Python. Limit: an include whose name is a variable is not followed.
"""

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
TEMPLATES = PACKAGE / "templates"
TAB_VIEWS = {
    "ipaddresses": ("views/base/ip_addresses_view.py", "views/sync/ip_addresses.py"),
    "interfaces": ("views/base/interfaces_view.py", "views/sync/interfaces.py"),
    "vlans": ("views/base/vlan_table_view.py", "views/sync/vlans.py"),
}
PLUGIN_TEMPLATE = re.compile(r"""["'](netbox_librenms_plugin/[\w/.-]+\.html)["']""")
INCLUDE = re.compile(r"""\{%\s*include\s+["'](netbox_librenms_plugin/[\w/.-]+\.html)["']""")
# One opening tag; a template tag inside it can hold ">".
OPENING_TAG = re.compile(r"<[a-zA-Z][\w-]*((?:[^>{]|\{[^}]*\})*)>")
POSTS = re.compile(r"""\bmethod\s*=\s*["']post["']|\bhx-(?:post|put|patch|delete)\s*=""", re.IGNORECASE)
MARKER = re.compile(r"""\bdata-draft-command\s*=\s*["'](?:draft|action|refresh)["']""")


# The tables of the tabs on the draft, which render some posting elements in Python.
TAB_TABLES = ("tables/interfaces.py", "tables/vlans.py", "tables/ipaddresses.py", "tables/draft_rows.py")


def python_markup(path):
    """Return each string constant of the module *path*: adjacent literals are one constant."""
    tree = ast.parse((PACKAGE / path).read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def tab_templates(tab):
    """Return the plugin templates that the views of *tab* render, with every plugin template that they include."""
    pending = {
        name
        for view in TAB_VIEWS[tab]
        for name in PLUGIN_TEMPLATE.findall((PACKAGE / view).read_text(encoding="utf-8"))
    }
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


@pytest.mark.parametrize(
    ("tab", "templates"),
    [
        (
            "ipaddresses",
            {
                "_ipaddress_sync_content.html",
                "inc/_ipaddress_sync_refresh.html",
                "htmx/ip_address_conflicts.html",
                "ip_address_conflicts_page.html",
            },
        ),
        (
            "interfaces",
            {"_interface_sync_content.html", "inc/_interface_sync_refresh.html", "htmx/interface_vlan_groups.html"},
        ),
        (
            "vlans",
            {
                "_vlan_sync_content.html",
                "inc/_vlan_sync_refresh.html",
                "htmx/vlan_conflicts.html",
                "vlan_conflicts_page.html",
            },
        ),
    ],
)
def test_the_scan_reads_the_tab_and_its_dialogs(tab, templates):
    assert {f"netbox_librenms_plugin/{name}" for name in templates} <= tab_templates(tab)


@pytest.mark.parametrize("tab", sorted(TAB_VIEWS))
def test_every_element_that_posts_in_the_tab_and_its_dialogs_is_a_draft_command(tab):
    unmarked = {
        name: tags
        for name in sorted(tab_templates(tab))
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


@pytest.mark.parametrize("path", TAB_TABLES)
def test_every_element_that_posts_in_the_tables_of_the_tabs_is_a_draft_command(path):
    unmarked = [tag for text in python_markup(path) for tag in unmarked_posting_tags(text)]

    assert not unmarked, f"give each of these a data-draft-command marker (see sync_draft.js): {unmarked}"


def test_the_table_scan_reads_the_relationship_buttons():
    assert any("hx-post=" in text and "data-draft-command=" in text for text in python_markup("tables/interfaces.py"))
