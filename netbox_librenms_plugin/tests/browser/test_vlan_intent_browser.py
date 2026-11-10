"""Browser checks for the VLAN row tokens: a changed choice, the pending state, and what a submit posts."""

import json

from test_sync_cache_browser import _add_page_scripts, _selection_form_pairs

PAGE_URL = "https://plugin.example.com/dcim/devices/7/librenms-sync/"
SYNC_URL = "https://plugin.example.com/vlan-sync"
VERIFY_ROUTE = "**/plugins/librenms_plugin/verify-vlan-sync-group/"


def _row(vid, *, checked=False):
    return f"""
        <tr data-vlan-id="{vid}">
          <td data-col="selection"><input type="checkbox" name="select" value="{vid}"{" checked" if checked else ""}></td>
          <td data-col="vlan_id"><span class="text-danger">{vid}</span></td>
          <td data-col="name"><span class="text-danger">VLAN {vid}</span></td>
          <td data-col="vlan_group_selection">
            <input type="hidden" name="confirmation_intent" value="vlan:rendered-{vid}" disabled>
            <select class="vlan-sync-group-select" name="vlan_group_{vid}" data-vlan-id="{vid}" data-vlan-name="VLAN {vid}">
              <option value="" selected>Global</option><option value="1">Group A</option><option value="2">Group B</option>
            </select>
          </td>
          <td data-col="status"><button id="sync-one-{vid}" type="submit" name="sync_one" value="{vid}">Create</button></td>
        </tr>"""


def _load(page, rows):
    html = f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="vlan-csrf-token">
        <div id="librenms-sync-tabs" data-server-key="default"></div>
        <button id="toolbar-sync" type="submit" form="vlan-sync-form">Sync Selected VLANs</button>
        <div id="tab-content">
          <form id="vlan-sync-form" method="post" action="{SYNC_URL}" hx-post="{SYNC_URL}" hx-target="#tab-content">
            <input type="hidden" name="server_key" value="default">
            <table id="librenms-vlan-table"><tbody>{"".join(rows)}</tbody></table>
          </form>
        </div></body></html>"""
    page.route(PAGE_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    page.goto(PAGE_URL)
    _add_page_scripts(page)
    page.evaluate("initializeVlanSyncGroupSelects()")


def _token(page, vid):
    return page.locator(f'tr[data-vlan-id="{vid}"] input[name="confirmation_intent"]').input_value()


def _answer(route, group, token):
    body = {
        "status": "success",
        "vlan_group_id": group,
        "css_class": "text-danger",
        "exists_in_netbox": False,
        "name_matches": False,
        "status_html": f'<button id="answered-{group}" type="submit" name="sync_one" value="501">Create</button>',
        "confirmation_intent": token,
    }
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _wait_for(page, condition):
    for _attempt in range(200):
        if condition():
            return
        page.wait_for_timeout(10)
    raise AssertionError("the page did not reach the expected state")


def test_a_changed_choice_drops_the_token_at_once_and_holds_apply_until_its_answer(page):
    _load(page, [_row(501)])
    held = []
    page.route(VERIFY_ROUTE, lambda route: held.append(route))

    page.locator(".vlan-sync-group-select").select_option("1")
    _wait_for(page, lambda: len(held) == 1)

    assert _token(page, 501) == ""
    assert page.locator("#toolbar-sync").is_disabled()
    assert page.locator("#sync-one-501").is_disabled()
    assert held[0].request.post_data_json == {
        "device_id": "7",
        "server_key": "default",
        "vid": "501",
        "vlan_group_id": "1",
        "sync_actions": True,
    }

    _answer(held[0], 1, "vlan:group-a")
    page.locator("#answered-1").wait_for()

    assert _token(page, 501) == "vlan:group-a"
    assert not page.locator("#toolbar-sync").is_disabled()
    assert not page.locator("#answered-1").is_disabled()


def test_an_answer_for_another_choice_sets_no_token(page):
    _load(page, [_row(501)])
    held = []
    page.route(VERIFY_ROUTE, lambda route: held.append(route))

    page.locator(".vlan-sync-group-select").select_option("1")
    _wait_for(page, lambda: len(held) == 1)
    _answer(held[0], 2, "vlan:group-b")
    page.wait_for_timeout(100)

    assert _token(page, 501) == ""
    assert page.locator("#answered-2").count() == 0
    assert page.locator("#toolbar-sync").is_disabled()


def test_a_submit_while_a_row_waits_for_its_answer_sends_nothing(page):
    _load(page, [_row(501, checked=True)])
    sent = []
    page.route(VERIFY_ROUTE, lambda route: None)
    page.route(SYNC_URL, lambda route: sent.append(route))

    page.locator(".vlan-sync-group-select").select_option("1")
    _wait_for(page, lambda: _token(page, 501) == "")
    page.locator("#vlan-sync-form").evaluate("form => form.requestSubmit()")
    page.wait_for_timeout(200)

    assert sent == []


def test_a_bulk_submit_posts_the_tokens_of_the_checked_rows_and_of_the_rows_on_other_pages(page):
    bodies = []

    def answer(route):
        bodies.append(route.request.post_data)
        route.fulfill(status=200, content_type="text/html", body='<span id="swapped">Synced</span>')

    _load(page, [_row(501, checked=True), _row(502)])
    page.route(SYNC_URL, answer)
    page.evaluate(
        """() => writeStoredSelection(document.getElementById('librenms-vlan-table'), {
            '503': {inputs: {vlan_group_503: '2', confirmation_intent: 'vlan:stored-503'}, auto: ''}
        })"""
    )

    page.click("#toolbar-sync")
    page.locator("#swapped").wait_for()

    tokens = [value for key, value in _selection_form_pairs(bodies[0]) if key == "confirmation_intent"]
    assert sorted(tokens) == ["vlan:rendered-501", "vlan:stored-503"]


def test_a_row_button_posts_only_its_own_token(page):
    bodies = []

    def answer(route):
        bodies.append(route.request.post_data)
        route.fulfill(status=200, content_type="text/html", body='<span id="swapped">Synced</span>')

    _load(page, [_row(501, checked=True), _row(502)])
    page.route(SYNC_URL, answer)

    page.click("#sync-one-502")
    page.locator("#swapped").wait_for()

    pairs = _selection_form_pairs(bodies[0])
    assert [value for key, value in pairs if key == "confirmation_intent"] == ["vlan:rendered-502"]
    assert ("sync_one", "502") in pairs
