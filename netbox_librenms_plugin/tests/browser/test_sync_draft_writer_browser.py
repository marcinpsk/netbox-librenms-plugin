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
ROWS = ("198.18.0.1/24", "198.18.0.2/24", "198.18.0.3/24", "198.18.0.4/24")


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
    """Keep one draft and serve the page, the row reads and the draft commands from it; hold requests on demand."""

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
        }
        for url, kind in routes.items():
            page.route(url, self._handler(kind))

    def _handler(self, kind):
        # Playwright passes the request as a second argument to a handler that takes two.
        return lambda route: self._take(kind, route)

    def _take(self, kind, route):
        request = route.request
        if kind == "page" and request.headers.get("hx-request") == "true":
            self.reads.append(request.url)
        elif kind == "row":
            self.reads.append(request.url)
        elif kind in ("selection", "choice", "management"):
            self.commands.append((kind, _form_fields(request)))
        elif kind == "submit":
            self.submits.append(_form_fields(request))
        if kind in self.hold:
            self.held.append((kind, route))
            return
        self.answer(kind, route)

    def answer(self, kind, route):
        """Answer one request from the draft as the plugin does."""
        if kind == "page":
            route.fulfill(status=200, content_type="text/html", body=_sync_document(self.body()))
        elif kind == "row":
            key = parse_qs(urlsplit(route.request.url).query)["key"][0]
            route.fulfill(status=200, content_type="text/html", body=self.row(key))
        elif kind == "submit":
            route.fulfill(status=200, content_type="text/html", body="", headers={"HX-Reswap": "none"})
        else:
            self._command(kind, route)

    def _command(self, kind, route):
        status = (self.answers.get(kind) or [204]).pop(0)
        fields = _form_fields(route.request)
        if status == 204 and int(fields["expected_revision"]) != self.revision:
            status = 409
        if status != 204:
            route.fulfill(status=status, body="")
            return
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
        route.fulfill(status=204, body="", headers=headers)

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
        return f"""<tr data-row-key="{key}" data-evidence="token-{key}-{vrf}" data-rendered="{self.renders}"
            hx-get="{ROW_URL}?key={key}" hx-trigger="librenms:row-read" hx-target="this" hx-swap="outerHTML"
            hx-sync="this:replace" hx-params="none">
            <td data-col="selection"><input type="checkbox" name="select" value="{key}"{checked}></td>
            <td data-col="vrf"><select class="form-select vrf-select">{options}</select></td></tr>"""

    def body(self):
        rows = "".join(self.row(key) for key in ROWS)
        return f"""{_navigation_loader()}
            <div id="librenms-sync-tabs" data-active-tab="ipaddresses">
              <a id="cables-tab" data-sync-nav href="?tab=cables">Cables</a>
              <output id="revision">{self.revision}</output>
              <input type="checkbox" id="set-primary-ip-toggle-cb">
              <form id="ipaddress-sync-form" method="post" action="{SUBMIT_URL}" hx-post="{SUBMIT_URL}"
                    hx-target="#htmx-modal-content" hx-swap="innerHTML">
                <input type="hidden" name="csrfmiddlewaretoken" value="csrf-test">
                <table id="librenms-ipaddress-table" data-draft-id="{self.draft_id}"
                       data-draft-revision="{self.revision}" data-selection-url="{escape(SELECTION_URL)}"
                       data-choice-url="{escape(CHOICE_URL)}" data-management-url="{escape(MANAGEMENT_URL)}">
                  <thead><tr><th><input type="checkbox" class="toggle"></th><th>VRF</th></tr></thead>
                  <tbody>{rows}</tbody>
                </table>
                <button id="sync-selected" type="submit"><span class="spinner spinner-border d-none"></span>Sync</button>
              </form>
            </div>
            <div id="htmx-modal-content"></div>"""

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
        index = next(i for i, (held_kind, _route) in enumerate(self.held) if held_kind == kind)
        _kind, route = self.held.pop(index)
        self.answer(kind, route)


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
    rejected = page.evaluate("librenmsSyncDraft.flush().then(() => 'resolved', () => 'rejected')")

    assert rejected == "rejected"
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
    assert server.commands == [("management", {"draft_id": "draft-1", "expected_revision": "5", "selected": "on"})]
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
