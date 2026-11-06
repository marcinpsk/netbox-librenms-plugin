"""
The IP tab's selection draft, end to end: the draft commands, the row read, and the bulk submit that claims the draft.

Each test refreshes the real tab from a loopback LibreNMS, then drives the real endpoints with a signed-in
client: the tab render, the selection and choice commands, the row read and the sync POST. NetBox rows are real.
"""

import json
import re
from html import unescape

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from ipam.models import VRF, IPAddress

from netbox_librenms_plugin.selection_draft import Assignment, Intent, read_evidence
from netbox_librenms_plugin.sync_rows import IP_TAB
from netbox_librenms_plugin.tests._html_helpers import open_tags
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_superuser,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _ip_row, _serve_librenms_ip_rows
from netbox_librenms_plugin.tests import test_ip_row_vrf_create
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    ip_tab,
    make_user_with_perms,
    select_ip_rows,
    submit_ip_selection,
    answers_in_place,
    switch_set_primary_ip,
)

pytestmark = pytest.mark.django_db

# A fixture: a tab refreshed from a recording whose rows offer Create VRF.
seeded = test_ip_row_vrf_create.seeded
RD_ROWS = test_ip_row_vrf_create.RD_ROWS

SERVER_KEY = "default"
HTMX = {"HTTP_HX_REQUEST": "true"}
CREATE_ROW = "198.18.60.10/24"
NO_INTERFACE_ROW = "198.18.60.11/24"
MANAGEMENT_ROW = "198.18.60.12/24"


def _refreshed(client, live_librenms, name, rows, *, management_ip="198.18.0.254"):
    """Serve *rows* from the loopback LibreNMS and refresh the device's IP tab as a user does it."""
    _serve_librenms_ip_rows(live_librenms.server, rows, device_name=name, management_ip=management_ip)
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 42}})
    for row in rows:
        if row["interface"].startswith("eth"):
            make_interface(device, row["interface"], iface_type="1000base-t")
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
        **HTMX,
    )
    assert response.status_code == 200
    return device


@pytest.fixture
def tab(client, live_librenms):
    """Return a device whose tab has a row to create on eth10, a row without a NetBox interface and the management row."""
    client.force_login(make_superuser("ip-draft-user"))
    rows = [
        _ip_row("198.18.60.10", 7610, "eth10"),
        _ip_row("198.18.60.11", 7611, "lo-missing"),
        _ip_row("198.18.60.12", 7612, "eth12"),
    ]
    return _refreshed(client, live_librenms, "ip-draft-device", rows, management_ip="198.18.60.12")


def _checked(tab_state, row_key):
    (box,) = [
        attrs for attrs in open_tags(_row_html(tab_state.html, row_key), "input") if attrs.get("name") == "select"
    ]
    return "checked" in box


def _row_html(html, row_key):
    for match in re.finditer(r"<tr\b[^>]*>.*?</tr>", html, re.S):
        if f'data-row-key="{row_key}"' in unescape(match.group(0)):
            return match.group(0)
    raise AssertionError(f"no row {row_key}")


def _command(client, url, **fields):
    return client.post(url, fields)


def _select(client, state, rows, revision=None):
    assignments = {row: {"state": "on", "token": state.rows[row]["data-evidence"]} for row in rows}
    return _command(
        client,
        state.table["data-selection-url"],
        draft_id=state.table["data-draft-id"],
        expected_revision=state.table["data-draft-revision"] if revision is None else revision,
        assignments=json.dumps(assignments),
    )


def _choose(client, state, row, value, revision=None):
    return _command(
        client,
        state.table["data-choice-url"],
        draft_id=state.table["data-draft-id"],
        expected_revision=state.table["data-draft-revision"] if revision is None else revision,
        row=row,
        value=value,
    )


def _row_read(client, row_url):
    return client.get(unescape(row_url))


