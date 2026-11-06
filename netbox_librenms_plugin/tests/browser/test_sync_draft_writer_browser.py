"""
Browser checks of the selection draft writer of sync_draft.js on a page shaped like a NetBox sync page.

HTMX loads in the head without a global, and the plugin scripts end the body. The test server keeps the
draft (row states, VRF choices and the revision), answers the draft commands, and renders the page and
single rows from it, so the page behaves as it does against the plugin. A test can hold any request, so
it decides the order in which the answers arrive.
"""

import json
from email.parser import BytesParser
from email.policy import HTTP
from html import escape
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

from test_sync_cache_browser import _navigation_loader, _serve_page_scripts, _sync_document

PAGE_URL = "https://plugin.example.com/dcim/devices/1/librenms-sync/"
SELECTION_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/ip-selection/?server_key=default"
CHOICE_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/ip-selection/choice/?server_key=default"
MANAGEMENT_URL = (
    "https://plugin.example.com/plugins/librenms_plugin/device/1/ip-selection/management/?server_key=default"
)
ROW_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/ip-row/"
SUBMIT_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/sync-ip-addresses/"
FRAGMENT_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/sync-cache-fragment/ipaddresses/"
REFRESH_URL = "https://plugin.example.com/plugins/librenms_plugin/devices/1/ipaddress-sync/"
FILTER_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/sync-tab-filter/ipaddresses/"
MOVE_URL = "https://plugin.example.com/plugins/librenms_plugin/ip-addresses/7/move-to-winner/"
VRF_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/ip-row-vrf/"
ROWS = ("198.18.0.1/24", "198.18.0.2/24", "198.18.0.3/24", "198.18.0.4/24")
MESSAGES = '<div id="django-messages" hx-swap-oob="true"><div class="toast">Created IP addresses</div></div>'
# The management rows: one that the page shows and one that no page shows.
MANAGEMENT_ROW = ROWS[3]
OFF_PAGE_MANAGEMENT_ROW = "198.18.0.9/24"


def _form_fields(request):
    """Return the fields of a multipart or urlencoded POST body."""
    content_type = request.headers.get("content-type", "")
    body = request.post_data_buffer or b""
    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
        return {
            part.get_param("name", header="content-disposition"): part.get_content() for part in message.iter_parts()
        }
    return {key: values[0] for key, values in parse_qs(body.decode()).items()}


