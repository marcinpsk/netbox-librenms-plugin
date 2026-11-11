"""Browser checks for the Link controls of a status-list page that htmx swaps in after the first page."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

PLUGIN_DIR = Path(__file__).parents[2]
HTMX_PATH = Path(__file__).parent / "vendor" / "htmx.min.js"
LIST_URL = "https://plugin.example.com/plugins/librenms_plugin/device-status/?role=7"
PAGE_TWO = "https://plugin.example.com/plugins/librenms_plugin/device-status/?role=7&page=2"
LINK_URL = "https://plugin.example.com/plugins/librenms_plugin/device/link-selected/"
TOKEN = "mapping_lifecycle:signed-row-token"


def _render_link_status():
    """Load the production row renderer from its file: it imports no NetBox table, unlike its package."""
    spec = importlib.util.spec_from_file_location("link_status", PLUGIN_DIR / "tables" / "link_status.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_link_status


def _forms():
    """Render the production link forms as the first page renders them."""
    from django.template import Context, Engine

    engine = Engine(dirs=[str(PLUGIN_DIR / "templates")])
    context = {"link_selected_url": LINK_URL, "link_server_key": "primary", "csrf_token": "browser-token"}
    return engine.get_template("netbox_librenms_plugin/inc/_link_selected_forms.html").render(
        Context(context, use_l10n=False)
    )


def _table(cell, *, next_page=False):
    pager = f'<a id="next-page" href="#" hx-get="{PAGE_TWO}" hx-target="closest .htmx-container" hx-swap="outerHTML">Next</a>'
    return (
        f'<div class="htmx-container" id="object_list"><table><tbody><tr><td>{cell}</td></tr></tbody></table>'
        f"{pager if next_page else ''}</div>"
    )


def _match_cell():
    row = SimpleNamespace(
        state="match", librenms_id=9962, hostname="core-1", matched_by="primary IP", owner_label=None, token=TOKEN
    )
    table = SimpleNamespace(link_rows={2: row})
    record = SimpleNamespace(pk=2, name="core-1")
    return str(_render_link_status()(table, None, record, "/device/2/librenms-sync/"))


def _serve(page, submissions):
    def handle_route(route):
        url = route.request.url
        if url == LIST_URL:
            linked = str(_render_link_status()(SimpleNamespace(link_rows={}), "linked", SimpleNamespace(pk=1), "/x/"))
            body = f"<html><body>{_forms()}{_table(linked, next_page=True)}</body></html>"
            route.fulfill(body=body, content_type="text/html")
        elif url == PAGE_TWO:
            route.fulfill(body=_table(_match_cell()), content_type="text/html")
        elif url == LINK_URL:
            submissions.append(parse_qs(route.request.post_data or "", keep_blank_values=True))
            route.fulfill(body="<p id='linked'>linked</p>", content_type="text/html")
        else:
            raise AssertionError(f"Unexpected browser request: {url}")

    page.route("**/*", handle_route)
    page.goto(LIST_URL)
    page.add_script_tag(path=str(HTMX_PATH))
    page.evaluate("htmx.process(document.body)")


def _page_two(page):
    assert page.locator("#librenms-link-actions").is_hidden()
    page.locator("#next-page").click()
    page.locator('[data-link-state="match"]').wait_for()


def test_the_link_button_of_a_swapped_in_page_posts_its_token(page):
    submissions = []
    _serve(page, submissions)
    _page_two(page)

    page.get_by_role("button", name="Link", exact=True).click()
    page.locator("#linked").wait_for()

    assert submissions == [
        {"csrfmiddlewaretoken": ["browser-token"], "server_key": ["primary"], "confirmation_intent": [TOKEN]}
    ]


def test_link_selected_appears_with_the_swapped_in_rows_and_posts_the_selected_tokens(page):
    submissions = []
    _serve(page, submissions)
    _page_two(page)

    page.get_by_label("Select: Link core-1 to LibreNMS ID 9962").check()
    page.get_by_role("button", name="Link selected").click()
    page.locator("#linked").wait_for()

    assert submissions == [
        {"csrfmiddlewaretoken": ["browser-token"], "server_key": ["primary"], "confirmation_intent": [TOKEN]}
    ]