def _normalized_row(html):
    """Return the row markup with its whitespace collapsed and its evidence token replaced by the token's payload."""

    def payload(match):
        return f'data-evidence="{json.dumps(read_evidence(IP_TAB, unescape(match.group(1))), sort_keys=True)}"'

    return re.sub(r"\s+", " ", re.sub(r'data-evidence="([^"]+)"', payload, html)).strip()


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class TestTheRowRead:
    """The row read answers the row exactly as the tab renders it, for the same draft."""

    def test_a_row_read_is_the_row_of_the_tab_render(self, client, tab):
        blue = VRF.objects.create(name="draft-blue")
        state = ip_tab(client, tab)
        revision = int(_select(client, state, [MANAGEMENT_ROW])["X-Draft-Revision"])
        _choose(client, state, CREATE_ROW, str(blue.pk), revision=revision)
        state = ip_tab(client, tab)

        for row_key, attrs in state.rows.items():
            response = _row_read(client, attrs["hx-get"])

            assert response.status_code == 200
            assert response["Cache-Control"] == "no-store"
            assert _normalized_row(response.content.decode()) == _normalized_row(_row_html(state.html, row_key))

    def test_a_row_read_names_the_row_to_read_it_again(self, client, tab):
        attrs = ip_tab(client, tab).rows[CREATE_ROW]

        (row,) = open_tags(_row_read(client, attrs["hx-get"]).content.decode(), "tr")

        assert (row["hx-get"], row["hx-trigger"], row["hx-swap"]) == (attrs["hx-get"], "librenms:row-read", "outerHTML")

    def test_an_unknown_row_or_a_hidden_subject_answers_404(self, client, tab):
        url = ip_tab(client, tab).rows[CREATE_ROW]["hx-get"]
        assert _row_read(client, url.replace("198.18.60.10", "198.18.60.99")).status_code == 404

        outsider = Client()
        outsider.force_login(make_user_with_perms("ip-draft-outsider", []))
        assert _row_read(outsider, url).status_code in (403, 404)


class TestTheSelectionCommand:
    """A selection is an absolute state with the row's evidence, applied only to the draft revision the page shows."""

    def test_a_selected_row_renders_checked_on_the_next_render(self, client, tab):
        state = ip_tab(client, tab)

        response = _select(client, state, [CREATE_ROW])

        assert response.status_code == 204
        assert int(response["X-Draft-Revision"]) == int(state.table["data-draft-revision"]) + 1
        after = ip_tab(client, tab)
        assert _checked(after, CREATE_ROW)
        assert not _checked(after, NO_INTERFACE_ROW)
        assert after.table["data-draft-revision"] == response["X-Draft-Revision"]

    def test_a_command_of_an_older_revision_conflicts_and_changes_nothing(self, client, tab):
        state = ip_tab(client, tab)
        assert _select(client, state, [CREATE_ROW]).status_code == 204

        response = _select(client, state, [NO_INTERFACE_ROW])

        assert response.status_code == 409
        after = ip_tab(client, tab)
        assert _checked(after, CREATE_ROW) and not _checked(after, NO_INTERFACE_ROW)

    def test_a_token_of_another_row_or_no_token_is_refused(self, client, tab):
        state = ip_tab(client, tab)
        other_token = state.rows[NO_INTERFACE_ROW]["data-evidence"]
        url, draft = state.table["data-selection-url"], state.table["data-draft-id"]
        revision = state.table["data-draft-revision"]

        for value in ({"state": "on", "token": other_token}, {"state": "on"}, {"state": "maybe"}, "on"):
            response = _command(
                client, url, draft_id=draft, expected_revision=revision, assignments=json.dumps({CREATE_ROW: value})
            )
            assert response.status_code == 400, value
        assert ip_tab(client, tab).table["data-draft-revision"] == revision

    def test_the_unload_beacon_posts_a_form_with_the_csrf_token(self, tab):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(make_superuser("ip-draft-user"))
        page = browser.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[tab.pk]))
        state = ip_tab(browser, tab)
        assignments = json.dumps({CREATE_ROW: {"state": "on", "token": state.rows[CREATE_ROW]["data-evidence"]}})
        fields = {
            "draft_id": state.table["data-draft-id"],
            "expected_revision": state.table["data-draft-revision"],
            "assignments": assignments,
        }

        assert browser.post(unescape(state.table["data-selection-url"]), fields).status_code == 403
        fields["csrfmiddlewaretoken"] = page.cookies["csrftoken"].value
        assert browser.post(unescape(state.table["data-selection-url"]), fields).status_code == 204
        assert _checked(ip_tab(browser, tab), CREATE_ROW)


