"""
The Interfaces tab on the selection draft, end to end: the commands, the row read, and the submit that claims the draft.

Each test seeds an interfaces snapshot and drives the real endpoints with a signed-in client: the tab
render, the selection and choice commands, the row read, the VLAN groups dialog and the sync POST.
NetBox rows and object permissions are real.

The last section runs the server's required-row rule (``required_rows``) over ``selection_rule_cases.json``;
``test_selection_mirror_browser.py`` runs the page's mirror (sync_draft.js) over the same file, so the two
cannot drift.
"""

import json
import re
from pathlib import Path
from html import unescape

import pytest
from dcim.models import Interface
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin.required_rows import (
    checked_rows,
    off_page_requirers,
    requirement_edges,
    requirements,
    taken_rows,
)
from netbox_librenms_plugin.selection_draft import Intent, read_evidence
from netbox_librenms_plugin.tests._html_helpers import open_tags
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
    map_device_to_librenms,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import bound_interface, sync_port
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import port_pair, port_vlans, seed_interfaces_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import (
    draft_command,
    draft_tab,
    grant,
    make_user_with_perms,
    row_action_fields,
    select_rows,
    submit_with_draft,
)
from netbox_librenms_plugin.views.base.interfaces_view import INTERFACE_TAB

pytestmark = pytest.mark.django_db

SERVER_KEY = "default"
HTMX = {"HTTP_HX_REQUEST": "true"}
QUERY = {"interface_name_field": "ifName"}


def _tab(client, owner, **query):
    return draft_tab(client, owner, "interfaces", **{**QUERY, **query})


def _select(client, owner, rows, **kwargs):
    return select_rows(client, owner, "interfaces", rows, **{**QUERY, **kwargs})


def _sync_url(owner):
    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": owner._meta.model_name, "object_id": owner.pk},
    )


def _submit(client, owner, rows, **kwargs):
    kwargs.setdefault("headers", HTMX)
    kwargs.setdefault("exclude_columns", ["mac_address"])
    return submit_with_draft(client, owner, "interfaces", _sync_url(owner), rows, query=QUERY, **kwargs)


def _post(client, owner, fields):
    return client.post(f"{_sync_url(owner)}?interface_name_field=ifName", {"server_key": SERVER_KEY, **fields}, **HTMX)


def _row_html(html, row_key):
    for match in re.finditer(r"<tr\b[^>]*>.*?</tr>", html, re.S):
        if f'data-row-key="{row_key}"' in unescape(match.group(0)):
            return match.group(0)
    raise AssertionError(f"no row {row_key}")


def _checked(shown, row_key):
    boxes = [attrs for attrs in open_tags(_row_html(shown.html, row_key), "input") if attrs.get("name") == "select"]
    return bool(boxes) and "checked" in boxes[0]


def _normalized_row(html):
    """Return the row markup with its whitespace collapsed and its evidence token replaced by its payload."""

    def payload(match):
        return f'data-evidence="{json.dumps(read_evidence(INTERFACE_TAB, unescape(match.group(1))), sort_keys=True)}"'

    return re.sub(r"\s+", " ", re.sub(r'data-evidence="([^"]+)"', payload, html)).strip()


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


@pytest.fixture
def device(client, settings):
    """
    Return a device with a mismatched row 10, a row 11 missing in NetBox, and a LAG po1 (20) with members 21 and 22.

    The client is signed in as a superuser.
    """
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("iface-draft-user"))
    owner = make_device("iface-draft", librenms_cf={SERVER_KEY: {"id": 4200}})
    eth0 = bound_interface(owner, "eth0", 10, iface_type="1000base-t")
    Interface.objects.filter(pk=eth0.pk).update(mtu=9000)
    seed_interfaces_snapshot(
        owner,
        [
            sync_port(10, "eth0"),
            sync_port(11, "eth1"),
            sync_port(20, "po1", if_type="ieee8023adLag"),
            sync_port(21, "eth21"),
            sync_port(22, "eth22"),
        ],
        [port_pair(21, 20), port_pair(22, 20)],
        server_key=SERVER_KEY,
    )
    return owner


