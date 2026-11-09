"""
The page's required-row mirror (sync_draft.js) against the shared case file, the commands of the interface table,
and the rules of the page reads that a tab on the draft depends on.

Each case of ``selection_rule_cases.json`` renders its page rows with the attributes that the server rule
(``required_rows.py``, loaded from its file) gives them, then clicks as the case says. After each click the
page must tick the rows that the server checks, and send the intent that the case names.
"""

import ast
import importlib.util
import json
import re
from types import SimpleNamespace
from pathlib import Path

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from test_sync_cache_browser import TEMPLATE_DIR, _navigation_loader, _serve_page_scripts, _sync_document
from test_sync_filters_browser import TABLE, _answer, _filter_tab, _next_request, _serve_filter_tab
from test_sync_navigation_browser import FRAGMENT_LOADER, FRAGMENT_URL
from test_sync_draft_writer_browser import _form_fields

PLUGIN_DIR = Path(__file__).resolve().parents[2]
CASES = json.loads((PLUGIN_DIR / "tests" / "selection_rule_cases.json").read_text(encoding="utf-8"))
PAGE_URL = "https://plugin.example.com/dcim/devices/1/librenms-sync/"
SELECTION_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/interface-selection/?server_key=default"
CHOICE_URL = (
    "https://plugin.example.com/plugins/librenms_plugin/device/1/interface-selection/choice/?server_key=default"
)
MEMBER_URL = (
    "https://plugin.example.com/plugins/librenms_plugin/device/1/interface-selection/member/?server_key=default"
)


