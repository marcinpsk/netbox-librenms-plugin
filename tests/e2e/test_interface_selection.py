"""
The Interfaces tab keeps its selection in the user's server-side draft.

The device is linked to the stub's ArcOS recording, whose bond1 aggregates swp4, swp5 and swp6, and whose
swp3 carries VLANs 100 and 200 (the stub adds them to a recording without VLANs). No NetBox interface
exists, so each row can be created. Each test refreshes the tab, which starts an empty draft.
"""

import re
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from .conftest import SERVER_KEY, NetBoxAPI, recorded, recording_device_id

RECORDING = "arcos-lag-transceivers"
LIBRENMS_ID = recording_device_id(RECORDING)
PORTS = {
    port["ifName"]: str(port["port_id"])
    for port in recorded(RECORDING, f"/api/v0/devices/{LIBRENMS_ID}/ports")["ports"]
}
TABLE = "#librenms-interface-table"
LAG = [PORTS[name] for name in ("bond1", "swp4", "swp5", "swp6")]


@pytest.fixture(scope="module")
def interface_device(netbox_api: NetBoxAPI, placement: dict):
    """Create a device linked to the ArcOS recording, and two VLAN groups of its site that both hold VLAN 100."""
    run = uuid4().hex[:12]
    manufacturer = netbox_api.create("dcim/manufacturers", {"name": f"E2E If {run}", "slug": f"e2e-if-{run}"})
    device_type = netbox_api.create(
        "dcim/device-types", {"manufacturer": manufacturer["id"], "model": f"E2E-IF-{run}", "slug": f"e2e-if-{run}"}
    )
    device = netbox_api.create(
        "dcim/devices",
        {
            "name": f"e2e-if-{run}",
            "device_type": device_type["id"],
            "role": placement["role"]["id"],
            "site": placement["site"]["id"],
            "custom_fields": {"librenms_id": {SERVER_KEY: LIBRENMS_ID}},
        },
    )
    groups = []
    for name in ("a", "b"):
        group = netbox_api.create(
            "ipam/vlan-groups",
            {
                "name": f"e2e-if-{name}-{run}",
                "slug": f"e2e-if-{name}-{run}",
                "scope_type": "dcim.site",
                "scope_id": placement["site"]["id"],
            },
        )
        netbox_api.create("ipam/vlans", {"vid": 100, "name": f"e2e-if-{name}-100", "group": group["id"]})
        groups.append(group)
    yield {"device": device, "groups": groups}
    netbox_api.delete(f"dcim/devices/{device['id']}")
    for group in groups:
        for vlan in netbox_api.list("ipam/vlans", group_id=group["id"]):
            netbox_api.delete(f"ipam/vlans/{vlan['id']}")
        netbox_api.delete(f"ipam/vlan-groups/{group['id']}")
    netbox_api.delete(f"dcim/device-types/{device_type['id']}")
    netbox_api.delete(f"dcim/manufacturers/{manufacturer['id']}")


def _row(page: Page, key: str):
    return page.locator(f'{TABLE} tr[data-row-key="{key}"]')


def _box(page: Page, key: str):
    return _row(page, key).locator('input[name="select"]')


def _settled(page: Page) -> None:
    """Wait until the draft writer has acknowledged every input of the page."""
    page.evaluate("librenmsSyncDraft.flush()")


def _is_selection_command(url: str) -> bool:
    return url.split("?")[0].endswith("/interface-selection/")


@pytest.fixture
def interface_tab(logged_in_page: Page, netbox_url: str, netbox_api: NetBoxAPI, interface_device: dict):
    """Open the device's Interfaces tab with fresh data and no NetBox interfaces: a new draft."""
    for interface in netbox_api.list("dcim/interfaces", device_id=interface_device["device"]["id"]):
        netbox_api.delete(f"dcim/interfaces/{interface['id']}")
    page = logged_in_page
    page.goto(
        f"{netbox_url}/dcim/devices/{interface_device['device']['id']}/librenms-sync/"
        f"?tab=interfaces&server_key={SERVER_KEY}&interfaces_per_page=100"
    )
    with page.expect_response(lambda response: "/interface-sync/" in response.url, timeout=60_000):
        page.get_by_role("button", name="Refresh Interfaces").click()
    expect(_row(page, LAG[0])).to_have_count(1, timeout=60_000)
    page.wait_for_function("history.state && history.state.lnmsSync === true")
    return page