class TestTheTab:
    """The tab renders every row from the user's draft, with the evidence and the requirements the page reads."""

    def test_the_table_names_its_draft_and_each_row_its_evidence_and_requirements(self, client, device):
        shown = _tab(client, device)

        assert shown.table["data-draft-revision"] == "0"
        for attribute in ("data-selection-url", "data-choice-url", "data-member-url", "data-vlan-groups-url"):
            assert shown.table[attribute].startswith("/plugins/")
        assert read_evidence(INTERFACE_TAB, shown.rows["11"]["data-evidence"])["row"] == "11"
        assert json.loads(shown.rows["21"]["data-requires"]) == ["20", "22"]
        assert json.loads(shown.rows["20"]["data-requires"]) == ["21", "22"]
        assert {key: attrs["data-intent"] for key, attrs in shown.rows.items()} == dict.fromkeys(shown.rows, "none")

    def test_a_selected_row_takes_its_required_rows_on_the_next_render(self, client, device):
        _select(client, device, [21])

        shown = _tab(client, device)

        assert [key for key in ("10", "11", "20", "21", "22") if _checked(shown, key)] == ["20", "21", "22"]
        assert (shown.rows["21"]["data-intent"], shown.rows["20"]["data-intent"]) == ("on", "none")

    def test_an_excluded_required_row_renders_unchecked(self, client, device):
        _select(client, device, [21], states={"22": "off"})

        shown = _tab(client, device)

        assert [key for key in ("20", "21", "22") if _checked(shown, key)] == ["20", "21"]
        assert shown.rows["22"]["data-intent"] == "off"

    def test_a_selected_row_on_another_page_counts_on_the_page_of_its_required_row(self, client, device):
        _select(client, device, [21])

        shown = _tab(client, device, interfaces_filter_name="po1")

        assert list(shown.rows) == ["20"]
        assert shown.rows["20"]["data-offpage-requirers"] == "1"
        assert _checked(shown, "20")

    def test_without_the_option_a_selection_takes_nothing_along(self, client, device):
        from netbox_librenms_plugin.utils import INTERFACE_SYNC_OPTIONS_PREFERENCE

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(username="iface-draft-user")
        user.config.set(
            INTERFACE_SYNC_OPTIONS_PREFERENCE, {"auto_select_lag_members": False, "exclude_columns": []}, commit=True
        )
        _select(client, device, [21])

        shown = _tab(client, device)

        assert [key for key in ("20", "21", "22") if _checked(shown, key)] == ["21"]


class TestTheRowRead:
    """The row read answers the row exactly as the tab renders it, for the same draft and page query."""

    def test_a_row_read_is_the_row_of_the_tab_render(self, client, device):
        _select(client, device, [21], states={"22": "off"})
        shown = _tab(client, device, interfaces_filter_name="eth2")

        for row_key, attrs in shown.rows.items():
            response = client.get(unescape(attrs["hx-get"]))

            assert response.status_code == 200
            assert response["Cache-Control"] == "no-store"
            assert _normalized_row(response.content.decode()) == _normalized_row(_row_html(shown.html, row_key))

    def test_an_unknown_row_or_a_hidden_subject_answers_404(self, client, device):
        url = unescape(_tab(client, device).rows["11"]["hx-get"])
        assert client.get(url.replace("/interface-row/11/", "/interface-row/99/")).status_code == 404

        outsider = Client()
        outsider.force_login(make_user_with_perms("iface-draft-outsider", []))
        assert outsider.get(url).status_code in (403, 404)


class TestTheCommands:
    """A command applies only to the draft revision the page shows, and a selection carries the row's own evidence."""

    def test_a_command_of_an_older_revision_conflicts_and_changes_nothing(self, client, device):
        shown = _tab(client, device)
        assignments = {"11": {"state": "on", "token": shown.rows["11"]["data-evidence"]}}
        assert (
            draft_command(client, shown, "data-selection-url", assignments=json.dumps(assignments)).status_code == 204
        )

        response = draft_command(client, shown, "data-selection-url", assignments=json.dumps({"10": {"state": "off"}}))

        assert response.status_code == 409
        after = _tab(client, device)
        assert _checked(after, "11") and after.rows["10"]["data-intent"] == "none"

    def test_a_token_of_another_row_is_refused(self, client, device):
        shown = _tab(client, device)
        forged = {"11": {"state": "on", "token": shown.rows["10"]["data-evidence"]}}

        response = draft_command(client, shown, "data-selection-url", assignments=json.dumps(forged))

        assert response.status_code == 400
        assert _tab(client, device).table["data-draft-revision"] == "0"