class TestTheVrfChoice:
    """A VRF choice is a command; the row read then shows the row in that VRF."""

    def test_a_choice_names_the_row_and_its_read_shows_the_vrf(self, client, tab):
        blue = VRF.objects.create(name="choice-blue")
        state = ip_tab(client, tab)

        response = _choose(client, state, CREATE_ROW, str(blue.pk))

        assert response.status_code == 204
        assert json.loads(response["HX-Trigger"]) == {"lnms-row-stale": {"key": CREATE_ROW}}
        row = _row_read(client, state.rows[CREATE_ROW]["hx-get"]).content.decode()
        assert re.search(rf'<option value="{blue.pk}"\s+selected>', row)

    def test_a_choice_on_a_selected_row_asks_to_select_it_again(self, client, tab):
        blue = VRF.objects.create(name="choice-reselect")
        state = ip_tab(client, tab)
        revision = int(_select(client, state, [CREATE_ROW])["X-Draft-Revision"])

        _choose(client, state, CREATE_ROW, str(blue.pk), revision=revision)

        after = ip_tab(client, tab)
        assert after.rows[CREATE_ROW]["data-evidence-changed"] == "true"
        assert not _checked(after, CREATE_ROW)
        assert _select(client, after, [CREATE_ROW]).status_code == 204
        again = ip_tab(client, tab)
        assert _checked(again, CREATE_ROW) and "data-evidence-changed" not in again.rows[CREATE_ROW]

    def test_choosing_the_vrf_the_row_shows_stores_no_choice(self, client, tab):
        blue = VRF.objects.create(name="choice-back")
        state = ip_tab(client, tab)
        revision = int(_choose(client, state, CREATE_ROW, str(blue.pk))["X-Draft-Revision"])
        _choose(client, state, CREATE_ROW, "", revision=revision)
        # Once VRF blue holds the address, the row shows blue without a choice.
        IPAddress.objects.create(address=CREATE_ROW, vrf=blue)

        row = _row_read(client, state.rows[CREATE_ROW]["hx-get"]).content.decode()

        assert re.search(rf'<option value="{blue.pk}"\s+selected>', row)

    @pytest.mark.parametrize("value", ["abc", "1.5", "true"])
    def test_a_malformed_vrf_answers_400(self, client, tab, value):
        assert _choose(client, ip_tab(client, tab), CREATE_ROW, value).status_code == 400

    def test_a_hidden_and_a_missing_vrf_give_the_same_404(self, client, tab):
        hidden = VRF.objects.create(name="choice-hidden")
        viewer = Client()
        user = make_user_with_perms("ip-draft-vrf-viewer", [("view", type(tab)), ("view", IPAddress)])
        viewer.force_login(grant(user, "view", VRF, constraints={"name": "nothing"}))
        state = ip_tab(viewer, tab)

        assert _choose(viewer, state, CREATE_ROW, str(hidden.pk)).status_code == 404
        assert _choose(viewer, state, CREATE_ROW, str(hidden.pk + 1000)).status_code == 404
        assert _choose(viewer, state, "198.18.60.99/24", "").status_code == 404


class TestTheBulkSubmit:
    """The bulk submit claims the draft, writes the approved rows and puts back every row it did not write."""

    def test_the_submit_writes_the_claimed_rows_and_puts_back_the_refused(self, client, tab):
        blue = VRF.objects.create(name="submit-blue")

        response = submit_ip_selection(
            client, tab, [CREATE_ROW, NO_INTERFACE_ROW], vrfs={CREATE_ROW: str(blue.pk)}, headers=HTMX
        )

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert "lnms-results-stale" in json.loads(response["HX-Trigger"])
        assert IPAddress.objects.get(address=CREATE_ROW).vrf == blue
        assert not IPAddress.objects.filter(address=NO_INTERFACE_ROW).exists()
        after = ip_tab(client, tab)
        assert not _checked(after, CREATE_ROW)
        assert _checked(after, NO_INTERFACE_ROW)
        # The page's draft writer goes on from the revision that the claim and the restore left.
        assert response["X-Draft-Revision"] == after.table["data-draft-revision"]

    def test_a_row_that_changed_after_its_selection_is_refused_and_put_back(self, client, tab):
        draft = select_ip_rows(client, tab, [CREATE_ROW])
        # The address appears in NetBox, so the row the user approved is no longer the row to sync.
        IPAddress.objects.create(address=CREATE_ROW)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", tab.pk]),
            {"server_key": SERVER_KEY, **draft},
            **HTMX,
        )

        assert IPAddress.objects.get(address=CREATE_ROW).assigned_object is None
        assert any("198.18.60.10" in text and "changed" in text for text in _messages(response))
        assert ip_tab(client, tab).rows[CREATE_ROW]["data-evidence-changed"] == "true"

    def test_a_selection_without_evidence_is_refused(self, client, tab):
        from netbox_librenms_plugin.sync_rows import open_ip_draft

        state = ip_tab(client, tab)
        request = client.get(unescape(state.rows[CREATE_ROW]["hx-get"])).wsgi_request
        from netbox_librenms_plugin import source_cache
        from netbox_librenms_plugin.acquisition.registry import Resource

        snapshot = source_cache.read(tab, Resource.IP_ADDRESSES, SERVER_KEY)
        draft = open_ip_draft(request, tab, SERVER_KEY, snapshot)
        revision = draft.assign(draft.draft_id, draft.revision, {CREATE_ROW: Assignment(Intent.ON)})

        client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", tab.pk]),
            {"server_key": SERVER_KEY, "draft_id": draft.draft_id, "expected_revision": revision},
            **HTMX,
        )

        assert not IPAddress.objects.filter(address=CREATE_ROW).exists()

    def test_a_submit_of_an_older_revision_writes_nothing_and_keeps_the_draft(self, client, tab):
        draft = select_ip_rows(client, tab, [CREATE_ROW])
        # Another window of the user clears a row after this page acknowledged its revision.
        select_ip_rows(client, tab, [NO_INTERFACE_ROW])

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", tab.pk]),
            {"server_key": SERVER_KEY, **draft},
            **HTMX,
        )

        assert response.status_code == 200
        assert not IPAddress.objects.filter(address=CREATE_ROW).exists()
        after = ip_tab(client, tab)
        assert _checked(after, CREATE_ROW) and _checked(after, NO_INTERFACE_ROW)

    def test_a_second_submit_of_one_revision_writes_nothing(self, client, tab):
        draft = select_ip_rows(client, tab, [CREATE_ROW])
        url = reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", tab.pk])
        client.post(url, {"server_key": SERVER_KEY, **draft}, **HTMX)
        IPAddress.objects.filter(address=CREATE_ROW).delete()

        client.post(url, {"server_key": SERVER_KEY, **draft}, **HTMX)

        assert not IPAddress.objects.filter(address=CREATE_ROW).exists()

    def test_the_submit_ignores_posted_checkboxes(self, client, tab):
        draft = select_ip_rows(client, tab, [])

        client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", tab.pk]),
            {"server_key": SERVER_KEY, "select": CREATE_ROW, **draft},
            **HTMX,
        )

        assert not IPAddress.objects.filter(address=CREATE_ROW).exists()