def _load_rule():
    """Load the server rule from its file: it imports nothing of the plugin, which needs NetBox."""
    spec = importlib.util.spec_from_file_location("required_rows", PLUGIN_DIR / "required_rows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULE = _load_rule()


def _rendered_rows(case):
    """Return the page rows of *case* with the attributes and the checkbox state that the server renders."""
    edges = {key: set(values) for key, values in case["edges"].items()}
    blocked = set(case.get("blocked", ()))
    intents = case["intents"]
    checked = RULE.checked_rows(edges, intents, blocked, enabled=case.get("enabled", True))
    off_page = RULE.off_page_requirers(edges, intents, blocked, case["page"])
    rows = []
    for key in case["page"]:
        requires = [] if key in blocked else sorted(RULE.requirements(edges, key, blocked))
        rows.append(
            f"""<tr data-row-key="{key}" data-intent="{intents.get(key, "none")}" data-requires='{json.dumps(requires)}'
                data-offpage-requirers="{off_page.get(key, 0)}" data-evidence="token-{key}">
                <td><input type="checkbox" name="select" value="{key}"{" checked" if key in checked else ""}></td>
                <td><select class="form-select vc-member-select" data-draft-choice="member">
                    <option value="" selected>Select</option><option value="7">member-7</option></select></td></tr>"""
        )
    return "".join(rows)


def _page(case, sync_url=None):
    enabled = " checked" if case.get("enabled", True) else ""
    # The sync form is a draft command, as the tab renders it, when the test posts it.
    posts = (
        f'data-draft-command="draft" hx-post="{sync_url}" hx-target="#sync-answer" hx-swap="innerHTML"'
        if sync_url
        else ""
    )
    submit = '<button id="sync-selected" type="submit">Sync</button><div id="sync-answer"></div>' if sync_url else ""
    return f"""<div id="librenms-sync-tabs">
        <input type="checkbox" id="autoSelectLagMembers"{enabled}>
        <form id="interface-sync-form" {posts}>{submit}
          <input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">
          <table id="librenms-interface-table" data-draft-id="draft-1" data-draft-revision="1"
                 data-selection-url="{SELECTION_URL}" data-choice-url="{CHOICE_URL}" data-member-url="{MEMBER_URL}">
            <thead><tr><th><input type="checkbox" class="toggle"></th><th>Member</th></tr></thead>
            <tbody>{_rendered_rows(case)}</tbody>
          </table>
        </form>
        <form data-write-url="{MEMBER_URL}"><select name="value"><option value="7" selected>member-7</option></select>
          <button type="button" id="apply-member" data-draft-write>Apply</button></form>
      </div>"""


class _Server:
    """Serve the page and acknowledge every draft command, recording each one."""

    def __init__(self, page, case):
        self.revision = 1
        self.commands = []
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        _serve_page_scripts(page)
        body = _sync_document(_page(case))
        page.route(f"{PAGE_URL}**", lambda route: route.fulfill(status=200, content_type="text/html", body=body))
        for url in (SELECTION_URL, CHOICE_URL, MEMBER_URL):
            page.route(url, self._command)
        page.goto(PAGE_URL)
        page.wait_for_function("() => window.librenmsSyncDraft !== undefined")

    def _command(self, route):
        self.commands.append((route.request.url, _form_fields(route.request)))
        self.revision += 1
        route.fulfill(status=204, headers={"X-Draft-Revision": str(self.revision)})

    def assignments(self):
        """Return the row states of every selection batch, in order."""
        return [json.loads(fields["assignments"]) for url, fields in self.commands if url == SELECTION_URL]


def _checked_rows(page):
    return sorted(
        page.evaluate(
            "() => Array.from(document.querySelectorAll('tr[data-row-key] input[name=select]:checked'), (box) => box.value)"
        )
    )


def _intents(page):
    return page.evaluate(
        "() => Object.fromEntries(Array.from(document.querySelectorAll('tr[data-row-key]'), (row) => [row.dataset.rowKey, row.dataset.intent]))"
    )


def _flush(page):
    page.evaluate("() => window.librenmsSyncDraft.flush()")


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_mirror_ticks_the_rows_the_server_checks_after_each_click(page, case):
    server = _Server(page, case)
    assert _checked_rows(page) == sorted(case["checked"])

    for step in case.get("steps", ()):
        page.locator(f'tr[data-row-key="{step["click"]}"] input[name="select"]').click()

        assert _checked_rows(page) == sorted(step["checked"]), step
        assert _intents(page) == {key: step["intents"].get(key, "none") for key in case["page"]}, step
        _flush(page)
        sent = server.assignments()[-1][step["click"]]
        expected = step["intents"][step["click"]]
        assert sent == {"state": expected, "token": f"token-{step['click']}" if expected == "on" else None}
    assert server.errors == []


LAG = {"edges": {"M1": ["G"], "M2": ["G"], "G": ["M1", "M2"]}, "page": ["G", "M1", "M2", "X"], "intents": {}}


def test_select_all_selects_every_row_also_a_required_one(page):
    server = _Server(page, {**LAG, "intents": {"M1": "on"}})
    assert _checked_rows(page) == ["G", "M1", "M2"]

    page.locator("th input.toggle").check()

    assert _checked_rows(page) == ["G", "M1", "M2", "X"]
    _flush(page)
    assert {key: state["state"] for key, state in server.assignments()[-1].items()} == dict.fromkeys(
        ["G", "M2", "X"], "on"
    )


def test_clearing_all_excludes_a_row_that_a_selected_row_of_another_page_requires(page):
    server = _Server(page, {"edges": {"A": ["B"]}, "page": ["B", "C"], "intents": {"A": "on", "C": "on"}})

    page.locator("th input.toggle").check()
    page.locator("th input.toggle").uncheck()

    assert _checked_rows(page) == []
    _flush(page)
    assert {key: state["state"] for key, state in server.assignments()[-1].items()} == {"B": "off", "C": "none"}


def test_the_option_ticks_the_required_rows_again_and_sends_nothing(page):
    server = _Server(page, {**LAG, "intents": {"M1": "on"}})

    page.locator("#autoSelectLagMembers").uncheck()
    assert _checked_rows(page) == ["M1"]
    page.locator("#autoSelectLagMembers").check()
    assert _checked_rows(page) == ["G", "M1", "M2"]

    _flush(page)
    assert server.commands == []


def test_a_member_choice_and_a_dialog_command_go_to_the_draft_in_order(page):
    server = _Server(page, LAG)

    page.locator('tr[data-row-key="X"] select').select_option("7")
    page.locator("#apply-member").click()
    _flush(page)

    assert [(url, fields.get("row"), fields.get("field"), fields["value"]) for url, fields in server.commands] == [
        (CHOICE_URL, "X", "member", "7"),
        (MEMBER_URL, None, None, "7"),
    ]
    assert [fields["expected_revision"] for _url, fields in server.commands] == ["1", "2"]


SYNC_URL = "https://plugin.example.com/plugins/librenms_plugin/devices/1/interface-sync/?server_key=default"


def _wait_for(page, condition):
    for _ in range(100):
        if condition():
            return
        page.wait_for_timeout(20)
    raise AssertionError("the condition did not come true")


def test_a_refused_member_choice_refuses_the_interface_submit_queued_behind_it(page):
    """The page shows a member that the server refused, so the submit behind the choice never writes the row."""
    held, submits, reads = [], [], []
    _serve_page_scripts(page)
    body = _sync_document(_navigation_loader() + _page(LAG, sync_url=SYNC_URL))

    def serve(route):
        reads.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body=body)

    def submit(route):
        submits.append(_form_fields(route.request))
        route.fulfill(status=200, content_type="text/html", body="", headers={"HX-Reswap": "none"})

    page.route(f"{PAGE_URL}**", serve)
    page.route(CHOICE_URL, lambda route: held.append(route))
    page.route(SYNC_URL, submit)
    page.goto(PAGE_URL)
    page.wait_for_function("history.state && history.state.lnmsSync === true")

    page.locator('tr[data-row-key="X"] select').select_option("7")
    _wait_for(page, lambda: held)
    page.click("#sync-selected")
    page.wait_for_timeout(200)
    held[0].fulfill(status=404)

    expect(page.locator("#librenms-draft-notice")).to_be_visible()
    # The page reads the tab again, which is the fresh presentation; no submit went before it.
    _wait_for(page, lambda: len(reads) >= 2)
    page.wait_for_timeout(300)
    assert submits == []

    page.click("#sync-selected")
    _wait_for(page, lambda: submits)
    assert (submits[0]["draft_id"], submits[0]["expected_revision"]) == ("draft-1", "1")