@pytest.fixture
def chassis(client, settings):
    """Return the first member of a two-member chassis whose snapshot has row 2001 on member two and row 2002 unresolved."""
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("iface-draft-vc-user"))
    _vc, (first, second) = make_virtual_chassis_members("iface-draft-vc", 2)
    seed_stored_mapping(first, {SERVER_KEY: {"id": 4300}}, save=True)
    bound_interface(second, "Ethernet2/1", 2001)
    seed_interfaces_snapshot(first, [sync_port(2001, "Ethernet2/1"), sync_port(2002, "mgmt")], server_key=SERVER_KEY)
    return first, second


def _member_shown(row_html):
    selects = re.findall(r"<select[^>]*vc-member-select.*?</select>", row_html, re.S)
    chosen = re.search(r'<option value="(\d+)"[^>]*selected', selects[0]) if selects else None
    return int(chosen.group(1)) if chosen else None


class TestTheMemberChoice:
    """A member choice is a draft command; the row read shows the row assessed for that member."""

    def test_a_member_choice_names_the_row_and_its_read_shows_the_member(self, client, chassis):
        first, second = chassis
        shown = _tab(client, first)
        assert _member_shown(_row_html(shown.html, "2002")) is None

        response = draft_command(client, shown, "data-choice-url", row="2002", field="member", value=str(second.pk))

        assert response.status_code == 204
        assert json.loads(response["HX-Trigger"]) == {"lnms-row-stale": {"key": "2002"}}
        row = client.get(unescape(shown.rows["2002"]["hx-get"])).content.decode()
        assert _member_shown(row) == second.pk
        assert read_evidence(INTERFACE_TAB, open_tags(row, "tr")[0]["data-evidence"])["member"] == second.pk

    def test_a_member_choice_on_a_selected_row_asks_to_select_it_again(self, client, chassis):
        first, second = chassis
        draft = _select(client, first, [2001])
        shown = _tab(client, first)

        draft_command(
            client,
            shown,
            "data-choice-url",
            draft["expected_revision"],
            row="2001",
            field="member",
            value=str(first.pk),
        )

        after = _tab(client, first)
        assert not _checked(after, "2001")
        assert after.rows["2001"]["data-evidence-changed"] == "true"

    def test_a_member_the_user_cannot_view_answers_404(self, client, chassis, settings):
        first, second = chassis
        viewer = make_user_with_perms("iface-draft-vc-viewer", [])
        grant(viewer, "view", Interface)
        grant(viewer, "view", type(first), constraints={"pk": first.pk})
        member_client = Client()
        member_client.force_login(viewer)
        shown = _tab(member_client, first)

        response = draft_command(
            member_client, shown, "data-choice-url", row="2002", field="member", value=str(second.pk)
        )

        assert response.status_code == 404
        # The row of the hidden member names no member and offers no selection.
        assert "data-evidence" not in shown.rows["2001"]

    def test_bulk_member_chooses_the_member_of_every_selected_row(self, client, chassis):
        first, second = chassis
        draft = _select(client, first, [2002])
        shown = _tab(client, first)

        response = draft_command(client, shown, "data-member-url", draft["expected_revision"], value=str(second.pk))

        assert response.status_code == 204
        assert json.loads(response["HX-Trigger"]) == {"lnms-results-stale": None}
        after = _tab(client, first)
        assert _member_shown(_row_html(after.html, "2002")) == second.pk
        # The row now writes to another member, so the user approves it again.
        assert not _checked(after, "2002")

    def test_the_submit_writes_each_row_to_its_chosen_member(self, client, chassis):
        first, second = chassis
        response = _submit(
            client,
            first,
            [2002],
            choices=[("data-choice-url", {"row": "2002", "field": "member", "value": str(second.pk)})],
        )

        assert response.status_code == 200
        assert list(Interface.objects.filter(name="mgmt").values_list("device_id", flat=True)) == [second.pk]