class _DraftServer:
    """
    Keep one draft and serve the page, the row reads, the tab fragment and the draft commands from it.

    The server handles each request when it arrives, as the plugin does; a held request only delays its answer.
    """

    def __init__(self, page):
        self.page = page
        self.revision = 5
        self.draft_id = "draft-1"
        self.checked = set()
        self.vrfs = dict.fromkeys(ROWS, "")
        self.renders = 0
        self.hold = set()
        self.held = []
        self.commands = []
        self.reads = []
        self.submits = []
        self.answers = {}
        # The submit answer names no revision, as after another window's change during the submit.
        self.changed_during_submit = False
        # False renders the page of a tab without a draft table.
        self.shows_table = True
        # The rows that a row action synced, and the rows whose row action asks for a confirmation first.
        self.synced = set()
        self.conflicts = set()
        # A migrated device's Move moved its IP to the winner.
        self.moved = False
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        _serve_page_scripts(page)
        routes = {
            f"{PAGE_URL}**": "page",
            SELECTION_URL: "selection",
            CHOICE_URL: "choice",
            MANAGEMENT_URL: "management",
            f"{ROW_URL}**": "row",
            f"{SUBMIT_URL}**": "submit",
            FRAGMENT_URL: "fragment",
            REFRESH_URL: "refresh",
            f"{FILTER_URL}**": "filter",
            MOVE_URL: "move",
            f"{VRF_URL}**": "vrf",
        }
        for url, kind in routes.items():
            page.route(url, self._handler(kind))

    def _handler(self, kind):
        # Playwright passes the request as a second argument to a handler that takes two.
        return lambda route: self._take(kind, route)

    def _take(self, kind, route):
        request = route.request
        if (kind == "page" and request.headers.get("hx-request") == "true") or kind in ("row", "fragment", "filter"):
            self.reads.append(request.url)
        elif kind in ("selection", "choice", "management"):
            self.commands.append((kind, _form_fields(request)))
        elif kind in ("submit", "vrf"):
            self.submits.append(_form_fields(request))
        answer = self._answer(kind, request)
        if kind in self.hold:
            self.held.append((kind, route, answer))
        else:
            route.fulfill(**answer)

    def _answer(self, kind, request):
        """Handle one request as the plugin does, and return its answer."""
        if kind == "page":
            return {"status": 200, "content_type": "text/html", "body": _sync_document(self.body())}
        if kind == "row":
            key = parse_qs(urlsplit(request.url).query)["key"][0]
            return {"status": 200, "content_type": "text/html", "body": self.row(key)}
        if kind == "fragment":
            return {"status": 200, "content_type": "text/html", "body": self.content()}
        if kind == "filter":
            query = urlsplit(request.url).query
            return {
                "status": 200,
                "content_type": "text/html",
                "body": self.content(),
                "headers": {"X-Sync-Page-Url": f"{PAGE_URL}?{query}"},
            }
        if kind == "refresh":
            # A data refresh starts the draft of the new snapshot.
            self.draft_id, self.revision, self.checked = f"{self.draft_id}+", 0, set()
            return {"status": 200, "content_type": "text/html", "body": self.content()}
        if kind == "submit":
            return self._submit(_form_fields(request))
        if kind == "vrf":
            return {
                "status": 200,
                "body": MESSAGES,
                "headers": {"HX-Reswap": "none", "HX-Trigger": json.dumps({"lnms-results-stale": None})},
            }
        if kind == "move":
            # The plugin's answer to an htmx move: the messages out of band, and the event that reads the tab again.
            self.moved = True
            headers = {"HX-Reswap": "none", "HX-Trigger": json.dumps({"lnms-results-stale": None})}
            return {"status": 200, "body": MESSAGES, "headers": headers}
        return self._command(kind, _form_fields(request))

    def _submit(self, fields):
        # The plugin's answer to an htmx sync: the messages out of band, and the event that reads the tab again.
        headers = {"HX-Reswap": "none", "HX-Trigger": json.dumps({"lnms-results-stale": None})}
        if "force_conflict" in fields:
            self.synced.add(fields["force_conflict"])
            closing = json.dumps({"lnms-results-stale": None, "closeModal": None})
            return {"status": 200, "body": MESSAGES, "headers": {**headers, "HX-Trigger": closing}}
        if fields.get("sync_one") in self.conflicts:
            return {"status": 200, "content_type": "text/html", "body": self.confirmation(fields["sync_one"])}
        if "sync_one" in fields:
            self.synced.add(fields["sync_one"])
            return {"status": 200, "body": MESSAGES, "headers": headers}
        if int(fields["expected_revision"]) != self.revision:
            return {"status": 200, "body": "", "headers": headers}
        # The claim takes the rows and the writer writes them all.
        self.revision += 1
        self.checked = set()
        if self.changed_during_submit:
            self.revision += 1
            return {"status": 200, "body": "", "headers": headers}
        return {"status": 200, "body": "", "headers": {**headers, "X-Draft-Revision": str(self.revision)}}

    def _command(self, kind, fields):
        status = (self.answers.get(kind) or [204]).pop(0)
        if status == 204 and int(fields["expected_revision"]) != self.revision:
            status = 409
        if status != 204:
            return {"status": status, "body": ""}
        headers = {}
        if kind == "selection":
            for key, value in json.loads(fields["assignments"]).items():
                (self.checked.add if value["state"] == "on" else self.checked.discard)(key)
        elif kind == "choice":
            self.vrfs[fields["row"]] = fields["value"]
            self.checked.discard(fields["row"])
            headers["HX-Trigger"] = json.dumps({"lnms-row-stale": {"key": fields["row"]}})
        else:
            headers["HX-Trigger"] = json.dumps({"lnms-results-stale": None})
        self.revision += 1
        headers["X-Draft-Revision"] = str(self.revision)
        return {"status": 204, "body": "", "headers": headers}

    def change_elsewhere(self):
        """Change the draft as another window of the user does."""
        self.revision += 1

    def row(self, key):
        self.renders += 1
        checked = " checked" if key in self.checked else ""
        vrf = self.vrfs[key]
        options = "".join(
            f'<option value="{value}"{" selected" if value == vrf else ""}>{label}</option>'
            for value, label in (("", "Global"), ("5", "Blue"), ("9", "Red"))
        )
        management = ' data-mgmt-ip="true"' if key == MANAGEMENT_ROW else ""
        status = (
            "Synced" if key in self.synced else f'<button type="submit" name="sync_one" value="{key}">Create</button>'
        )

        return f"""<tr data-row-key="{key}" data-evidence="token-{key}-{vrf}" data-rendered="{self.renders}"{management}
            hx-get="{ROW_URL}?key={key}" hx-trigger="librenms:row-read" hx-target="this" hx-swap="outerHTML"
            hx-sync="this:replace" hx-params="none">
            <td data-col="selection"><input type="checkbox" name="select" value="{key}"{checked}></td>
            <td data-col="vrf"><select class="form-select vrf-select">{options}</select>
              <button type="submit" name="create_vrf" value="{key}" formaction="{VRF_URL}"
                      data-confirm="Create the VRF of this row?">Create VRF</button></td>
            <td data-col="status">{status}</td></tr>"""

    @staticmethod
    def confirmation(key):
        """Render the dialog that asks to confirm the change of one row."""
        return f"""<div class="modal-header"><h5 class="modal-title">Confirm IP address changes</h5></div>
            <form hx-post="{SUBMIT_URL}" hx-target="#htmx-modal-content" hx-swap="innerHTML"
                  data-draft-command="action">
              <input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">
              <input type="checkbox" name="force_conflict" value="{key}" checked>
              <button id="confirm-change" type="submit">Sync confirmed changes</button>
            </form>"""

    def content(self):
        """Render the IP tab content: the refresh button and the sync form with the draft table."""
        rows = "".join(self.row(key) for key in ROWS)
        offpage = sorted(self.checked - set(ROWS))
        notice = (
            f"<div data-offpage-selection>{len(offpage)} more selected rows"
            f'<button type="button" data-clear-offpage="{escape(json.dumps(offpage))}">Clear</button></div>'
            if offpage
            else ""
        )
        # The table's token of the shown row is older than the row's own: a row READ changed the row since.
        management = {
            MANAGEMENT_ROW: "token-from-the-table",
            OFF_PAGE_MANAGEMENT_ROW: f"token-{OFF_PAGE_MANAGEMENT_ROW}-",
        }
        return f"""<button id="refresh" type="button" hx-post="{REFRESH_URL}" hx-target="#ipaddress-sync-content"
                data-draft-command="refresh">
                Refresh</button>
              <output id="revision">{self.revision}</output>
              {notice}
              <output id="moved">{"moved" if self.moved else "here"}</output>
              <button id="move" type="button" data-draft-command="action" hx-post="{MOVE_URL}" hx-swap="none"
                      hx-confirm="Move IP to the winner?">Move</button>
              <form id="ipaddress-sync-form" method="post" action="{SUBMIT_URL}" hx-post="{SUBMIT_URL}"
                    hx-target="#htmx-modal-content" hx-swap="innerHTML" data-draft-command="draft">
                <input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">
                <table id="librenms-ipaddress-table" data-draft-id="{self.draft_id}"
                       data-draft-revision="{self.revision}" data-selection-url="{escape(SELECTION_URL)}"
                       data-choice-url="{escape(CHOICE_URL)}" data-management-url="{escape(MANAGEMENT_URL)}"
                       data-management-evidence="{escape(json.dumps(management))}">
                  <thead><tr><th><input type="checkbox" class="toggle"></th><th>VRF</th><th>Status</th></tr></thead>
                  <tbody>{rows}</tbody>
                </table>
                <button id="sync-selected" type="submit"><span class="spinner spinner-border d-none"></span>Sync</button>
              </form>"""

    def body(self):
        return f"""{_navigation_loader()}
            <div id="librenms-sync-tabs" data-active-tab="ipaddresses">
              <a id="cables-tab" data-sync-nav href="?tab=cables">Cables</a>
              <input type="checkbox" id="set-primary-ip-toggle-cb">
              <form id="ipaddresses_filter-form" class="sync-filter-form" hx-get="{FILTER_URL}" hx-trigger="submit"
                    hx-target="#ipaddress-sync-content" hx-swap="innerHTML" hx-sync="#librenms-sync-nav:queue last">
                <input id="filter-q" name="ipaddresses_q" value="198.18">
              </form>
              <span id="fragment-loader" class="d-none" data-fragment-loader hx-get="{FRAGMENT_URL}"
                    hx-trigger="librenms:load-fragment" hx-target="#ipaddress-sync-content" hx-swap="innerHTML"></span>
              <div id="ipaddress-sync-content">{self.content() if self.shows_table else ""}</div>
            </div>
            <div id="htmx-modal" class="modal"><div class="modal-dialog"><div class="modal-content"
                id="htmx-modal-content"></div></div></div>
            <div id="django-messages" hx-swap-oob="true"></div>"""

    def open(self):
        self.page.goto(f"{PAGE_URL}?tab=ipaddresses")
        self.page.wait_for_function("history.state && history.state.lnmsSync === true")

    def wait_until(self, condition):
        """Let the page run until *condition* holds; the route handlers run while the page waits."""
        for _attempt in range(100):
            if condition():
                return
            self.page.wait_for_timeout(50)
        raise AssertionError("the page did not get there")

    def wait_for_commands(self, count):
        self.wait_until(lambda: len(self.commands) >= count)

    def release(self, kind):
        """Answer the oldest held request of *kind*."""
        index = next(i for i, (held_kind, _route, _answer) in enumerate(self.held) if held_kind == kind)
        _kind, route, answer = self.held.pop(index)
        route.fulfill(**answer)


