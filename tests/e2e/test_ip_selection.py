"""
The IP Addresses tab keeps its selection in the user's server-side draft.

The device is linked to the stub's IOS XE recording, whose ten addresses sit on interfaces that the
fixture creates in NetBox, so each row can be created. The tab shows three rows on its first page
(NetBox's paginator moves up to five last rows onto the page before), so a selection crosses pages.
Each test refreshes the tab, which starts an empty draft.
"""

import json
import re
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from .conftest import SERVER_KEY, NetBoxAPI, recorded, recording_device_id

RECORDING = "iosxe-subinterfaces"
LIBRENMS_ID = recording_device_id(RECORDING)
_PORTS = {port["port_id"]: port for port in recorded(RECORDING, f"/api/v0/devices/{LIBRENMS_ID}/ports")["ports"]}
_ADDRESSES = recorded(RECORDING, f"/api/v0/devices/{LIBRENMS_ID}/ip")["addresses"]
ROWS = [f"{row['ipv4_address']}/{row['ipv4_prefixlen']}" for row in _ADDRESSES]
INTERFACES = sorted({_PORTS[row["port_id"]]["ifName"] for row in _ADDRESSES})
# The LibreNMS VRFs of the rows; Create VRF adds them to NetBox.
LIBRENMS_VRFS = {vrf["vrf_name"] for vrf in recorded(RECORDING, "/api/v0/routing/vrf")["vrfs"]}
TABLE = "#librenms-ipaddress-table"
CONTENT = "#ipaddress-sync-content"
PER_PAGE = 3


def _delete_librenms_vrfs(netbox_api: NetBoxAPI) -> None:
    for name in LIBRENMS_VRFS:
        for vrf in netbox_api.list("ipam/vrfs", name=name):
            netbox_api.delete(f"ipam/vrfs/{vrf['id']}")


@pytest.fixture(scope="module")
def ip_device(netbox_api: NetBoxAPI, placement: dict):
    """Create a device linked to the IOS XE recording, with the NetBox interfaces its addresses name."""
    _delete_librenms_vrfs(netbox_api)
    run = uuid4().hex[:12]
    manufacturer = netbox_api.create("dcim/manufacturers", {"name": f"E2E IP {run}", "slug": f"e2e-ip-{run}"})
    device_type = netbox_api.create(
        "dcim/device-types", {"manufacturer": manufacturer["id"], "model": f"E2E-IP-{run}", "slug": f"e2e-ip-{run}"}
    )
    device = netbox_api.create(
        "dcim/devices",
        {
            "name": f"e2e-ip-{run}",
            "device_type": device_type["id"],
            "role": placement["role"]["id"],
            "site": placement["site"]["id"],
            "custom_fields": {"librenms_id": {SERVER_KEY: LIBRENMS_ID}},
        },
    )
    for name in INTERFACES:
        netbox_api.create("dcim/interfaces", {"device": device["id"], "name": name, "type": "virtual"})
    vrf = netbox_api.create("ipam/vrfs", {"name": f"e2e-blue-{run}"})
    yield {"device": device, "vrf": vrf}
    for address in netbox_api.list("ipam/ip-addresses", device_id=device["id"]):
        netbox_api.delete(f"ipam/ip-addresses/{address['id']}")
    netbox_api.delete(f"dcim/devices/{device['id']}")
    netbox_api.delete(f"ipam/vrfs/{vrf['id']}")
    _delete_librenms_vrfs(netbox_api)
    netbox_api.delete(f"dcim/device-types/{device_type['id']}")
    netbox_api.delete(f"dcim/manufacturers/{manufacturer['id']}")


def _tab_url(netbox_url: str, device: dict, **query) -> str:
    params = "&".join(f"{key}={value}" for key, value in {"ipaddresses_per_page": PER_PAGE, **query}.items())
    return f"{netbox_url}/dcim/devices/{device['id']}/librenms-sync/?tab=ipaddresses&server_key={SERVER_KEY}&{params}"


def _row(page: Page, key: str):
    return page.locator(f'{TABLE} tr[data-row-key="{key}"]')


def _box(page: Page, key: str):
    return _row(page, key).locator('input[name="select"]')


def _settled(page: Page) -> None:
    """Wait until the draft writer has acknowledged every input of the page."""
    page.evaluate("librenmsSyncDraft.flush()")


def _page_link(page: Page, number: int):
    return page.locator(f"{CONTENT} .pagination a.page-link", has_text=str(number)).first


def _is_selection_command(url: str) -> bool:
    return "/ip-selection/" in url and "/choice/" not in url and "/management/" not in url


@pytest.fixture
def ip_tab(logged_in_page: Page, netbox_url: str, netbox_api: NetBoxAPI, ip_device: dict):
    """Open the device's IP tab with fresh data and no NetBox addresses: a new draft."""
    for address in netbox_api.list("ipam/ip-addresses", device_id=ip_device["device"]["id"]):
        netbox_api.delete(f"ipam/ip-addresses/{address['id']}")
    page = logged_in_page
    page.goto(_tab_url(netbox_url, ip_device["device"]))
    with page.expect_response(lambda response: "/ipaddress-sync/" in response.url, timeout=60_000):
        page.get_by_role("button", name="Refresh IP Addresses").click()
    expect(_row(page, ROWS[0])).to_have_count(1, timeout=60_000)
    page.wait_for_function("history.state && history.state.lnmsSync === true")
    return page