class TestTheSubmit:
    """A bulk submit claims the draft, writes the approved rows and puts back every row it did not write."""

    def test_the_submit_writes_the_approved_rows_and_consumes_them(self, client, device):
        response = _submit(client, device, [11])

        assert response.status_code == 200
        assert json.loads(response["HX-Trigger"]) == {"lnms-results-stale": None}
        assert response["HX-Reswap"] == "none"
        assert Interface.objects.filter(device=device, name="eth1").exists()
        after = _tab(client, device)
        assert response["X-Draft-Revision"] == after.table["data-draft-revision"]
        assert not _checked(after, "11")

    def test_the_submit_takes_the_required_rows_except_an_excluded_one(self, client, device):
        _submit(client, device, [21], states={"22": "off"}, auto_select_lag_members="1")

        assert set(Interface.objects.filter(device=device).values_list("name", flat=True)) == {"eth0", "po1", "eth21"}
        # The exclusion stays in the draft for the next submit.
        assert _tab(client, device).rows["22"]["data-intent"] == "off"

    def test_a_row_that_changed_after_its_selection_is_refused_and_put_back(self, client, device):
        draft = _select(client, device, [10])
        # The interface that the user approved is gone: the row would now create one.
        Interface.objects.filter(device=device, name="eth0").delete()

        response = _post(client, device, draft)

        assert any("eth0" in text and "changed" in text for text in _messages(response))
        assert _tab(client, device).rows["10"]["data-evidence-changed"] == "true"

    def test_a_submit_of_an_older_revision_writes_nothing_and_keeps_the_draft(self, client, device):
        draft = _select(client, device, [11])
        _select(client, device, [10])

        response = _post(client, device, draft)

        assert not Interface.objects.filter(device=device, name="eth1").exists()
        assert any("selection changed" in text for text in _messages(response))
        assert "X-Draft-Revision" not in response
        after = _tab(client, device)
        assert _checked(after, "11") and _checked(after, "10")

    def test_the_submit_ignores_posted_checkboxes_and_member_fields(self, client, device):
        draft = _select(client, device, [11])

        _post(client, device, {**draft, "select": ["21"], "device_selection_11": "999"})

        assert set(Interface.objects.filter(device=device).values_list("name", flat=True)) == {"eth0", "eth1"}


class TestTheRowAction:
    """``sync_one`` and ``rebind_one`` act on the row as its page shows it, and leave the draft as it is."""

    def test_sync_one_writes_the_shown_row_and_keeps_the_selection(self, client, device):
        _select(client, device, [10])

        response = _post(
            client, device, {"sync_one": "11", **row_action_fields(client, device, "interfaces", 11, **QUERY)}
        )

        assert response.status_code == 200
        assert Interface.objects.filter(device=device, name="eth1").exists()
        assert _checked(_tab(client, device), "10")

    def test_a_row_action_of_another_revision_or_evidence_writes_nothing(self, client, device):
        fields = row_action_fields(client, device, "interfaces", 11, **QUERY)
        _select(client, device, [10])

        stale = _post(client, device, {"sync_one": "11", **fields})
        forged = _post(
            client,
            device,
            {
                "sync_one": "11",
                **row_action_fields(client, device, "interfaces", 11, **QUERY),
                "evidence": fields["evidence"][:-2],
            },
        )

        assert not Interface.objects.filter(device=device, name="eth1").exists()
        assert all(any("changed after the page showed it" in text for text in _messages(r)) for r in (stale, forged))


@pytest.fixture
def vlan_device(client, settings):
    """Return a device whose rows 41 and 42 carry untagged VLAN 100, which two device groups and no global VLAN hold."""
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("iface-draft-vlan-user"))
    owner = make_device("iface-draft-vlans", librenms_cf={SERVER_KEY: {"id": 4400}})
    groups = []
    for name in ("iface-draft-a", "iface-draft-b"):
        group = VLANGroup.objects.create(name=name, slug=name, scope=owner.site)
        VLAN.objects.create(vid=100, name=f"v100-{name}", group=group)
        groups.append(group)
    seed_interfaces_snapshot(
        owner,
        [
            sync_port(41, "eth41", **port_vlans("access", 100)),
            sync_port(42, "eth42", **port_vlans("access", 100)),
        ],
        server_key=SERVER_KEY,
    )
    return owner, groups


