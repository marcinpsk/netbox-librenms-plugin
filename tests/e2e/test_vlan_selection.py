"""
The VLANs tab keeps its selection and its per-row VLAN groups in the user's server-side draft.

The device is linked to the stub's IOS XE recording, for which the stub serves VLANs 100 and 200. VLAN 100
exists in group A of the device's site, group B of the site is empty, and VLAN 200 exists nowhere. Each test
refreshes the tab, which starts an empty draft.
"""

import re
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from .conftest import SERVER_KEY, NetBoxAPI, recording_device_id

RECORDING = "iosxe-subinterfaces"
LIBRENMS_ID = recording_device_id(RECORDING)
TABLE = "#librenms-vlan-table"


@pytest.fixture(scope="module")
def vlan_device(netbox_api: NetBoxAPI, placement: dict):
    """Create a device linked to the IOS XE recording, group A with VLAN 100, and an empty group B."""
    run = uuid4().hex[:12]
    manufacturer = netbox_api.create("dcim/manufacturers", {"name": f"E2E VLAN {run}", "slug": f"e2e-vlan-{run}"})
    device_type = netbox_api.create(
        "dcim/device-types", {"manufacturer": manufacturer["id"], "model": f"E2E-VLAN-{run}", "slug": f"e2e-vlan-{run}"}
    )
    device = netbox_api.create(
        "dcim/devices",
        {
            "name": f"e2e-vlan-{run}",
            "device_type": device_type["id"],
            "role": placement["role"]["id"],
            "site": placement["site"]["id"],
            "custom_fields": {"librenms_id": {SERVER_KEY: LIBRENMS_ID}},
        },
    )
    groups = [
        netbox_api.create(
            "ipam/vlan-groups",
            {
                "name": f"e2e-vlan-{name}-{run}",
                "slug": f"e2e-vlan-{name}-{run}",
                "scope_type": "dcim.site",
                "scope_id": placement["site"]["id"],
            },
        )
        for name in ("a", "b")
    ]
    yield {"device": device, "groups": groups}
    netbox_api.delete(f"dcim/devices/{device['id']}")
    for group in groups:
        for vlan in netbox_api.list("ipam/vlans", group_id=group["id"]):
            netbox_api.delete(f"ipam/vlans/{vlan['id']}")
        netbox_api.delete(f"ipam/vlan-groups/{group['id']}")
    netbox_api.delete(f"dcim/device-types/{device_type['id']}")
    netbox_api.delete(f"dcim/manufacturers/{manufacturer['id']}")


def _row(page: Page, vid: int):
    return page.locator(f'{TABLE} tr[data-row-key="{vid}"]')


def _box(page: Page, vid: int):
    return _row(page, vid).locator('input[name="select"]')


def _choose_group(page: Page, vid: int, group_id) -> None:
    page.evaluate(
        """([vid, group]) => {
            const row = [...document.querySelectorAll('tr[data-row-key]')].find(tr => tr.dataset.rowKey === vid);
            const select = row.querySelector('select[data-draft-choice="group"]');
            if (select.tomselect) select.tomselect.setValue(group); else {
                select.value = group;
                select.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""",
        [str(vid), str(group_id)],
    )


@pytest.fixture
def vlan_tab(logged_in_page: Page, netbox_url: str, netbox_api: NetBoxAPI, vlan_device: dict):
    """Open the device's VLANs tab with fresh data: VLAN 100 in group A only, and a new draft."""
    group_a, group_b = vlan_device["groups"]
    for group in (group_a, group_b):
        for vlan in netbox_api.list("ipam/vlans", group_id=group["id"]):
            netbox_api.delete(f"ipam/vlans/{vlan['id']}")
    netbox_api.create("ipam/vlans", {"vid": 100, "name": "Stub VLAN 100", "group": group_a["id"]})
    page = logged_in_page
    page.goto(
        f"{netbox_url}/dcim/devices/{vlan_device['device']['id']}/librenms-sync/?tab=vlans&server_key={SERVER_KEY}"
    )
    with page.expect_response(lambda response: "/vlan-sync/" in response.url, timeout=60_000):
        page.get_by_role("button", name="Refresh VLANs").click()
    expect(_row(page, 100)).to_have_count(1, timeout=60_000)
    page.wait_for_function("history.state && history.state.lnmsSync === true")
    return page


def test_a_group_choice_reads_the_row_again_compared_in_that_group(vlan_tab: Page, vlan_device: dict):
    page = vlan_tab
    _group_a, group_b = vlan_device["groups"]
    expect(_row(page, 100).locator('td[data-col="status"] button[name="sync_one"]')).to_have_count(0)

    with page.expect_response(lambda response: "/vlan-row/100/" in response.url):
        _choose_group(page, 100, group_b["id"])

    # VLAN 100 is not in group B, so the row now offers to create it there.
    expect(_row(page, 100).locator('td[data-col="status"] button[name="sync_one"]')).to_have_count(1)
    expect(_row(page, 100).locator(f'option[value="{group_b["id"]}"][selected]')).to_have_count(1)


def test_the_submit_creates_the_vlan_in_the_chosen_group(vlan_tab: Page, netbox_api: NetBoxAPI, vlan_device: dict):
    page = vlan_tab
    _group_a, group_b = vlan_device["groups"]
    with page.expect_response(lambda response: "/vlan-row/200/" in response.url):
        _choose_group(page, 200, group_b["id"])
    _box(page, 200).click()

    page.get_by_role("button", name="Sync Selected VLANs").click()

    expect(page.locator("#django-messages")).to_contain_text("VLANs synced")
    assert [vlan["vid"] for vlan in netbox_api.list("ipam/vlans", group_id=group_b["id"])] == [200]
    expect(_box(page, 200)).not_to_be_checked()


def test_a_group_choice_on_a_selected_row_asks_to_select_it_again(vlan_tab: Page, vlan_device: dict):
    page = vlan_tab
    group_a, _group_b = vlan_device["groups"]
    _box(page, 200).click()
    page.evaluate("librenmsSyncDraft.flush()")

    with page.expect_response(lambda response: "/vlan-row/200/" in response.url):
        _choose_group(page, 200, group_a["id"])

    expect(_box(page, 200)).not_to_be_checked()
    expect(_row(page, 200)).to_have_attribute("data-evidence-changed", "true")


def test_a_vlan_selection_reaches_the_vlan_draft_while_the_interfaces_pane_shows_a_draft_table(vlan_tab: Page):
    page = vlan_tab
    # With interface data, the page renders a draft table in the interfaces pane, before the VLANs pane.
    page.locator("#interfaces-tab").click()
    page.wait_for_url(re.compile(r"tab=interfaces"))
    with page.expect_response(lambda response: "/interface-sync/" in response.url, timeout=60_000):
        page.get_by_role("button", name="Refresh Interfaces").click()
    expect(page.locator("#librenms-interface-table")).to_have_count(1, timeout=60_000)
    page.locator("#vlans-tab").click()
    page.wait_for_url(re.compile(r"tab=vlans"))

    _box(page, 200).click()
    page.evaluate("librenmsSyncDraft.flush()")

    expect(page.locator("#librenms-draft-notice")).to_have_count(0)
    page.reload()
    expect(_box(page, 200)).to_be_checked()