VLAN_GROUPS_URL = (
    "https://plugin.example.com/plugins/librenms_plugin/device/1/interface-selection/vlan-groups/?server_key=default"
)
VLAN_PREVIEW_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/interface-row/10/vlan-groups/"


def _vlan_dialog(group_id, *, apply_to_all, css="text-success", missing=False, row_key="10"):
    """Return the VLAN groups dialog of row *row_key* with VLAN 100 in group *group_id*, as the server renders it."""
    from django.template import Context, Engine

    engine = Engine(dirs=[str(TEMPLATE_DIR.parent)])
    return engine.get_template("netbox_librenms_plugin/htmx/interface_vlan_groups.html").render(
        Context(
            {
                "row_key": row_key,
                "interface_name": f"eth{row_key}",
                "vlans": [{"vid": 100, "type": "T", "css": css, "missing": missing, "group_id": group_id}],
                "groups": [
                    SimpleNamespace(pk=7, name="group-7", scope=None),
                    SimpleNamespace(pk=8, name="group-8", scope=None),
                ],
                "command_url": VLAN_GROUPS_URL,
                "preview_url": VLAN_PREVIEW_URL,
                "apply_to_all": apply_to_all,
            },
            use_l10n=False,
        )
    )


def test_a_late_vlan_dialog_preview_keeps_a_newer_apply_to_all_and_save_sends_it(page):
    """A preview shows the colours of the chosen groups; it never sets back a control that the user changed after it."""
    previews, writes = [], []
    _serve_page_scripts(page)
    body = _sync_document(
        f"""<div id="librenms-sync-tabs"><input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">
            <table id="librenms-interface-table" data-draft-id="draft-1" data-draft-revision="1"
                   data-selection-url="{SELECTION_URL}" data-choice-url="{CHOICE_URL}"><tbody></tbody></table>
            <div id="vlanDetailModalContent">{_vlan_dialog("7", apply_to_all=False)}</div></div>"""
    )

    def write(route):
        writes.append(_form_fields(route.request))
        route.fulfill(status=204, headers={"X-Draft-Revision": "2"})

    page.route(f"{PAGE_URL}**", lambda route: route.fulfill(status=200, content_type="text/html", body=body))
    page.route(f"{VLAN_PREVIEW_URL}**", lambda route: previews.append(route))
    page.route(VLAN_GROUPS_URL, write)
    page.goto(PAGE_URL)
    page.wait_for_function("() => window.librenmsSyncDraft !== undefined")

    page.select_option('select[name="group_100"]', "8")
    _wait_for(page, lambda: previews)
    page.check("#applyVlanGroupToAll")
    previews[0].fulfill(
        status=200,
        content_type="text/html",
        body=_vlan_dialog("8", apply_to_all=False, css="text-danger", missing=True),
    )

    expect(page.locator('tr[data-vid="100"] span.text-danger')).to_have_count(1)
    expect(page.locator("#applyVlanGroupToAll")).to_be_checked()
    page.click("#saveVlanGroups")
    _flush(page)
    assert [(fields["row"], fields["group_100"], fields.get("apply_to_all")) for fields in writes] == [("10", "8", "1")]