def _evidence_groups(shown, row_key):
    return read_evidence(INTERFACE_TAB, shown.rows[row_key]["data-evidence"])["vlan_groups"]


class TestTheVlanGroups:
    """The VLAN groups dialog is server-rendered; Save is a draft command, and the row read shows the groups."""

    def test_the_dialog_renders_the_rows_vlans_and_previews_a_group(self, client, vlan_device):
        owner, (first, second) = vlan_device
        shown = _tab(client, owner)
        (button,) = [attrs for attrs in open_tags(_row_html(shown.html, "41"), "button") if "hx-get" in attrs]

        dialog = client.get(unescape(button["hx-get"]))
        preview = client.get(f"{unescape(button['hx-get'])}&group_100={second.pk}")

        assert dialog.status_code == preview.status_code == 200
        assert re.search(r'<select name="group_100"', dialog.content.decode())
        assert re.search(rf'<option value="{second.pk}"\s+selected>', preview.content.decode())
        (form,) = [attrs for attrs in open_tags(dialog.content.decode(), "form") if "data-write-url" in attrs]
        assert unescape(form["data-write-url"]) == unescape(shown.table["data-vlan-groups-url"])

    def test_save_sets_the_rows_group_and_its_read_shows_it(self, client, vlan_device):
        owner, (first, second) = vlan_device
        shown = _tab(client, owner)

        response = draft_command(client, shown, "data-vlan-groups-url", row="41", group_100=str(second.pk))

        assert response.status_code == 204
        assert json.loads(response["HX-Trigger"]) == {"lnms-row-stale": {"key": "41"}}
        after = _tab(client, owner)
        assert _evidence_groups(after, "41") == {"100": str(second.pk)}
        assert _evidence_groups(after, "42") != {"100": str(second.pk)}

    def test_apply_to_all_sets_the_group_of_every_row_with_the_vlan(self, client, vlan_device):
        owner, (first, second) = vlan_device
        shown = _tab(client, owner)

        response = draft_command(
            client, shown, "data-vlan-groups-url", row="41", group_100=str(second.pk), apply_to_all="1"
        )

        assert json.loads(response["HX-Trigger"]) == {"lnms-results-stale": None}
        after = _tab(client, owner)
        assert _evidence_groups(after, "41") == _evidence_groups(after, "42") == {"100": str(second.pk)}

    def test_a_group_the_row_does_not_offer_changes_nothing(self, client, vlan_device):
        from dcim.models import Site

        owner, _groups = vlan_device
        elsewhere = Site.objects.create(name="iface-draft-elsewhere", slug="iface-draft-elsewhere")
        foreign = VLANGroup.objects.create(name="iface-draft-foreign", slug="iface-draft-foreign", scope=elsewhere)
        shown = _tab(client, owner)

        draft_command(client, shown, "data-vlan-groups-url", row="41", group_100=str(foreign.pk))

        assert str(foreign.pk) not in _evidence_groups(_tab(client, owner), "41").values()

    def test_the_submit_writes_the_vlan_of_the_chosen_group(self, client, vlan_device):
        owner, (first, second) = vlan_device

        _submit(
            client,
            owner,
            [41],
            choices=[("data-vlan-groups-url", {"row": "41", "group_100": str(second.pk)})],
            exclude_columns=[],
        )

        interface = Interface.objects.get(device=owner, name="eth41")
        assert interface.untagged_vlan.group_id == second.pk


def test_a_virtual_machine_tab_selects_and_syncs_through_its_draft(client, settings):
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("iface-draft-vm-user"))
    vm = map_device_to_librenms(make_vm("iface-draft-vm"), 4500, server_key=SERVER_KEY)
    seed_interfaces_snapshot(vm, [sync_port(51, "ens3"), sync_port(52, "ens4")], server_key=SERVER_KEY)

    response = _submit(client, vm, [52])

    assert response.status_code == 200
    assert list(vm.interfaces.values_list("name", flat=True)) == ["ens4"]