def _box(page, key):
    return page.locator(f'tr[data-row-key="{key}"] input[name="select"]')


def _assignments(fields):
    return {key: value["state"] for key, value in json.loads(fields["assignments"]).items()}


def test_clicks_paint_at_once_and_one_batch_carries_them_with_their_tokens(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("selection")

    # Three clicks within the batch time; each checkbox shows its click before any answer.
    painted = page.evaluate(
        """keys => keys.map(key => {
            const box = [...document.querySelectorAll('tr[data-row-key]')]
                .find(row => row.dataset.rowKey === key).querySelector('input[name="select"]');
            box.click();
            return box.checked;
        })""",
        list(ROWS[:3]),
    )
    server.wait_for_commands(1)
    page.wait_for_timeout(300)

    assert painted == [True, True, True]

    ((kind, fields),) = server.commands
    assert kind == "selection"
    assert (fields["draft_id"], fields["expected_revision"]) == ("draft-1", "5")
    assert json.loads(fields["assignments"]) == {key: {"state": "on", "token": f"token-{key}-"} for key in ROWS[:3]}
    assert server.errors == []


def test_one_command_is_in_flight_and_the_next_expects_the_acknowledged_revision(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("selection")

    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    _box(page, ROWS[1]).click()
    page.wait_for_timeout(400)
    assert len(server.commands) == 1
    server.hold.clear()
    server.release("selection")
    server.wait_for_commands(2)

    assert server.commands[1][1]["expected_revision"] == "6"
    assert _assignments(server.commands[1][1]) == {ROWS[1]: "on"}


def test_select_all_and_a_shift_range_set_rows_in_one_batch_each(page):
    server = _DraftServer(page)
    server.open()

    page.check("th input.toggle")
    server.wait_for_commands(1)
    assert _assignments(server.commands[0][1]) == dict.fromkeys(ROWS, "on")
    page.uncheck("th input.toggle")
    server.wait_for_commands(2)
    _box(page, ROWS[0]).click()
    server.wait_for_commands(3)
    _box(page, ROWS[2]).click(modifiers=["Shift"])
    server.wait_for_commands(4)

    assert _assignments(server.commands[3][1]) == {key: "on" for key in ROWS[:3]}
    for key in ROWS[:3]:
        expect(_box(page, key)).to_be_checked()
    expect(_box(page, ROWS[3])).not_to_be_checked()


def test_flush_resolves_with_the_last_acknowledged_revision(page):
    server = _DraftServer(page)
    server.open()

    _box(page, ROWS[0]).click()
    revision = page.evaluate("librenmsSyncDraft.flush()")

    assert revision == 6
    assert len(server.commands) == 1


def test_a_navigation_sends_the_batch_first_and_then_reads_the_page(page):
    server = _DraftServer(page)
    server.open()
    order = []
    page.on(
        "request",
        lambda request: order.append(request.url) if request.method == "POST" or "tab=cables" in request.url else None,
    )

    _box(page, ROWS[0]).click()
    page.click("#cables-tab")
    page.wait_for_url(f"{PAGE_URL}?tab=cables")

    assert order == [SELECTION_URL, f"{PAGE_URL}?tab=cables"]
    expect(_box(page, ROWS[0])).to_be_checked()


def test_a_conflict_drops_the_unsent_input_and_shows_the_draft_again(page):
    server = _DraftServer(page)
    server.open()
    server.change_elsewhere()

    _box(page, ROWS[0]).click()
    # The unsent input is dropped, so the page may read the draft again: flush resolves.
    settled = page.evaluate("librenmsSyncDraft.flush().then(() => 'resolved', () => 'rejected')")

    assert settled == "resolved"
    expect(page.locator('[data-draft-notice="conflict"]')).to_be_visible()
    expect(page.locator("#revision")).to_have_text("6")
    expect(_box(page, ROWS[0])).not_to_be_checked()
    _box(page, ROWS[1]).click()
    server.wait_for_commands(2)
    assert server.commands[1][1]["expected_revision"] == "6"


def test_a_failed_write_keeps_the_input_and_holds_navigation_until_a_retry_succeeds(page):
    server = _DraftServer(page)
    server.open()
    server.answers["selection"] = [500, 500]

    _box(page, ROWS[0]).click()
    expect(page.locator('[data-draft-notice="failed"]')).to_be_visible()
    # The navigation tries the write again first, and the second failure keeps the page where it is.
    page.click("#cables-tab")
    server.wait_for_commands(2)
    page.wait_for_timeout(300)

    assert page.url == f"{PAGE_URL}?tab=ipaddresses"
    assert server.reads == []
    expect(page.locator('[data-draft-notice="failed"]')).to_be_visible()
    page.click('[data-draft-notice="failed"] button')
    server.wait_for_commands(3)
    expect(page.locator("#librenms-draft-notice")).to_have_count(0)
    assert _assignments(server.commands[2][1]) == {ROWS[0]: "on"}
    assert server.checked == {ROWS[0]}
    page.click("#cables-tab")
    page.wait_for_url(f"{PAGE_URL}?tab=cables")


def test_a_vrf_choice_is_a_command_and_the_row_is_read_again(page):
    server = _DraftServer(page)
    server.open()
    rendered = page.locator(f'tr[data-row-key="{ROWS[1]}"]').get_attribute("data-rendered")

    page.select_option(f'tr[data-row-key="{ROWS[1]}"] select', "9")

    row = page.locator(f'tr[data-row-key="{ROWS[1]}"]')
    expect(row).not_to_have_attribute("data-rendered", rendered)
    expect(row.locator("select")).to_have_value("9")
    assert server.commands == [
        ("choice", {"draft_id": "draft-1", "expected_revision": "5", "row": ROWS[1], "value": "9"})
    ]
    assert server.reads == [f"{ROW_URL}?key={ROWS[1]}"]


def test_a_row_read_answer_after_a_later_input_is_dropped_and_the_row_is_read_again(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("row")
    page.select_option(f'tr[data-row-key="{ROWS[1]}"] select', "5")
    server.wait_until(lambda: server.held)
    rendered = page.locator(f'tr[data-row-key="{ROWS[1]}"]').get_attribute("data-rendered")

    _box(page, ROWS[2]).click()
    server.release("row")
    page.wait_for_timeout(300)
    assert page.locator(f'tr[data-row-key="{ROWS[1]}"]').get_attribute("data-rendered") == rendered
    server.hold.clear()
    while server.held:
        server.release("row")

    expect(page.locator(f'tr[data-row-key="{ROWS[1]}"]')).not_to_have_attribute("data-rendered", rendered)
    expect(_box(page, ROWS[2])).to_be_checked()
    assert [kind for kind, _fields in server.commands] == ["choice", "selection"]
    assert len(server.reads) == 2


def test_the_sync_submit_waits_for_the_batch_and_names_the_acknowledged_revision(page):
    server = _DraftServer(page)
    server.open()

    _box(page, ROWS[0]).click()
    page.click("#sync-selected")
    server.wait_until(lambda: server.submits)

    assert [kind for kind, _fields in server.commands] == ["selection"]
    assert (server.submits[0]["draft_id"], server.submits[0]["expected_revision"]) == ("draft-1", "6")


def test_a_submit_whose_input_is_not_saved_is_not_sent_and_its_button_works_again(page):
    server = _DraftServer(page)
    server.open()
    server.answers["selection"] = [500]

    _box(page, ROWS[0]).click()
    page.click("#sync-selected")

    expect(page.locator('[data-draft-notice="failed"]')).to_be_visible()
    expect(page.locator("#sync-selected")).to_be_enabled()
    page.wait_for_timeout(300)
    assert server.submits == []


def test_set_primary_ip_is_a_command_and_the_page_is_read_again(page):
    server = _DraftServer(page)
    server.open()

    page.check("#set-primary-ip-toggle-cb")

    expect(page.locator("#revision")).to_have_text("6")
    ((kind, fields),) = server.commands
    assert kind == "management"
    assert (fields["draft_id"], fields["expected_revision"], fields["selected"]) == ("draft-1", "5", "on")
    assert server.reads == [f"{PAGE_URL}?tab=ipaddresses"]


def test_the_unload_prompt_asks_only_while_a_command_is_unanswered(page):
    server = _DraftServer(page)
    server.open()
    prompt = """() => {
        const event = new Event('beforeunload', {cancelable: true});
        window.dispatchEvent(event);
        return event.defaultPrevented;
    }"""
    server.hold.add("selection")

    assert page.evaluate(prompt) is False
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    assert page.evaluate(prompt) is True
    server.release("selection")
    page.wait_for_function(f"() => !({prompt})()")


def test_the_unload_beacon_carries_the_unsent_rows_with_the_csrf_token(page):
    server = _DraftServer(page)
    server.open()

    _box(page, ROWS[3]).click()
    # The page leaves before the batch time ends.
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    server.wait_for_commands(1)
    page.wait_for_timeout(300)

    ((kind, fields),) = server.commands
    assert kind == "selection"
    assert fields["csrfmiddlewaretoken"] == "csrf-test"
    assert (fields["draft_id"], fields["expected_revision"]) == ("draft-1", "5")
    assert _assignments(fields) == {ROWS[3]: "on"}


def test_a_row_action_names_the_draft_revision_and_the_evidence_of_its_row(page):
    server = _DraftServer(page)
    server.open()

    page.locator(f'tr[data-row-key="{ROWS[1]}"] button[name="sync_one"]').click()
    server.wait_until(lambda: server.submits)

    (fields,) = server.submits
    assert (fields["sync_one"], fields["draft_id"], fields["expected_revision"]) == (ROWS[1], "draft-1", "5")
    assert fields["evidence"] == f"token-{ROWS[1]}-"


def test_no_row_action_goes_while_the_page_waits_for_the_draft_of_another_window(page):
    server = _DraftServer(page)
    server.open()
    server.change_elsewhere()
    server.hold.add("page")

    _box(page, ROWS[0]).click()
    expect(page.locator('[data-draft-notice="conflict"]')).to_be_visible()
    page.locator(f'tr[data-row-key="{ROWS[1]}"] button[name="sync_one"]').click()
    page.wait_for_timeout(300)

    assert server.submits == []
    server.hold.clear()
    server.release("page")
    expect(page.locator("#revision")).to_have_text("6")
    page.locator(f'tr[data-row-key="{ROWS[1]}"] button[name="sync_one"]').click()
    server.wait_until(lambda: server.submits)
    assert server.submits[0]["expected_revision"] == "6"


def test_input_during_a_bulk_submit_waits_for_the_claim_and_takes_its_revision(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("submit")
    _box(page, ROWS[0]).click()

    page.click("#sync-selected")
    server.wait_until(lambda: server.submits)
    _box(page, ROWS[2]).click()
    page.wait_for_timeout(400)
    assert [kind for kind, _fields in server.commands] == ["selection"]
    server.hold.clear()
    server.release("submit")
    server.wait_for_commands(2)

    # The claim took revision 7; the click during the submit goes with it, and nothing conflicts.
    assert server.commands[1][1]["expected_revision"] == "7"
    assert _assignments(server.commands[1][1]) == {ROWS[2]: "on"}
    page.wait_for_timeout(300)
    expect(page.locator("#librenms-draft-notice")).to_have_count(0)
    assert server.checked == {ROWS[2]}


def test_a_tab_fragment_read_before_a_later_input_does_not_replace_the_table(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("fragment")
    page.evaluate("document.getElementById('fragment-loader').dispatchEvent(new CustomEvent('librenms:load-fragment'))")
    server.wait_until(lambda: server.held)
    rendered = page.locator(f'tr[data-row-key="{ROWS[0]}"]').get_attribute("data-rendered")

    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    page.wait_for_timeout(200)
    server.hold.clear()
    server.release("fragment")

    # The stale fragment does not replace the row; the page reads the tab again, which shows the click.
    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#revision")).to_have_text("6")
    expect(_box(page, ROWS[0])).to_be_checked()
    assert page.locator(f'tr[data-row-key="{ROWS[0]}"]').get_attribute("data-rendered") != rendered
    _box(page, ROWS[1]).click()
    server.wait_for_commands(2)
    assert server.commands[1][1]["expected_revision"] == "6"


def test_a_refresh_answer_before_a_later_input_reads_the_tab_again(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("refresh")
    page.click("#refresh")
    server.wait_until(lambda: server.held)

    _box(page, ROWS[0]).click()
    page.wait_for_timeout(300)
    # The refresh is the command in flight, so the click waits for it.
    assert server.commands == []
    server.hold.clear()
    server.release("refresh")

    # The click went to the replaced draft and conflicts; the page shows the new draft.
    server.wait_for_commands(1)
    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#librenms-ipaddress-table")).to_have_attribute("data-draft-id", "draft-1+")
    _box(page, ROWS[1]).click()
    server.wait_for_commands(2)
    assert (server.commands[1][1]["draft_id"], server.commands[1][1]["expected_revision"]) == ("draft-1+", "0")


def _prompts_on_unload(page):
    return page.evaluate(
        """() => {
            const event = new Event('beforeunload', {cancelable: true});
            window.dispatchEvent(event);
            return event.defaultPrevented;
        }"""
    )


def test_the_unload_prompt_asks_for_unsent_rows_also_after_a_failed_write(page):
    server = _DraftServer(page)
    server.open()
    server.answers["selection"] = [500]

    # Within the batch time the click is not sent yet.
    assert page.evaluate(
        """() => {
            [...document.querySelectorAll('tr[data-row-key]')][0].querySelector('input[name="select"]').click();
            const event = new Event('beforeunload', {cancelable: true});
            window.dispatchEvent(event);
            return event.defaultPrevented;
        }"""
    )
    expect(page.locator('[data-draft-notice="failed"]')).to_be_visible()

    assert _prompts_on_unload(page) is True


def test_a_tab_click_after_a_waiting_filter_wins(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("selection")
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)

    page.evaluate("document.getElementById('ipaddresses_filter-form').requestSubmit()")
    page.click("#cables-tab")
    server.hold.clear()
    server.release("selection")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    page.wait_for_timeout(300)
    assert not any(url.startswith(FILTER_URL) for url in server.reads)
    assert page.url == f"{PAGE_URL}?tab=cables"


def test_a_command_answer_while_a_tab_click_waits_keeps_the_tab_history_entry(page):
    server = _DraftServer(page)
    server.open()
    entries = page.evaluate("history.length")
    server.hold.add("management")
    page.check("#set-primary-ip-toggle-cb")
    server.wait_for_commands(1)

    page.click("#cables-tab")
    server.hold.clear()
    server.release("management")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    page.wait_for_timeout(300)
    assert page.evaluate("history.length") == entries + 1
    assert [url for url in server.reads if url.startswith(PAGE_URL)] == [f"{PAGE_URL}?tab=cables"]
    page.go_back()
    page.wait_for_url(f"{PAGE_URL}?tab=ipaddresses")


def test_a_submit_answer_without_a_revision_reads_the_tab_before_any_more_input(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("submit")
    server.changed_during_submit = True
    page.click("#sync-selected")
    server.wait_until(lambda: server.submits)
    _box(page, ROWS[1]).click()
    server.hold.clear()
    server.release("submit")

    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#revision")).to_have_text("7")
    page.wait_for_timeout(300)
    assert server.commands == []


def test_a_conflict_while_a_tab_click_waits_makes_the_tab_click_the_read_of_the_changed_draft(page):
    server = _DraftServer(page)
    server.open()
    server.change_elsewhere()
    server.hold.add("selection")
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)

    page.click("#cables-tab")
    server.hold.clear()
    server.release("selection")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator('[data-draft-notice="conflict"]')).to_be_visible()
    expect(page.locator("#revision")).to_have_text("6")
    page.wait_for_timeout(300)
    assert [url for url in server.reads if url.startswith(PAGE_URL)] == [f"{PAGE_URL}?tab=cables"]
    # The page takes input again, on the draft that it shows now.
    _box(page, ROWS[1]).click()
    server.wait_for_commands(2)
    assert server.commands[1][1]["expected_revision"] == "6"


def test_a_tab_click_wins_over_a_filter_that_htmx_queued_behind_a_running_filter(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("filter")
    page.evaluate("document.getElementById('ipaddresses_filter-form').requestSubmit()")
    server.wait_until(lambda: server.held)
    page.evaluate(
        """() => {
            document.getElementById('filter-q').value = '198.18.0';
            document.getElementById('ipaddresses_filter-form').requestSubmit();
        }"""
    )
    page.wait_for_timeout(200)
    # htmx queues the second filter behind the first one on the navigation loader.
    assert len([url for url in server.reads if url.startswith(FILTER_URL)]) == 1
    server.hold.clear()

    page.click("#cables-tab")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    page.wait_for_timeout(500)
    assert page.url == f"{PAGE_URL}?tab=cables"
    assert len([url for url in server.reads if url.startswith(FILTER_URL)]) == 1
    assert server.errors == []


def test_a_row_cleared_after_a_command_is_sent_after_that_command(page):
    server = _DraftServer(page)
    server.open()
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    server.hold.add("selection")
    _box(page, ROWS[1]).click()
    server.wait_for_commands(2)

    # While the second batch waits for its answer: Set Primary IP, then the first row is cleared.
    page.check("#set-primary-ip-toggle-cb")
    _box(page, ROWS[0]).click()
    server.hold.clear()
    server.release("selection")
    server.wait_for_commands(4)

    assert [kind for kind, _fields in server.commands] == ["selection", "selection", "management", "selection"]
    assert _assignments(server.commands[3][1]) == {ROWS[0]: "none"}
    assert server.commands[3][1]["expected_revision"] == "8"


def test_a_queued_filter_whose_form_left_the_page_does_not_hold_back_the_read_of_the_results(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("filter")
    page.evaluate("document.getElementById('ipaddresses_filter-form').requestSubmit()")
    server.wait_until(lambda: server.held)
    page.evaluate("document.getElementById('ipaddresses_filter-form').requestSubmit()")
    page.evaluate("document.getElementById('ipaddresses_filter-form').remove()")

    page.check("#set-primary-ip-toggle-cb")

    # The command asks for the results again; the queued filter will never go, so the page reads them itself.
    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#revision")).to_have_text("6")


def test_a_filter_that_another_script_cancels_does_not_hold_back_the_read_of_the_results(page):
    server = _DraftServer(page)
    server.open()
    page.evaluate(
        """() => document.addEventListener('htmx:configRequest', (event) => {
            if (event.detail.elt.matches('.sync-filter-form')) event.preventDefault();
        })"""
    )
    page.evaluate("document.getElementById('ipaddresses_filter-form').requestSubmit()")
    page.wait_for_timeout(200)
    assert server.reads == []

    page.check("#set-primary-ip-toggle-cb")

    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#revision")).to_have_text("6")


def test_a_click_on_a_table_that_a_navigation_just_inserted_is_saved(page):
    server = _DraftServer(page)
    server.shows_table = False
    server.open()
    server.shows_table = True
    # The click comes after htmx inserted the new table and before htmx settled it.
    page.evaluate(
        """() => {
            document.addEventListener('htmx:afterSettle', () => { window.__settled = true; });
            document.addEventListener('htmx:afterSwap', () => setTimeout(() => {
                const box = document.querySelector('tr[data-row-key] input[name="select"]');
                if (!box || window.__clicked !== undefined) return;
                window.__clicked = !window.__settled;
                box.click();
            }, 0));
        }"""
    )

    page.click("#cables-tab")

    page.wait_for_function("window.__clicked !== undefined")
    assert page.evaluate("window.__clicked") is True
    server.wait_for_commands(1)
    assert _assignments(server.commands[0][1]) == {ROWS[0]: "on"}
    assert server.commands[0][1]["draft_id"] == "draft-1"
    assert server.checked == {ROWS[0]}


def test_a_click_on_a_table_whose_draft_the_writer_did_not_take_reads_the_tab_and_says_so(page):
    server = _DraftServer(page)
    server.open()
    # A copy of the table that no READ installed, so the writer did not take it.
    page.evaluate(
        """() => {
            const content = document.getElementById('ipaddress-sync-content');
            content.innerHTML = content.innerHTML;
        }"""
    )

    _box(page, ROWS[0]).click()

    expect(page.locator("#librenms-draft-notice")).to_be_visible()
    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#revision")).to_have_text("5")
    expect(_box(page, ROWS[0])).not_to_be_checked()
    page.wait_for_timeout(300)
    assert server.commands == []


def test_a_page_that_back_restores_from_the_cache_reads_the_draft_again(bfcache_page):
    page = bfcache_page
    server = _DraftServer(page)
    server.open()
    other_page = "https://plugin.example.com/dcim/devices/1/"
    page.route(other_page, lambda route: route.fulfill(status=200, content_type="text/html", body="<p>Device</p>"))
    page.evaluate("window.__kept = true")
    page.goto(other_page)
    # Another window selects a row while this page is in the cache.
    server.checked.add(ROWS[2])
    server.change_elsewhere()

    page.go_back(wait_until="commit")

    # The browser restored the document itself, not a new load of it.
    page.wait_for_function("window.__kept === true")
    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#revision")).to_have_text("6")
    expect(_box(page, ROWS[2])).to_be_checked()
    assert page.url == f"{PAGE_URL}?tab=ipaddresses"


def test_a_choice_made_before_a_row_settles_is_kept_for_the_next_choice(page):
    server = _DraftServer(page)
    server.open()
    row = f'tr[data-row-key="{ROWS[1]}"]'
    server.hold.add("row")
    page.select_option(f"{row} select", "5")
    server.wait_until(lambda: server.held)
    # Choose Red right after htmx inserts the row read of Blue, before htmx settles it; its answer waits.
    page.evaluate(
        """key => {
            document.addEventListener('htmx:afterSettle', () => { window.__settled = true; }, { once: true });
            document.addEventListener('htmx:afterSwap', () => setTimeout(() => {
                window.__choseBeforeSettle = !window.__settled;
                const select = [...document.querySelectorAll('tr[data-row-key]')]
                    .find(row => row.dataset.rowKey === key).querySelector('select');
                select.value = '9';
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }, 0), { once: true });
        }""",
        ROWS[1],
    )
    server.hold = {"choice"}
    server.release("row")
    server.wait_for_commands(2)
    page.wait_for_function("window.__settled === true")
    assert page.evaluate("window.__choseBeforeSettle") is True

    # Back to Blue while the Red command waits for its answer.
    page.select_option(f"{row} select", "5")
    server.hold.clear()
    server.release("choice")

    server.wait_for_commands(3)
    assert [fields["value"] for _kind, fields in server.commands] == ["5", "9", "5"]
    expect(page.locator(f"{row} select")).to_have_value("5")
    assert server.vrfs[ROWS[1]] == "5"


def test_set_primary_ip_sends_the_shown_evidence_and_a_submit_before_the_next_read_takes_its_revision(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("page")

    page.check("#set-primary-ip-toggle-cb")
    server.wait_for_commands(1)
    # The page reads the tab after the command; that read waits, and the user submits meanwhile.
    server.wait_until(lambda: server.held)
    page.click("#sync-selected")
    server.wait_until(lambda: server.submits)

    ((kind, fields),) = server.commands
    assert kind == "management"
    # A shown row gives the token that the row shows; a row that no page shows gives the table's token.
    assert json.loads(fields["evidence"]) == {
        MANAGEMENT_ROW: f"token-{MANAGEMENT_ROW}-",
        OFF_PAGE_MANAGEMENT_ROW: f"token-{OFF_PAGE_MANAGEMENT_ROW}-",
    }
    assert server.submits[0]["expected_revision"] == "6"
    assert server.errors == []


def test_a_navigation_answer_from_before_a_bulk_submit_does_not_bring_back_the_claimed_rows(page):
    server = _DraftServer(page)
    server.open()
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    page.wait_for_function("librenmsSyncDraft.flush().then(revision => revision === 6)")
    # The server renders the tab click with the row selected, and its answer waits.
    server.hold.add("page")
    page.click("#cables-tab")
    server.wait_until(lambda: server.held)

    # The form is still on the page: the bulk submit claims the row and answers with the next revision.
    page.click("#sync-selected")
    server.wait_until(lambda: server.submits)
    page.wait_for_function("librenmsSyncDraft.flush().then(revision => revision === 7)")
    page.evaluate(
        """() => {
            window.__shown = [];
            document.addEventListener('htmx:afterSwap', () => {
                window.__shown.push(document.querySelector('#librenms-ipaddress-table')?.dataset.draftRevision);
            });
        }"""
    )
    server.hold.clear()
    server.release("page")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator("#revision")).to_have_text("7")
    # The answer from before the claim is never shown; the tab is read again after the claim.
    assert page.evaluate("window.__shown") == ["7"]
    expect(_box(page, ROWS[0])).not_to_be_checked()
    _box(page, ROWS[1]).click()
    server.wait_for_commands(2)
    assert server.commands[1][1]["expected_revision"] == "7"
    page.wait_for_timeout(300)
    assert server.checked == {ROWS[1]}
    expect(page.locator("#librenms-draft-notice")).to_have_count(0)


def test_a_table_of_an_older_revision_does_not_take_the_writer_back(page):
    server = _DraftServer(page)
    server.open()
    _box(page, ROWS[0]).click()
    page.wait_for_function("librenmsSyncDraft.flush().then(revision => revision === 6)")
    older = server.content().replace('data-draft-revision="6"', 'data-draft-revision="5"')
    page.route(FRAGMENT_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=older))

    page.evaluate("document.getElementById('fragment-loader').dispatchEvent(new CustomEvent('librenms:load-fragment'))")

    # The page reads the tab again, and the next input names the revision that the writer had.
    server.wait_until(lambda: any(url.startswith(PAGE_URL) for url in server.reads))
    expect(page.locator("#librenms-ipaddress-table")).to_have_attribute("data-draft-revision", "6")
    _box(page, ROWS[1]).click()
    server.wait_for_commands(2)
    assert server.commands[1][1]["expected_revision"] == "6"
    expect(page.locator("#librenms-draft-notice")).to_have_count(0)


def _row_action(page, key):
    page.locator(f'tr[data-row-key="{key}"] button[name="sync_one"]').click()


def test_a_row_action_answer_after_a_newer_navigation_does_not_take_the_page_back(page):
    server = _DraftServer(page)
    server.open()
    page.evaluate("window.__sameDocument = true")
    server.hold.add("submit")
    _row_action(page, ROWS[1])
    server.wait_until(lambda: server.submits)

    page.click("#cables-tab")
    server.hold.clear()
    server.release("submit")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator(f'tr[data-row-key="{ROWS[1]}"] td[data-col="status"]')).to_have_text("Synced")
    page.wait_for_timeout(300)
    assert page.url == f"{PAGE_URL}?tab=cables"
    assert page.evaluate("window.__sameDocument") is True
    expect(page.locator("#django-messages .toast")).to_have_count(1)


def test_a_navigation_during_a_row_action_waits_for_it_and_reads_the_tab_after_it(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("submit")
    _row_action(page, ROWS[1])
    server.wait_until(lambda: server.submits)

    page.click("#cables-tab")
    page.wait_for_timeout(300)
    # The row action is the writer's command in flight, so the navigation waits for it.
    assert not any(url.startswith(PAGE_URL) for url in server.reads)
    server.hold.clear()
    server.release("submit")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator(f'tr[data-row-key="{ROWS[1]}"] td[data-col="status"]')).to_have_text("Synced")
    page.wait_for_timeout(300)
    assert [url for url in server.reads if url.startswith(PAGE_URL)] == [f"{PAGE_URL}?tab=cables"]


def test_a_read_sent_before_a_row_action_is_read_again_after_it(page):
    server = _DraftServer(page)
    server.open()
    # The server renders the tab click before the row action, and its answer waits.
    server.hold.add("page")
    page.click("#cables-tab")
    server.wait_until(lambda: server.held)

    _row_action(page, ROWS[1])
    server.wait_until(lambda: server.submits)
    page.wait_for_function("librenmsSyncDraft.flush().then(() => true)")
    server.hold.clear()
    server.release("page")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator(f'tr[data-row-key="{ROWS[1]}"] td[data-col="status"]')).to_have_text("Synced")


def test_a_confirmation_in_the_dialog_closes_it_and_reads_the_tab_again(page):
    server = _DraftServer(page)
    server.conflicts.add(ROWS[1])
    server.open()
    _row_action(page, ROWS[1])
    expect(page.locator("#htmx-modal")).to_be_visible()

    page.click("#confirm-change")

    expect(page.locator("#htmx-modal")).to_be_hidden()
    expect(page.locator(f'tr[data-row-key="{ROWS[1]}"] td[data-col="status"]')).to_have_text("Synced")
    assert page.url == f"{PAGE_URL}?tab=ipaddresses"
    assert server.errors == []


def test_a_navigation_answer_from_before_a_confirmation_is_read_again_after_it(page):
    server = _DraftServer(page)
    server.conflicts.add(ROWS[1])
    server.open()
    _row_action(page, ROWS[1])
    expect(page.locator("#htmx-modal")).to_be_visible()
    # A navigation starts while the dialog is open; the server renders it before the confirmation.
    server.hold.add("page")
    page.evaluate("document.getElementById('cables-tab').click()")
    server.wait_until(lambda: server.held)

    page.click("#confirm-change")
    server.wait_until(lambda: len(server.submits) == 2)
    expect(page.locator("#htmx-modal")).to_be_hidden()
    server.hold.clear()
    server.release("page")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator(f'tr[data-row-key="{ROWS[1]}"] td[data-col="status"]')).to_have_text("Synced")
    assert server.errors == []


def test_a_navigation_answer_from_before_a_refresh_is_read_again_after_it(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("page")
    page.click("#cables-tab")
    server.wait_until(lambda: server.held)

    page.evaluate("document.getElementById('refresh').click()")
    page.wait_for_function("librenmsSyncDraft.flush().then(() => true)")
    server.hold.clear()
    server.release("page")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator("#librenms-ipaddress-table")).to_have_attribute("data-draft-id", "draft-1+")
    _box(page, ROWS[1]).click()
    server.wait_for_commands(1)
    assert server.commands[0][1]["draft_id"] == "draft-1+"
    expect(page.locator("#librenms-draft-notice")).to_have_count(0)


def test_a_navigation_that_waits_for_a_move_wins_over_the_move_answer(page):
    server = _DraftServer(page)
    server.open()
    page.evaluate("window.__sameDocument = true")
    server.hold.add("move")
    page.click("#move")
    # The move goes only after the user confirms it.
    page.wait_for_timeout(200)
    assert server.held == []
    page.click("#librenms-confirm-modal [data-confirm-accept]")
    server.wait_until(lambda: server.held)

    server.hold.add("page")
    page.click("#cables-tab")
    page.wait_for_timeout(200)
    server.hold.discard("move")
    server.release("move")
    server.wait_until(lambda: any(kind == "page" for kind, _route, _answer in server.held))
    server.hold.clear()
    server.release("page")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator("#moved")).to_have_text("moved")
    page.wait_for_timeout(300)
    assert page.url == f"{PAGE_URL}?tab=cables"
    assert page.evaluate("window.__sameDocument") is True
    assert server.errors == []


def test_a_move_answer_reads_the_current_page_again_and_a_cancelled_move_sends_nothing(page):
    server = _DraftServer(page)
    server.open()
    page.click("#move")
    page.click("#librenms-confirm-modal [data-bs-dismiss='modal'].btn-secondary")
    page.wait_for_timeout(200)
    assert server.moved is False

    page.click("#move")
    page.click("#librenms-confirm-modal [data-confirm-accept]")

    # The tab that the move answer names is not a URL: the page reads the page it shows, on any tab.
    expect(page.locator("#moved")).to_have_text("moved")
    assert [url for url in server.reads if url.startswith(PAGE_URL)] == [f"{PAGE_URL}?tab=ipaddresses"]
    expect(page.locator("#django-messages .toast")).to_have_count(1)


def _page_reads(server):
    return [url for url in server.reads if url.startswith(PAGE_URL)]


def test_an_action_and_a_navigation_that_wait_for_one_write_run_in_order(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("selection")
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    server.hold.update({"submit", "page"})

    # Both wait for the write: the row action first, then the tab click.
    _row_action(page, ROWS[1])
    page.click("#cables-tab")
    page.wait_for_timeout(200)
    assert server.submits == [] and _page_reads(server) == []
    server.hold.discard("selection")
    server.release("selection")

    server.wait_until(lambda: server.submits)
    page.wait_for_timeout(300)
    # The tab click waits for the answer of the row action.
    assert _page_reads(server) == []
    server.hold.discard("submit")
    server.release("submit")
    server.wait_until(lambda: _page_reads(server))
    server.hold.clear()
    server.release("page")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator(f'tr[data-row-key="{ROWS[1]}"] td[data-col="status"]')).to_have_text("Synced")
    expect(_box(page, ROWS[0])).to_be_checked()
    assert server.errors == []


def test_two_commands_that_wait_for_one_write_go_one_after_the_other(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("selection")
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    server.hold.add("submit")
    _row_action(page, ROWS[1])
    _row_action(page, ROWS[2])
    server.hold.discard("selection")
    server.release("selection")

    server.wait_until(lambda: server.submits)
    # While the first command runs, the second one and a later tab click wait for it.
    page.click("#cables-tab")
    page.wait_for_timeout(300)
    assert [fields["sync_one"] for fields in server.submits] == [ROWS[1]]
    assert _page_reads(server) == []
    server.release("submit")
    server.wait_until(lambda: len(server.submits) == 2)
    assert [fields["sync_one"] for fields in server.submits] == [ROWS[1], ROWS[2]]
    page.wait_for_timeout(300)
    assert _page_reads(server) == []
    server.hold.clear()
    server.release("submit")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    for key in ROWS[1:3]:
        expect(page.locator(f'tr[data-row-key="{key}"] td[data-col="status"]')).to_have_text("Synced")
    assert server.errors == []


def test_a_row_action_that_a_refresh_replaced_while_it_waited_is_refused_and_input_works_again(page):
    server = _DraftServer(page)
    server.open()
    server.hold.add("refresh")
    page.click("#refresh")
    server.wait_until(lambda: server.held)
    # A confirmed row action waits for the refresh in flight.
    page.locator(f'tr[data-row-key="{ROWS[2]}"] button[name="create_vrf"]').click()
    page.click("#librenms-confirm-modal [data-confirm-accept]")
    page.wait_for_timeout(200)
    assert server.submits == []

    server.hold.clear()
    server.release("refresh")

    # The refresh replaced the action's form: the page says the action was not sent, and sends nothing.
    expect(page.locator("#librenms-draft-notice")).to_be_visible()
    page.wait_for_timeout(300)
    assert server.submits == []
    expect(page.locator("#librenms-ipaddress-table")).to_have_attribute("data-draft-id", "draft-1+")
    _box(page, ROWS[0]).click()
    server.wait_for_commands(1)
    assert (server.commands[0][1]["draft_id"], server.commands[0][1]["expected_revision"]) == ("draft-1+", "0")


def test_clear_on_the_offpage_notice_clears_those_rows_and_reads_the_tab_again(page):
    server = _DraftServer(page)
    server.checked = {OFF_PAGE_MANAGEMENT_ROW}
    server.open()

    page.click("[data-clear-offpage]")

    server.wait_for_commands(1)
    assert _assignments(server.commands[0][1]) == {OFF_PAGE_MANAGEMENT_ROW: "none"}
    expect(page.locator("[data-offpage-selection]")).to_have_count(0)
    assert _page_reads(server) == [f"{PAGE_URL}?tab=ipaddresses"]
    assert server.checked == set()


def test_a_page_read_keeps_the_messages_that_the_last_request_showed(page):
    server = _DraftServer(page)
    server.open()
    # The messages of the last request; NetBox's page, which a READ answers with, carries an empty container.
    page.evaluate(
        "document.getElementById('django-messages').innerHTML = '<div class=\"toast\" id=\"shown\">Synced</div>'"
    )

    page.click("#cables-tab")

    page.wait_for_url(f"{PAGE_URL}?tab=cables")
    expect(page.locator("#django-messages #shown")).to_have_count(1)
    expect(page.locator("#cables-tab")).to_have_count(1)