RELATIONSHIP_URL = (
    "https://plugin.example.com/plugins/librenms_plugin/device/1/sync-interface-lag/?interface_name_field=ifName"
)


def test_a_relationship_action_after_a_member_choice_goes_after_it_bound_to_the_row_it_showed(page):
    """The action waits for the member choice; it then names the new revision and the evidence that its row showed."""
    held, actions, order = [], [], []
    _serve_page_scripts(page)
    values = json.dumps({"port_id": "10", "lag_port_id": "20", "server_key": "default"})
    body = _sync_document(
        f"""<div id="librenms-sync-tabs"><form id="interface-sync-form">
            <input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">
            <table id="librenms-interface-table" data-draft-id="draft-1" data-draft-revision="1"
                   data-selection-url="{SELECTION_URL}" data-choice-url="{CHOICE_URL}">
              <tbody><tr data-row-key="10" data-evidence="token-10">
                <td><input type="checkbox" name="select" value="10"></td>
                <td><select data-draft-choice="member"><option value="" selected>Select</option>
                    <option value="7">member-7</option></select></td>
                <td><button type="button" class="lag-sync-btn" data-draft-command="action" hx-post="{RELATIONSHIP_URL}"
                    hx-vals='{values}' hx-params="port_id,lag_port_id,server_key" hx-swap="none">Sync</button></td>
              </tr></tbody>
            </table></form></div>"""
    )

    def choose(route):
        order.append("choice")
        held.append(route)

    def act(route):
        order.append("action")
        actions.append(_form_fields(route.request))
        route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "row changed"}))

    page.route(f"{PAGE_URL}**", lambda route: route.fulfill(status=200, content_type="text/html", body=body))
    page.route(CHOICE_URL, choose)
    page.route(RELATIONSHIP_URL, act)
    page.goto(PAGE_URL)
    page.wait_for_function("() => window.librenmsSyncDraft !== undefined")

    page.locator('tr[data-row-key="10"] select').select_option("7")
    _wait_for(page, lambda: held)
    page.click(".lag-sync-btn")
    page.wait_for_timeout(200)
    assert actions == []
    held[0].fulfill(
        status=204, headers={"X-Draft-Revision": "2", "HX-Trigger": json.dumps({"lnms-row-stale": {"key": "10"}})}
    )

    _wait_for(page, lambda: actions)
    assert order == ["choice", "action"]
    fields = actions[0]
    assert (
        fields["port_id"],
        fields["lag_port_id"],
        fields["draft_id"],
        fields["expected_revision"],
        fields["evidence"],
    ) == (
        "10",
        "20",
        "draft-1",
        "2",
        "token-10",
    )


DIALOG_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/interface-row/{key}/vlan-dialog/"


def _vlan_edit_button(url):
    """Return the edit button that the interface table renders for a row whose VLAN groups dialog is at *url*."""
    tree = ast.parse((PLUGIN_DIR / "tables" / "interfaces.py").read_text(encoding="utf-8"))
    (markup,) = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "vlan-edit-btn" in node.value
    ]
    # The table fills its one placeholder with format_html.
    return markup.format(url)


def _vlan_dialog_page(keys):
    """Return a sync page whose interface rows *keys* open the VLAN groups dialog, as the tab renders them."""
    template = (TEMPLATE_DIR / "_interface_sync_content.html").read_text(encoding="utf-8")
    dialog = re.search(r"<!-- VLAN groups dialog.*?-->(.*?)\{% else %\}", template, flags=re.S).group(1)
    dialog = re.sub(r"\{#.*?#\}", "", dialog)
    rows = "".join(
        f"""<tr data-row-key="{key}" data-evidence="token-{key}">
            <td><input type="checkbox" name="select" value="{key}"></td>
            <td>{_vlan_edit_button(DIALOG_URL.format(key=key))}</td></tr>"""
        for key in keys
    )
    return _sync_document(
        f"""<div id="librenms-sync-tabs"><input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">
            <table id="librenms-interface-table" data-draft-id="draft-1" data-draft-revision="1"
                   data-selection-url="{SELECTION_URL}" data-choice-url="{CHOICE_URL}"><tbody>{rows}</tbody></table>
            {dialog}</div>"""
    )


def _open_vlan_dialog(page, key):
    page.click(f'#librenms-interface-table tr[data-row-key="{key}"] .vlan-edit-btn')