def _row_action(client, owner, state, row_key):
    """Post the row's own sync button as the page that rendered *state* does: its draft, revision and the row's evidence."""
    return client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", owner.pk]),
        {
            "server_key": SERVER_KEY,
            "sync_one": row_key,
            "draft_id": state.table["data-draft-id"],
            "expected_revision": state.table["data-draft-revision"],
            "evidence": state.rows[row_key]["data-evidence"],
        },
        **HTMX,
    )


class TestTheRowAction:
    """A row's own button syncs the row that the page shows, in its VRF, and leaves the draft as it is."""

    def test_sync_one_takes_the_vrf_that_the_page_shows_and_keeps_the_selection(self, client, tab):
        blue = VRF.objects.create(name="sync-one-blue")
        select_ip_rows(client, tab, [NO_INTERFACE_ROW], vrfs={CREATE_ROW: str(blue.pk)})

        response = _row_action(client, tab, ip_tab(client, tab), CREATE_ROW)

        assert answers_in_place(response)
        assert IPAddress.objects.get(address=CREATE_ROW).vrf == blue
        assert _checked(ip_tab(client, tab), NO_INTERFACE_ROW)

    def test_a_row_action_of_a_page_that_shows_another_vrf_writes_nothing(self, client, tab):
        blue = VRF.objects.create(name="sync-one-other-window")
        shown = ip_tab(client, tab)
        # Another window of the user chooses blue after this page showed the row in Global.
        select_ip_rows(client, tab, [], vrfs={CREATE_ROW: str(blue.pk)})

        response = _row_action(client, tab, shown, CREATE_ROW)

        assert answers_in_place(response)
        assert not IPAddress.objects.filter(address=CREATE_ROW).exists()
        after = ip_tab(client, tab)
        assert re.search(rf'<option value="{blue.pk}"\s+selected>', _row_html(after.html, CREATE_ROW))
        assert after.table["data-draft-revision"] == str(int(shown.table["data-draft-revision"]) + 1)

    def test_a_row_action_whose_evidence_is_not_the_current_one_writes_nothing(self, client, tab):
        shown = ip_tab(client, tab)
        # Another interface of the same name takes the place of the one the page showed.
        tab.interfaces.get(name="eth10").delete()
        make_interface(tab, "eth10", iface_type="1000base-t")

        _row_action(client, tab, shown, CREATE_ROW)

        assert not IPAddress.objects.filter(address=CREATE_ROW).exists()