def test_a_checkbox_paints_before_the_draft_answers(ip_tab: Page):
    page = ip_tab
    held = []
    page.route(_is_selection_command, lambda route: held.append(route))

    _box(page, ROWS[0]).click()

    expect(_box(page, ROWS[0])).to_be_checked()
    for _attempt in range(40):
        if held:
            break
        page.wait_for_timeout(50)
    assert len(held) == 1
    expect(_box(page, ROWS[0])).to_be_checked()
    held[0].continue_()
    page.unroute(_is_selection_command)
    _settled(page)


def test_shift_range_and_select_all_reach_the_draft(ip_tab: Page, netbox_url: str, ip_device: dict):
    page = ip_tab
    _box(page, ROWS[0]).click()
    _box(page, ROWS[1]).click(modifiers=["Shift"])
    _settled(page)
    page.reload()

    for key in ROWS[:2]:
        expect(_box(page, key)).to_be_checked()
    expect(_box(page, ROWS[2])).not_to_be_checked()

    page.locator(f"{TABLE} th input.toggle").check()
    _settled(page)
    page.reload()
    for key in ROWS[:PER_PAGE]:
        expect(_box(page, key)).to_be_checked()


def test_the_selection_survives_pagination_filters_search_and_per_page(ip_tab: Page):
    page = ip_tab
    _box(page, ROWS[0]).click()
    _page_link(page, 2).click()
    expect(_row(page, ROWS[PER_PAGE])).to_have_count(1)
    # The page states the selected row that it does not show, which a submit takes too.
    expect(page.locator(f"{CONTENT} [data-offpage-selection]")).to_be_visible()
    _box(page, ROWS[PER_PAGE]).click()

    page.go_back()
    expect(_box(page, ROWS[0])).to_be_checked()

    page.locator("#id_ipaddresses_q").fill(ROWS[PER_PAGE].partition("/")[0])
    expect(_row(page, ROWS[0])).to_have_count(0)
    expect(_box(page, ROWS[PER_PAGE])).to_be_checked()
    page.locator("#id_ipaddresses_q").fill("")
    expect(_row(page, ROWS[0])).to_have_count(1)
    expect(_box(page, ROWS[0])).to_be_checked()

    page.locator(f"{CONTENT} .dropdown-menu a.dropdown-item", has_text=re.compile(r"^\s*25\s*$")).first.dispatch_event(
        "click"
    )
    expect(_row(page, ROWS[PER_PAGE])).to_have_count(1)
    expect(_box(page, ROWS[0])).to_be_checked()
    expect(_box(page, ROWS[PER_PAGE])).to_be_checked()


def test_a_submit_syncs_the_rows_of_every_page(ip_tab: Page, netbox_api: NetBoxAPI, ip_device: dict):
    page = ip_tab
    _box(page, ROWS[0]).click()
    _page_link(page, 2).click()
    _box(page, ROWS[PER_PAGE]).click()

    page.get_by_role("button", name="Sync Selected IP Addresses").click()

    expect(page.locator("#django-messages")).to_contain_text("Created IP addresses")
    created = {
        address["address"] for address in netbox_api.list("ipam/ip-addresses", device_id=ip_device["device"]["id"])
    }
    assert created == {ROWS[0], ROWS[PER_PAGE]}
    # The tab reads its results again: the written rows are no longer selected.
    expect(_box(page, ROWS[PER_PAGE])).not_to_be_checked()


def test_an_invalid_filter_shows_the_hidden_selection_and_clear_empties_what_a_submit_writes(
    ip_tab: Page, netbox_api: NetBoxAPI, ip_device: dict
):
    page = ip_tab
    _box(page, ROWS[0]).click()
    _box(page, ROWS[1]).click()
    _settled(page)

    # The search is longer than a filter takes: the tab shows no row, and says which selected rows a submit writes.
    page.locator("#id_ipaddresses_q").fill("x" * 300)
    expect(page.locator(f"{CONTENT} [data-filter-invalid]")).to_be_visible()
    notice = page.locator(f"{CONTENT} [data-offpage-selection]")
    expect(notice).to_be_visible()
    clear = notice.locator("[data-clear-offpage]")
    assert sorted(json.loads(clear.get_attribute("data-clear-offpage"))) == sorted(ROWS[:2])

    clear.click()
    expect(notice).to_have_count(0)
    with page.expect_response(lambda response: "/sync-ip-addresses/" in response.url):
        page.get_by_role("button", name="Sync Selected IP Addresses").click()

    assert netbox_api.list("ipam/ip-addresses", device_id=ip_device["device"]["id"]) == []