def test_a_late_answer_of_an_earlier_vlan_dialog_opening_never_replaces_the_dialog(page):
    """Two rows open the dialog in turn; the answer of the first one comes last, and Save sends the second row's form."""
    dialogs, writes = {}, []
    _serve_page_scripts(page)
    body = _vlan_dialog_page(["10", "11"])

    def write(route):
        writes.append(_form_fields(route.request))
        route.fulfill(status=204, headers={"X-Draft-Revision": "2"})

    page.route(f"{PAGE_URL}**", lambda route: route.fulfill(status=200, content_type="text/html", body=body))
    page.route("**/vlan-dialog/", lambda route: dialogs.setdefault(route.request.url.split("/")[-3], route))
    page.route(VLAN_GROUPS_URL, write)
    page.goto(PAGE_URL)
    page.wait_for_function("() => window.librenmsSyncDraft !== undefined")

    _open_vlan_dialog(page, "10")
    _wait_for(page, lambda: "10" in dialogs)
    _open_vlan_dialog(page, "11")
    _wait_for(page, lambda: "11" in dialogs)
    dialogs["11"].fulfill(
        status=200, content_type="text/html", body=_vlan_dialog("7", apply_to_all=False, row_key="11")
    )
    page.check("#applyVlanGroupToAll")
    dialogs["10"].fulfill(status=200, content_type="text/html", body=_vlan_dialog("7", apply_to_all=False))
    page.wait_for_timeout(300)

    expect(page.locator('#vlanDetailModalContent input[name="row"]')).to_have_value("11")
    expect(page.locator("#applyVlanGroupToAll")).to_be_checked()
    page.click("#saveVlanGroups")
    _flush(page)
    assert [(fields["row"], fields.get("apply_to_all")) for fields in writes] == [("11", "1")]


class _VlanDialogServer:
    """Serve the VLAN dialog page; hold each dialog read with the group the draft has when it comes, and each write."""

    def __init__(self, page):
        self.group = "7"
        self.dialogs, self.writes, self.held = [], [], []
        _serve_page_scripts(page)
        body = _vlan_dialog_page(["10"])
        page.route(f"{PAGE_URL}**", lambda route: route.fulfill(status=200, content_type="text/html", body=body))
        page.route(DIALOG_URL.format(key="10"), lambda route: self.dialogs.append((route, self.group)))
        page.route(f"{VLAN_PREVIEW_URL}**", lambda route: route.fulfill(status=204))
        page.route(VLAN_GROUPS_URL, self._write)
        page.route(SELECTION_URL, self._write)
        page.goto(PAGE_URL)
        page.wait_for_function("() => window.librenmsSyncDraft !== undefined")

    def _write(self, route):
        self.writes.append((route.request.url, _form_fields(route.request)))
        self.held.append(route)

    def acknowledge(self, group, revision):
        """Answer the held write: the draft now holds *group*, and the row is stale."""
        self.group = group
        stale = json.dumps({"lnms-row-stale": {"key": "10"}})
        self.held.pop(0).fulfill(status=204, headers={"X-Draft-Revision": revision, "HX-Trigger": stale})

    def answer_dialog(self):
        """Answer the oldest held dialog read with the group that the draft had when the read came."""
        route, group = self.dialogs.pop(0)
        route.fulfill(status=200, content_type="text/html", body=_vlan_dialog(group, apply_to_all=False))


def test_a_vlan_dialog_opened_again_during_its_save_shows_the_saved_group(page):
    """Save holds group 8 while the dialog opens again; the dialog then shows 8, and a second Save cannot send 7."""
    server = _VlanDialogServer(page)
    _open_vlan_dialog(page, "10")
    _wait_for(page, lambda: server.dialogs)
    server.answer_dialog()
    page.select_option('select[name="group_100"]', "8")
    page.click("#saveVlanGroups")
    _wait_for(page, lambda: server.held)

    _open_vlan_dialog(page, "10")
    page.wait_for_timeout(200)
    server.acknowledge(server.writes[-1][1]["group_100"], "2")
    _wait_for(page, lambda: server.dialogs)
    page.wait_for_timeout(200)
    # The answer of the dialog opened during the Save comes after the Save and its row read.
    server.answer_dialog()

    expect(page.locator('select[name="group_100"]')).to_have_value("8")
    page.click("#saveVlanGroups")
    _wait_for(page, lambda: len(server.writes) == 2)
    assert [(fields["group_100"], fields["expected_revision"]) for _url, fields in server.writes] == [
        ("8", "1"),
        ("8", "2"),
    ]


