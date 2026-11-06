"""The interface sync and Rebind POSTs answer an htmx submit with the messages and a results-stale event, not a redirect."""

import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from dcim.models import Interface
from django.urls import reverse
from virtualization.models import VMInterface

from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests._html_helpers import open_tags
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    make_vm,
    map_device_to_librenms,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import select_interface_rows
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import draft_tab, row_action_fields

SERVER_KEY = "default"
FIRST_PORT = 5000
PORT_COUNT = 30
RESULTS_STALE = "lnms-results-stale"


def _port(port_id, name):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": "ethernetCsmacd",
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }


def _ports():
    # Zero-padded names keep the table's name order equal to the port order.
    return [_port(FIRST_PORT + index, f"eth{index:02d}") for index in range(PORT_COUNT)]


def _seed(owner, ports):
    seed_interfaces_snapshot(owner, ports, server_key=SERVER_KEY)


def _url(name, owner, object_type, **query):
    # The tab posts to a URL that carries the page query, as the rendered form action does.
    return (
        reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"object_type": object_type, "object_id": owner.pk})
        + "?"
        + urlencode({"interface_name_field": "ifName", **query})
    )


def _post(client, owner, data, *, name="sync_selected_interfaces", object_type="device", htmx=True, **query):
    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    return client.post(_url(name, owner, object_type, **query), {"server_key": SERVER_KEY, **data}, **headers)


def _sync_data(client, owner, *port_ids):
    return {
        **select_interface_rows(client, owner, port_ids),
        "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
    }


def _assert_draft_answer(response):
    """Assert the htmx answer swaps only the messages and asks the page to read the tab's results again."""
    assert response.status_code == 200
    assert "Location" not in response
    assert response["HX-Reswap"] == "none"
    assert RESULTS_STALE in json.loads(response["HX-Trigger"])