def test_a_submit_with_an_invalid_filter_writes_the_rows_that_the_notice_names(
    ip_tab: Page, netbox_api: NetBoxAPI, ip_device: dict
):
    page = ip_tab
    _box(page, ROWS[0]).click()
    _box(page, ROWS[1]).click()
    _settled(page)
    page.locator("#id_ipaddresses_q").fill("x" * 300)
    clear = page.locator(f"{CONTENT} [data-offpage-selection] [data-clear-offpage]")
    expect(clear).to_be_visible()
    named = set(json.loads(clear.get_attribute("data-clear-offpage")))

    with page.expect_response(lambda response: "/sync-ip-addresses/" in response.url):
        page.get_by_role("button", name="Sync Selected IP Addresses").click()

    created = {
        address["address"] for address in netbox_api.list("ipam/ip-addresses", device_id=ip_device["device"]["id"])
    }
    assert created == named == set(ROWS[:2])


def test_a_vrf_choice_repaints_the_row_and_create_vrf_adds_the_missing_vrf(ip_tab: Page, ip_device: dict):
    page = ip_tab
    key = ROWS[0]
    page.evaluate(
        """([key, vrf]) => {
            const row = [...document.querySelectorAll('tr[data-row-key]')].find(tr => tr.dataset.rowKey === key);
            const select = row.querySelector('select.vrf-select');
            if (select.tomselect) select.tomselect.setValue(vrf); else {
                select.value = vrf;
                select.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""",
        [key, str(ip_device["vrf"]["id"])],
    )

    expect(_row(page, key).locator(f'option[value="{ip_device["vrf"]["id"]}"][selected]')).to_have_count(1)
    create = page.locator(f'{TABLE} button[name="create_vrf"]').first
    expect(create).to_be_visible()
    row_key = create.locator("xpath=ancestor::tr").get_attribute("data-row-key")
    create.click()
    page.locator("#librenms-confirm-modal").get_by_role("button", name="Confirm").click()

    expect(page.locator("#django-messages")).to_contain_text("Created NetBox VRF")
    expect(_row(page, row_key).locator('button[name="create_vrf"]')).to_have_count(0)


def test_a_data_refresh_clears_the_selection_and_says_so(ip_tab: Page):
    page = ip_tab
    _box(page, ROWS[0]).click()
    _settled(page)

    with page.expect_response(lambda response: "/ipaddress-sync/" in response.url, timeout=60_000):
        page.get_by_role("button", name="Refresh IP Addresses").click()

    expect(page.locator("[data-selection-cleared]")).to_be_visible()
    expect(_box(page, ROWS[0])).not_to_be_checked()


def test_a_change_in_a_second_window_is_shown_instead_of_a_conflicting_click(
    ip_tab: Page, netbox_url: str, ip_device: dict
):
    page = ip_tab
    second = page.context.new_page()
    second.goto(_tab_url(netbox_url, ip_device["device"]))
    _box(second, ROWS[1]).click()
    second.evaluate("librenmsSyncDraft.flush()")

    _box(page, ROWS[2]).click()

    expect(page.locator('[data-draft-notice="conflict"]')).to_be_visible()
    expect(_box(page, ROWS[1])).to_be_checked()
    expect(_box(page, ROWS[2])).not_to_be_checked()
    second.close()


def test_back_after_a_selection_shows_the_draft_not_a_stored_page(ip_tab: Page):
    page = ip_tab
    # A snapshot that htmx stored before the page owned its history must never come back.
    page.evaluate(
        """() => sessionStorage.setItem('htmx-history-cache', JSON.stringify([
            {url: location.pathname + location.search, content: '<p id="stale">stale</p>', title: '', scroll: 0},
        ]))"""
    )
    _box(page, ROWS[0]).click()
    _page_link(page, 2).click()
    expect(_row(page, ROWS[PER_PAGE])).to_have_count(1)

    page.go_back()

    expect(_box(page, ROWS[0])).to_be_checked()
    expect(page.locator("#stale")).to_have_count(0)


def test_a_filter_on_another_tab_survives_back_and_reload(ip_tab: Page):
    page = ip_tab
    page.locator("#interfaces-tab").click()
    page.wait_for_url(re.compile(r"tab=interfaces"))
    with page.expect_response(lambda response: "/interface-sync/" in response.url, timeout=60_000):
        page.get_by_role("button", name="Refresh Interfaces").click()
    expect(page.locator("#id_interfaces_q")).to_be_visible(timeout=60_000)
    page.locator("#id_interfaces_q").fill("Gi0")
    page.wait_for_url(re.compile(r"interfaces_q=Gi0"))
    page.locator("#ipaddresses-tab").click()
    page.wait_for_url(re.compile(r"tab=ipaddresses"))

    page.go_back()
    page.wait_for_url(re.compile(r"tab=interfaces.*interfaces_q=Gi0|interfaces_q=Gi0.*tab=interfaces"))
    expect(page.locator("#id_interfaces_q")).to_have_value("Gi0")
    page.reload()
    expect(page.locator("#id_interfaces_q")).to_have_value("Gi0")