class TestSetPrimaryIp:
    """Set Primary IP selects the management row on the server: once in a new draft, and on each switch change."""

    def _enable(self, client):
        client.post(
            reverse("plugins:netbox_librenms_plugin:save_user_pref"),
            json.dumps({"key": "set_primary_ip", "value": True}),
            content_type="application/json",
        )

    def test_a_new_draft_selects_the_management_row_once(self, client, tab):
        self._enable(client)

        state = ip_tab(client, tab)
        assert _checked(state, MANAGEMENT_ROW)
        cleared = {MANAGEMENT_ROW: {"state": "none", "token": None}}
        _command(
            client,
            state.table["data-selection-url"],
            draft_id=state.table["data-draft-id"],
            expected_revision=state.table["data-draft-revision"],
            assignments=json.dumps(cleared),
        )

        assert not _checked(ip_tab(client, tab), MANAGEMENT_ROW)

    def test_the_switch_selects_and_clears_the_management_row(self, client, tab):
        state = ip_tab(client, tab)
        assert not _checked(state, MANAGEMENT_ROW)

        on = switch_set_primary_ip(client, state.table, "on")
        assert on.status_code == 204
        assert json.loads(on["HX-Trigger"]) == {"lnms-results-stale": None}
        after = ip_tab(client, tab)
        assert _checked(after, MANAGEMENT_ROW)

        off = switch_set_primary_ip(client, after.table, "off")
        assert off.status_code == 204
        assert not _checked(ip_tab(client, tab), MANAGEMENT_ROW)

    def test_the_switch_approves_the_management_address_that_the_page_showed(self, client, tab):
        interface = tab.interfaces.get(name="eth12")
        shown = IPAddress.objects.create(address=MANAGEMENT_ROW, assigned_object=interface)
        state = ip_tab(client, tab)
        # Another operation replaces the address that the page shows, on the same interface.
        shown.delete()
        replacement = IPAddress.objects.create(address=MANAGEMENT_ROW, assigned_object=interface)

        switched = switch_set_primary_ip(client, state.table, "on")
        assert switched.status_code == 204
        # The page submits before its read of the tab after the switch.
        draft = {"draft_id": state.table["data-draft-id"], "expected_revision": switched["X-Draft-Revision"]}
        response = _submit(client, tab, draft, **{"set-primary-ip-toggle": "on"})

        tab.refresh_from_db()
        assert tab.primary_ip4_id is None
        assert any("198.18.60.12" in text and "changed" in text for text in _messages(response))
        assert IPAddress.objects.get(pk=replacement.pk).assigned_object == interface

    def test_the_switch_leaves_a_management_row_without_shown_evidence_unapproved(self, client, tab):
        state = ip_tab(client, tab)

        switched = switch_set_primary_ip(client, {**state.table, "data-management-evidence": "{}"}, "on")

        assert switched.status_code == 204
        after = ip_tab(client, tab)
        assert not _checked(after, MANAGEMENT_ROW)
        assert after.rows[MANAGEMENT_ROW]["data-evidence-changed"] == "true"
        draft = {"draft_id": after.table["data-draft-id"], "expected_revision": after.table["data-draft-revision"]}
        _submit(client, tab, draft, **{"set-primary-ip-toggle": "on"})
        tab.refresh_from_db()
        assert tab.primary_ip4_id is None
        assert not IPAddress.objects.filter(address=MANAGEMENT_ROW).exists()

    def test_the_switch_refuses_evidence_of_another_row(self, client, tab):
        state = ip_tab(client, tab)
        foreign = json.dumps({MANAGEMENT_ROW: state.rows[CREATE_ROW]["data-evidence"]})

        switched = switch_set_primary_ip(client, {**state.table, "data-management-evidence": foreign}, "on")

        assert switched.status_code == 400


def test_a_data_refresh_clears_the_selection_and_says_so(client, live_librenms, tab):
    select_ip_rows(client, tab, [CREATE_ROW])

    refreshed = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[tab.pk]),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
        **HTMX,
    )

    assert "data-selection-cleared" in refreshed.content.decode()
    after = ip_tab(client, tab)
    assert not _checked(after, CREATE_ROW)
    assert "data-selection-cleared" not in after.html


def test_a_filter_keeps_the_draft_and_another_name_field_has_its_own(client, tab):
    draft = ip_tab(client, tab, interface_name_field="ifName").table["data-draft-id"]

    assert (
        ip_tab(client, tab, interface_name_field="ifName", ipaddresses_q="198.18.60.1").table["data-draft-id"] == draft
    )
    assert ip_tab(client, tab, interface_name_field="ifDescr").table["data-draft-id"] != draft


def test_a_return_to_an_earlier_name_field_does_not_bring_back_its_selection(client, tab):
    select_ip_rows(client, tab, [CREATE_ROW], interface_name_field="ifName")
    first = ip_tab(client, tab, interface_name_field="ifName")
    stale = {"draft_id": first.table["data-draft-id"], "expected_revision": first.table["data-draft-revision"]}
    ip_tab(client, tab, interface_name_field="ifDescr")

    # Back on ifName, with a filter that does not show the row selected before.
    back = ip_tab(client, tab, interface_name_field="ifName", ipaddresses_q=MANAGEMENT_ROW.partition("/")[0])
    assert CREATE_ROW not in back.rows
    assert back.table["data-draft-id"] != stale["draft_id"]
    draft = {"draft_id": back.table["data-draft-id"], "expected_revision": back.table["data-draft-revision"]}
    # A submit of the page from before the change conflicts, and the page's own submit has no rows.
    _submit(client, tab, stale, interface_name_field="ifName")
    _submit(client, tab, draft, interface_name_field="ifName")

    assert not IPAddress.objects.filter(address=CREATE_ROW).exists()
    assert not _checked(ip_tab(client, tab, interface_name_field="ifName"), CREATE_ROW)