def test_a_vlan_dialog_answer_read_before_a_later_write_never_shows_and_the_dialog_is_read_again(page):
    """The dialog read goes, then a row click writes the draft; the old answer is dropped and a new read shows."""
    server = _VlanDialogServer(page)
    _open_vlan_dialog(page, "10")
    _wait_for(page, lambda: server.dialogs)

    page.locator('tr[data-row-key="10"] input[name="select"]').check()
    _wait_for(page, lambda: server.held)
    server.acknowledge("8", "2")
    page.wait_for_timeout(200)
    server.answer_dialog()
    page.wait_for_timeout(200)

    expect(page.locator('select[name="group_100"]')).to_have_count(0)
    _wait_for(page, lambda: server.dialogs)
    server.answer_dialog()
    expect(page.locator('select[name="group_100"]')).to_have_value("8")


VLAN_SELECTION_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/vlan-selection/?server_key=default"


def _two_panes(active):
    """Return the sync tabs with an interfaces pane and a VLANs pane, each with its draft table, as the page renders them."""

    def pane(tab_id, table_id, draft_id, url, key):
        shown = " show active" if tab_id == active else ""
        return f"""<div class="tab-pane{shown}" id="{tab_id}"><form><table id="{table_id}" data-draft-id="{draft_id}"
              data-draft-revision="3" data-selection-url="{url}"><tbody><tr data-row-key="{key}" data-evidence="token-{key}">
              <td><input type="checkbox" name="select" value="{key}"></td></tr></tbody></table></form></div>"""

    return f"""<div id="librenms-sync-tabs" data-active-tab="{active}">
        <a id="vlans-tab" data-sync-nav href="?tab=vlans">VLANs</a>
        <div class="tab-content" id="librenmsTabContent">
          {pane("interfaces", "librenms-interface-table", "interfaces-draft", SELECTION_URL, "10")}
          {pane("vlans", "librenms-vlan-table", "vlans-draft", VLAN_SELECTION_URL, "300")}
        </div></div>"""


def test_after_a_tab_change_only_the_draft_table_of_the_active_tab_takes_input(page):
    """Every tab's pane is in the page; after htmx swaps in another tab, its table takes input, not the first table."""
    commands = []
    _serve_page_scripts(page)
    from test_sync_cache_browser import _navigation_loader

    def document(active):
        return _sync_document(
            f"""<style>.tab-pane {{ display: none; }} .tab-pane.active {{ display: block; }}</style>
            <input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">{_navigation_loader()}{_two_panes(active)}"""
        )

    page.route(
        f"{PAGE_URL}**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=document("vlans" if "tab=vlans" in route.request.url else "interfaces"),
        ),
    )

    def answer(route):
        commands.append((route.request.url, _form_fields(route.request)))
        route.fulfill(status=204, headers={"X-Draft-Revision": "4"})

    page.route(SELECTION_URL, answer)
    page.route(VLAN_SELECTION_URL, answer)
    page.goto(PAGE_URL)
    page.wait_for_function("history.state && history.state.lnmsSync === true")

    page.click("#vlans-tab")
    page.locator('#librenms-vlan-table input[name="select"]').click()
    _flush(page)

    assert [(url, fields["draft_id"], fields["expected_revision"]) for url, fields in commands] == [
        (VLAN_SELECTION_URL, "vlans-draft", "3")
    ]
    assert page.locator("#librenms-draft-notice").count() == 0


def test_a_fragment_sent_after_the_typing_does_not_drop_the_search_that_waits_for_its_delay(page):
    """A data refresh reloads the tab's fragment after the user started to type: the search still goes."""
    held = _serve_filter_tab(page, extra=FRAGMENT_LOADER)
    fragments = []
    page.route(f"{FRAGMENT_URL}**", lambda route: fragments.append(route))
    search = page.locator("#id_interfaces_q")

    search.press_sequentially("eth")
    page.evaluate("document.getElementById('fragment-loader').dispatchEvent(new CustomEvent('librenms:load-fragment'))")
    for _ in range(100):
        if fragments:
            break
        page.wait_for_timeout(20)
    fragments[0].fulfill(status=200, content_type="text/html", body=_filter_tab({}))

    route = _next_request(page, held, 1)
    assert parse_qs(urlsplit(route.request.url).query)["interfaces_q"] == ["eth"]
    _answer(route)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    expect(search).to_have_value("eth")