def test_a_lag_member_ticks_its_aggregate_and_the_other_members_before_the_draft_answers(interface_tab: Page):
    page = interface_tab
    held = []
    page.route(_is_selection_command, lambda route: held.append(route))

    _box(page, PORTS["swp4"]).click()

    for key in LAG:
        expect(_box(page, key)).to_be_checked()
    for _attempt in range(40):
        if held:
            break
        page.wait_for_timeout(50)
    assert len(held) == 1
    held[0].continue_()
    page.unroute(_is_selection_command)
    _settled(page)
    page.reload()
    for key in LAG:
        expect(_box(page, key)).to_be_checked()


def test_a_cleared_required_row_stays_out_of_the_submit(
    interface_tab: Page, netbox_api: NetBoxAPI, interface_device: dict
):
    page = interface_tab
    _box(page, PORTS["swp4"]).click()
    _box(page, PORTS["swp6"]).click()
    expect(_box(page, PORTS["swp6"])).not_to_be_checked()
    expect(_box(page, PORTS["bond1"])).to_be_checked()

    page.get_by_role("button", name="Sync Selected Interfaces").click()

    expect(page.locator("#django-messages")).to_contain_text("synced successfully")
    created = {
        interface["name"]
        for interface in netbox_api.list("dcim/interfaces", device_id=interface_device["device"]["id"])
    }
    assert created == {"bond1", "swp4", "swp5"}
    # The page reads the tab again: the written rows are no longer selected, the exclusion stays.
    expect(_box(page, PORTS["swp4"])).not_to_be_checked()
    expect(_row(page, PORTS["swp6"])).to_have_attribute("data-intent", "off")


def test_the_vlan_dialog_saves_a_group_and_the_row_reads_again(interface_tab: Page, interface_device: dict):
    page = interface_tab
    _first, second = interface_device["groups"]
    row = _row(page, PORTS["swp3"])
    row.locator("button.vlan-edit-btn").click()
    dialog = page.locator("#vlanDetailModalContent")
    select = dialog.locator('select[name="group_100"]')
    expect(select).to_have_count(1)
    # The preview recolours the VID cells only, so the select keeps the group that the user chose.
    with page.expect_response(lambda response: "/interface-row/" in response.url and "/vlan-groups/" in response.url):
        page.evaluate(
            """(group) => {
                const select = document.querySelector('#vlanDetailModalContent select[name="group_100"]');
                if (select.tomselect) select.tomselect.setValue(group); else {
                    select.value = group;
                    select.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            str(second["id"]),
        )
    expect(select).to_have_value(str(second["id"]))

    with page.expect_response(lambda response: "/interface-row/" in response.url):
        dialog.get_by_role("button", name="Save").click()

    expect(_row(page, PORTS["swp3"]).locator("td[data-col='vlans'] span[title]").first).to_have_attribute(
        "title", re.compile(rf"VLAN 100\(U\) → {re.escape(second['name'])}")
    )


def test_a_data_refresh_clears_the_selection_and_says_so(interface_tab: Page):
    page = interface_tab
    _box(page, PORTS["swp3"]).click()
    _settled(page)

    with page.expect_response(lambda response: "/interface-sync/" in response.url, timeout=60_000):
        page.get_by_role("button", name="Refresh Interfaces").click()

    expect(page.locator("[data-selection-cleared]")).to_be_visible()
    expect(_box(page, PORTS["swp3"])).not_to_be_checked()