def test_the_selected_rows_that_the_page_does_not_show_are_counted_above_the_table(client, tab):
    select_ip_rows(client, tab, [CREATE_ROW, NO_INTERFACE_ROW])

    # The filter shows only the management row, so both selected rows are on no shown page.
    filtered = ip_tab(client, tab, ipaddresses_q=MANAGEMENT_ROW.partition("/")[0])

    assert [attrs for attrs in open_tags(filtered.html, "div") if "data-offpage-selection" in attrs]
    (clear,) = [attrs for attrs in open_tags(filtered.html, "button") if "data-clear-offpage" in attrs]
    assert sorted(json.loads(clear["data-clear-offpage"])) == sorted([CREATE_ROW, NO_INTERFACE_ROW])
    # A page that shows every selected row has no such notice.
    assert "data-offpage-selection" not in ip_tab(client, tab).html


def test_an_invalid_filter_counts_every_selected_row_as_hidden_and_keeps_the_draft_on_the_page(client, tab):
    select_ip_rows(client, tab, [CREATE_ROW, NO_INTERFACE_ROW])
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": tab.pk, "tab": "ipaddresses"},
    )

    # The search is longer than a filter takes, so the tab shows no row.
    html = client.get(url, {"server_key": SERVER_KEY, "ipaddresses_q": "x" * 300}).content.decode()

    assert "data-filter-invalid" in html
    assert not [attrs for attrs in open_tags(html, "tr") if attrs.get("data-row-key")]
    (clear,) = [attrs for attrs in open_tags(html, "button") if "data-clear-offpage" in attrs]
    assert sorted(json.loads(clear["data-clear-offpage"])) == sorted([CREATE_ROW, NO_INTERFACE_ROW])
    # The page keeps the draft that Clear and the submit name.
    (table,) = [attrs for attrs in open_tags(html, "table") if attrs.get("data-draft-id")]
    assert table["data-draft-id"] == ip_tab(client, tab).table["data-draft-id"]
    assert table["data-selection-url"]


def test_a_row_read_is_the_row_of_a_page_without_a_server_key(client, seeded):
    """The page query of the sync page names no server; the row read names it, and both render one Create VRF action."""
    owner = seeded("row-read-without-server-key")
    client.force_login(make_superuser("ip-draft-user"))
    page = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]), {"tab": "ipaddresses"}
    )
    html = page.content.decode()
    row_html = _row_html(html, RD_ROWS[0])
    assert 'name="create_vrf"' in row_html
    (row,) = open_tags(row_html, "tr")

    response = _row_read(client, row["hx-get"])

    assert response.status_code == 200
    assert _normalized_row(response.content.decode()) == _normalized_row(row_html)


def _stored_draft_record(user, owner):
    """Return the stored IP draft record of *user* on *owner*, or None, read straight from the cache."""
    from django.core.cache import cache

    from netbox_librenms_plugin.selection_draft import draft_key

    return cache.get(draft_key(user, owner, "ipaddresses", "ipaddresses", SERVER_KEY))


def test_a_command_starts_no_draft(client, tab):
    from django.contrib.auth import get_user_model

    state = ip_tab(client, tab)
    other_user = get_user_model().objects.create_user(username="ip-draft-no-draft", is_superuser=True)
    other = Client()
    other.force_login(other_user)
    assignments = json.dumps({CREATE_ROW: {"state": "none", "token": None}})

    response = _command(
        other, state.table["data-selection-url"], draft_id="no-draft", expected_revision=0, assignments=assignments
    )

    assert response.status_code == 409
    assert _stored_draft_record(other_user, tab) is None


def test_a_presentation_of_an_older_snapshot_keeps_the_newer_draft(client, tab):
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.sync_rows import open_ip_draft
    from netbox_librenms_plugin.tests.view_test_helpers import make_request

    older = source_cache.read(tab, Resource.IP_ADDRESSES, SERVER_KEY)
    refreshed = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[tab.pk]),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
        **HTMX,
    )
    assert refreshed.status_code == 200
    assert source_cache.read(tab, Resource.IP_ADDRESSES, SERVER_KEY).snapshot_id != older.snapshot_id
    select_ip_rows(client, tab, [CREATE_ROW])
    user = refreshed.wsgi_request.user

    # A page READ that read the snapshot before the refresh opens its draft after it.
    open_ip_draft(make_request("get", {"server_key": SERVER_KEY}, user=user), tab, SERVER_KEY, older)

    assert _checked(ip_tab(client, tab), CREATE_ROW)