@pytest.mark.django_db
class TestHtmxSync:
    """An htmx sync answers with its messages; the page reads the tab at the page and page size the user was on."""

    @pytest.fixture(autouse=True)
    def _server(self, settings):
        configure_default_librenms_server(settings)

    def test_the_form_posts_the_page_query_and_the_answer_names_the_draft_revision(self, client):
        device = make_device("htmx-sync-page", librenms_cf={SERVER_KEY: {"id": 81}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-page-user"))
        synced_port = FIRST_PORT + 12
        page_query = {"interfaces_page": "2", "interfaces_per_page": "10"}
        shown = draft_tab(client, device, "interfaces", interface_name_field="ifName", **page_query)
        (form,) = [attrs for attrs in open_tags(shown.html, "form") if attrs.get("id") == "interface-sync-form"]
        # The page state lives in the URL only, never in a hidden input the sync form would post.
        start = shown.html.index('<form method="post" id="interface-sync-form"')
        sync_form = shown.html[start : shown.html.index("</form>", start)]
        hidden = {attrs.get("name") for attrs in open_tags(sync_form, "input") if attrs.get("type") == "hidden"}
        assert not hidden & {"interfaces_page", "interfaces_per_page"}
        action = urlsplit(form["hx-post"])
        assert action.path == _url("sync_selected_interfaces", device, "device").split("?")[0]
        assert parse_qs(action.query) == {
            "server_key": [SERVER_KEY],
            "interface_name_field": ["ifName"],
            "interfaces_page": ["2"],
            "interfaces_per_page": ["10"],
        }
        assert form["hx-target"] == "#htmx-modal-content"

        response = _post(client, device, _sync_data(client, device, synced_port), **page_query)

        _assert_draft_answer(response)
        assert "Selected interfaces synced successfully." in response.content.decode()
        assert response["X-Draft-Revision"] == draft_tab(client, device, "interfaces").table["data-draft-revision"]
        interface = Interface.objects.get(device=device, name="eth12")
        assert read_mapping(interface).own_id(SERVER_KEY) == synced_port

    def test_the_cache_transition_rides_on_the_answer(self, client, django_capture_on_commit_callbacks):
        device = make_device("htmx-sync-transition", librenms_cf={SERVER_KEY: {"id": 82}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-transition-user"))
        data = _sync_data(client, device, FIRST_PORT)

        with django_capture_on_commit_callbacks(execute=True):
            response = _post(client, device, data)

        transition = json.loads(response["X-LibreNMS-Cache-Transition"])
        assert transition["source_tab"] == "interfaces"
        trigger = json.loads(response["HX-Trigger"])
        assert trigger["librenmsCacheChanged"] == transition
        assert RESULTS_STALE in trigger

    def test_a_rejected_submit_reports_in_the_answer(self, client):
        device = make_device("htmx-sync-empty", librenms_cf={SERVER_KEY: {"id": 83}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-empty-user"))

        response = _post(client, device, _sync_data(client, device), interfaces_page="3", interfaces_per_page="10")

        _assert_draft_answer(response)
        assert "No interfaces selected for synchronization." in response.content.decode()
        assert not Interface.objects.filter(device=device).exists()

    def test_an_unknown_server_writes_nothing_and_says_so(self, client):
        device = make_device("htmx-sync-server", librenms_cf={SERVER_KEY: {"id": 84}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-server-user"))

        response = _post(client, device, {**_sync_data(client, device, FIRST_PORT), "server_key": "gone"})

        _assert_draft_answer(response)
        assert "Selected LibreNMS server is no longer configured." in response.content.decode()
        assert "X-Draft-Revision" not in response
        assert not Interface.objects.filter(device=device).exists()

    def test_a_virtual_machine_syncs_through_its_own_draft(self, client):
        vm = map_device_to_librenms(make_vm("htmx-sync-vm"), 87, server_key=SERVER_KEY)
        _seed(vm, _ports())
        client.force_login(make_superuser("htmx-sync-vm-user"))
        assert 'id="librenms-interface-table-vm"' in draft_tab(client, vm, "interfaces").html

        response = _post(
            client, vm, _sync_data(client, vm, FIRST_PORT + 1), object_type="virtualmachine", interfaces_per_page="10"
        )

        _assert_draft_answer(response)
        assert "Selected interfaces synced successfully." in response.content.decode()
        assert VMInterface.objects.filter(virtual_machine=vm, name="eth01").exists()

    def test_a_plain_submit_still_redirects_with_the_page_query(self, client):
        device = make_device("htmx-sync-plain", librenms_cf={SERVER_KEY: {"id": 85}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-plain-user"))

        response = _post(client, device, _sync_data(client, device, FIRST_PORT), htmx=False, interfaces_page="2")

        assert response.status_code == 302
        assert "tab=interfaces" in response["Location"]
        assert "interfaces_page=2" in response["Location"]


@pytest.mark.django_db
def test_an_htmx_rebind_answers_with_its_messages(client, settings, django_capture_on_commit_callbacks):
    configure_default_librenms_server(settings)
    device = make_device("htmx-rebind", librenms_cf={SERVER_KEY: {"id": 86}})
    interface = make_interface(device, "eth12")
    seed_mapping(interface, SERVER_KEY, own=8679, save=False)
    interface.save()
    _seed(device, _ports())
    client.force_login(make_superuser("htmx-rebind-user"))
    port_id = FIRST_PORT + 12
    shown = row_action_fields(client, device, "interfaces", port_id, interface_name_field="ifName")

    with django_capture_on_commit_callbacks(execute=True):
        response = _post(
            client,
            device,
            {"rebind_one": str(port_id), f"rebind_expected_port_{port_id}": "8679", **shown},
            name="rebind_interface_port",
            interfaces_page="2",
            interfaces_per_page="10",
        )

    _assert_draft_answer(response)
    assert f"is now bound to LibreNMS port {port_id}" in response.content.decode()
    interface.refresh_from_db()
    assert read_mapping(interface).own_id(SERVER_KEY) == port_id
    assert json.loads(response["HX-Trigger"])["librenmsCacheChanged"]["source_tab"] == "interfaces"