def test_a_data_refresh_clears_the_selection_and_says_so(client, device):
    _select(client, device, [11])
    seed_interfaces_snapshot(device, [sync_port(11, "eth1")], server_key=SERVER_KEY)

    shown = _tab(client, device)

    assert not _checked(shown, "11")
    assert "data-selection-cleared" in shown.html


def test_a_command_starts_no_draft(client, device):
    shown = _tab(client, device)
    seed_interfaces_snapshot(device, [sync_port(11, "eth1")], server_key=SERVER_KEY)

    response = draft_command(client, shown, "data-choice-url", row="11", field="member", value="")

    assert response.status_code == 409


def test_a_rebind_acts_on_the_member_and_the_row_that_its_page_shows(client, settings):
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("iface-draft-rebind-user"))
    owner = make_device("iface-draft-rebind", librenms_cf={SERVER_KEY: {"id": 4600}})
    holder = bound_interface(owner, "eth9", 61)
    seed_interfaces_snapshot(owner, [sync_port(62, "eth9")], server_key=SERVER_KEY)
    url = reverse(
        "plugins:netbox_librenms_plugin:rebind_interface_port", kwargs={"object_type": "device", "object_id": owner.pk}
    )
    fields = {"server_key": SERVER_KEY, "rebind_one": "62", "rebind_expected_port_62": "61"}
    stale = row_action_fields(client, owner, "interfaces", 62, **QUERY)
    _select(client, owner, [62])

    refused = client.post(f"{url}?interface_name_field=ifName", {**fields, **stale}, **HTMX)
    shown = row_action_fields(client, owner, "interfaces", 62, **QUERY)
    rebound = client.post(f"{url}?interface_name_field=ifName", {**fields, **shown}, **HTMX)

    assert any("changed after the page showed it" in text for text in _messages(refused))
    assert any("is now bound to LibreNMS port 62" in text for text in _messages(rebound))
    holder.refresh_from_db()
    from netbox_librenms_plugin.server_mappings import read_mapping

    assert read_mapping(holder).own_id(SERVER_KEY) == 62


# ============================================
# THE REQUIRED-ROW RULE CASES
# ============================================
# Each case holds the requirements, the page, the intents, and the rows that show checked after each click.

CASES = json.loads((Path(__file__).parent / "selection_rule_cases.json").read_text(encoding="utf-8"))


def _checked_on_page(case, intents):
    """Return the page rows that the server renders checked for *intents*."""
    checked = checked_rows(
        {key: set(values) for key, values in case["edges"].items()},
        {key: Intent(value) for key, value in intents.items()},
        set(case.get("blocked", ())),
        enabled=case.get("enabled", True),
    )
    return sorted(key for key in checked if key in case["page"])


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_server_rule_checks_the_rows_of_each_step(case):
    assert _checked_on_page(case, case["intents"]) == sorted(case["checked"])
    for step in case.get("steps", ()):
        assert _checked_on_page(case, step["intents"]) == sorted(step["checked"]), step


def test_the_snapshot_relationships_give_both_lag_directions_and_one_parent_and_bridge_direction():
    edges = requirement_edges(lag_members={11: 10, 12: 10}, sub_interfaces={21: 20}, bridge_members={31: 30})

    assert edges == {"11": {"10"}, "12": {"10"}, "10": {"11", "12"}, "21": {"20"}, "31": {"30"}}


def test_a_page_row_counts_the_selected_off_page_rows_that_require_it():
    edges = {"A": {"B"}, "C": {"B"}, "D": {"B"}}
    intents = {"A": Intent.ON, "C": Intent.ON, "D": Intent.ON}

    # D is on the page, so only A and C count; a blocked requirer counts for nothing.
    assert off_page_requirers(edges, intents, {"C"}, page_keys={"B", "D"}) == {"B": 1}


def test_a_selected_row_is_not_among_the_taken_rows_and_a_blocked_one_takes_nothing():
    edges = {"A": {"B"}, "B": {"A"}, "X": {"Y"}}

    assert taken_rows(edges, {"A": Intent.ON, "B": Intent.ON, "X": Intent.ON}, blocked={"X"}) == set()
    assert requirements(edges, "A", blocked=set()) == {"B"}