def test_a_new_address_in_the_chosen_vrf_asks_to_select_the_row_again(client, tab):
    blue = VRF.objects.create(name="evidence-blue")
    interface = tab.interfaces.get(name="eth10")
    IPAddress.objects.create(address=CREATE_ROW)
    chosen = IPAddress.objects.create(address=CREATE_ROW, vrf=blue, assigned_object=interface)
    draft = select_ip_rows(client, tab, [CREATE_ROW], vrfs={CREATE_ROW: str(blue.pk)})
    # Another address takes the place of the one the row showed in blue, on the same interface.
    chosen.delete()
    replacement = IPAddress.objects.create(address=CREATE_ROW, vrf=blue, assigned_object=interface)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", tab.pk]),
        {"server_key": SERVER_KEY, **draft},
        **HTMX,
    )

    assert any("198.18.60.10" in text and "changed" in text for text in _messages(response))
    assert ip_tab(client, tab).rows[CREATE_ROW]["data-evidence-changed"] == "true"
    assert IPAddress.objects.get(pk=replacement.pk).assigned_object == interface


@pytest.fixture
def before_the_writer(monkeypatch):
    """Run a callback at the start of each writer attempt: after the evidence check, before the writer reads NetBox."""
    from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

    real_attempt = SyncIPAddressesView._process_ip_sync
    callbacks = []

    def attempt(self, *args, **kwargs):
        while callbacks:
            callbacks.pop(0)()
        return real_attempt(self, *args, **kwargs)

    monkeypatch.setattr(SyncIPAddressesView, "_process_ip_sync", attempt)
    return callbacks


def _submit(client, owner, draft, **fields):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", owner.pk]),
        {"server_key": SERVER_KEY, **draft, **fields},
        **HTMX,
    )


class TestTheWriterChecksTheApprovedRow:
    """The writer compares the evidence the user approved with what it is about to write, after its locks."""

    def test_a_port_bound_to_another_interface_after_the_check_writes_nothing(self, client, tab, before_the_writer):
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

        draft = select_ip_rows(client, tab, [CREATE_ROW])
        other = make_interface(tab, "eth10b", iface_type="1000base-t")
        before_the_writer.append(lambda: seed_mapping(other, SERVER_KEY, own=7610))

        response = _submit(client, tab, draft)

        assert not IPAddress.objects.filter(address=CREATE_ROW).exists()
        assert any("198.18.60.10" in text and "changed" in text for text in _messages(response))
        assert _checked(ip_tab(client, tab), CREATE_ROW) is False

    def test_a_management_address_replaced_after_the_check_does_not_become_primary(
        self, client, tab, before_the_writer
    ):
        interface = tab.interfaces.get(name="eth12")
        approved = IPAddress.objects.create(address=MANAGEMENT_ROW, assigned_object=interface)
        draft = select_ip_rows(client, tab, [MANAGEMENT_ROW])
        replacement = {}

        def replace():
            approved.delete()
            replacement["ip"] = IPAddress.objects.create(address=MANAGEMENT_ROW, assigned_object=interface)

        before_the_writer.append(replace)

        response = _submit(client, tab, draft, **{"set-primary-ip-toggle": "on"})

        tab.refresh_from_db()
        assert tab.primary_ip4_id is None
        assert any("198.18.60.12" in text and "changed" in text for text in _messages(response))
        assert IPAddress.objects.get(pk=replacement["ip"].pk).assigned_object == interface


@transactional_db_with_all_apps()
def test_an_interface_that_another_sync_binds_before_the_create_is_not_taken_for_an_approved_missing_one(
    client, settings, monkeypatch
):
    from dcim.models import Interface
    from psycopg.types.json import Jsonb

    from netbox_librenms_plugin.server_mappings import read_mapping
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
    from netbox_librenms_plugin.tests.interface_sync_post_helpers import sync_port
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot
    from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

    configure_default_librenms_server(settings)
    device = seed_stored_mapping(make_device("ip-draft-reuse"), {SERVER_KEY: {"id": 42}}, save=True)
    other = make_interface(device, "eth3/9", iface_type="1000base-t")
    seed_ip_snapshot(device, [ip_address_row("198.18.23.10", 7023)], [sync_port(7023, "eth3/1", alias="uplink")])
    client.force_login(make_superuser("ip-draft-reuse-user"))
    real_create = SyncIPAddressesView._create_interface_for_ip

    with second_connection() as connection:

        def bind_then_create(self, *args, **kwargs):
            # Another interface sync binds the port after the writer found no interface, before the owner lock.
            bound = Interface.objects.get(pk=other.pk)
            seed_mapping(bound, SERVER_KEY, own=7023, save=False)
            commit_row_change(connection, Interface, other.pk, {"custom_field_data": Jsonb(bound.custom_field_data)})
            return real_create(self, *args, **kwargs)

        monkeypatch.setattr(SyncIPAddressesView, "_create_interface_for_ip", bind_then_create)
        response = submit_ip_selection(
            client, device, ["198.18.23.10/24"], **{"create-missing-interfaces-toggle": "on"}
        )

    assert not IPAddress.objects.filter(address="198.18.23.10/24").exists()
    assert any("198.18.23.10" in text and "changed" in text for text in _messages(response))
    other.refresh_from_db()
    # The helper's update of the reused interface rolled back with the row.
    assert (other.name, other.description) == ("eth3/9", "")
    assert read_mapping(other).own_id(SERVER_KEY) == 7023
    assert not Interface.objects.filter(device=device, name="eth3/1").exists()


@transactional_db_with_all_apps()
def test_a_failing_commit_callback_keeps_the_written_row_and_puts_back_the_other_claimed_rows(
    client, settings, before_the_writer
):
    from django.db import transaction

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
    from netbox_librenms_plugin.tests.interface_sync_post_helpers import bound_interface, sync_port
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot
    from netbox_librenms_plugin.tests.view_test_helpers import read_ip_row
    from netbox_librenms_plugin.transactions import CommittedFollowUpError

    configure_default_librenms_server(settings)
    device = seed_stored_mapping(make_device("ip-draft-follow-up"), {SERVER_KEY: {"id": 42}}, save=True)
    bound_interface(device, "eth5/1", 7051)
    bound_interface(device, "eth5/3", 7053)
    seed_ip_snapshot(
        device,
        [ip_address_row(f"198.18.25.{host}", 7050 + host) for host in (1, 2, 3)],
        [sync_port(7050 + host, f"eth5/{host}") for host in (1, 2, 3)],
    )
    blue = VRF.objects.create(name="follow-up-blue")
    client.force_login(make_superuser("ip-draft-follow-up-user"))
    # 25.1 writes, 25.2 has no interface, and 25.3 holds only a VRF choice.
    draft = select_ip_rows(client, device, ["198.18.25.1/24", "198.18.25.2/24"], vrfs={"198.18.25.3/24": str(blue.pk)})

    def fail():
        raise RuntimeError("A commit callback failed.")

    before_the_writer.append(lambda: transaction.on_commit(fail))

    with pytest.raises(CommittedFollowUpError):
        _submit(client, device, draft)

    assert IPAddress.objects.filter(address="198.18.25.1/24").exists()
    after = ip_tab(client, device)
    assert not _checked(after, "198.18.25.1/24")
    assert _checked(after, "198.18.25.2/24")
    assert read_ip_row(client, device, "198.18.25.3/24").shown_vrf == str(blue.pk)


def test_a_submit_answer_names_no_revision_when_another_window_changed_the_draft_meanwhile(
    client, tab, before_the_writer
):
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.sync_rows import find_ip_draft
    from netbox_librenms_plugin.tests.view_test_helpers import make_request

    draft = select_ip_rows(client, tab, [CREATE_ROW, NO_INTERFACE_ROW])
    token = ip_tab(client, tab).rows[MANAGEMENT_ROW]["data-evidence"]
    user = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[tab.pk])).wsgi_request.user

    def other_window_selects():
        request = make_request("get", {"server_key": SERVER_KEY}, user=user)
        snapshot = source_cache.read(tab, Resource.IP_ADDRESSES, SERVER_KEY)
        stored = find_ip_draft(request, tab, SERVER_KEY, snapshot)
        stored.assign(
            stored.draft_id, stored.revision, {MANAGEMENT_ROW: Assignment(Intent.ON, read_evidence(IP_TAB, token))}
        )

    before_the_writer.append(other_window_selects)

    response = _submit(client, tab, draft)

    # The restore puts back the refused row, but the page did not see the other window's change.
    assert "X-Draft-Revision" not in response
    after = ip_tab(client, tab)
    assert _checked(after, NO_INTERFACE_ROW) and _checked(after, MANAGEMENT_ROW)


def test_a_failure_after_the_claim_and_before_the_writer_puts_the_selection_back(client, tab):
    from django.db import DatabaseError, connection

    draft = select_ip_rows(client, tab, [CREATE_ROW, NO_INTERFACE_ROW])
    state = {"claimed": False}

    def fail_after_the_claim(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql:
            state["claimed"] = True
        elif state["claimed"] and '"ipam_vrf"' in sql:
            raise DatabaseError("The reassessment after the claim failed.")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_after_the_claim), pytest.raises(DatabaseError):
        _submit(client, tab, draft)

    after = ip_tab(client, tab)
    assert _checked(after, CREATE_ROW) and _checked(after, NO_INTERFACE_ROW)
