"""Browser checks for the server mapping editor on the sync page."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

TEMPLATES_DIR = Path(__file__).parents[2] / "templates"
STYLE_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "css" / "librenms_sync.css"
ACTION = "https://plugin.example.com/devices/1/edit-server-mapping/"
DESTINATION = "https://plugin.example.com/device/1/librenms-sync/"
PHONE_WIDTH = 390
LONG_SERVER_NAME = "Default Server (https://librenms-with-a-long-host-name.datacenter.example.net)"
# The NetBox rules that decide how far the editor can shrink.
NETBOX_PAGE_CSS = """
*, ::before, ::after { box-sizing: border-box; }
body { margin: 0; font: 14px/1.4 sans-serif; }
.page-body { padding: 0 16px; }
.card { display: flex; flex-direction: column; min-width: 0; border: 1px solid #ccc; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { padding: .25rem .5rem; }
.ps-3 { padding-left: 1rem !important; }
.pe-3 { padding-right: 1rem !important; }
.text-end { text-align: right !important; }
.text-nowrap { white-space: nowrap !important; }
.form-control, .form-select { display: block; width: 100%; padding: .25rem .5rem; font: inherit; }
.btn { display: inline-flex; padding: .25rem .5rem; white-space: nowrap; }
"""


def _render_editor(display_name="Secondary"):
    """Render the production editor include for one mapped and one unmapped server."""
    from django.template import Context, Engine, Library

    stubs = Library()
    stubs.simple_tag(lambda *args, **kwargs: ACTION, name="url")
    engine = Engine(dirs=[str(TEMPLATES_DIR)])
    engine.template_builtins.append(stubs)
    rows = [
        {"server_key": "primary", "display_name": "Primary", "own_id": 42, "oob_id": 43, "oob_type": "idrac"},
        {"server_key": "secondary", "display_name": display_name, "own_id": None, "oob_id": None, "oob_type": None},
    ]
    context = {
        "mapping_editor": {
            "blocked": False,
            "rows": rows,
            "oob_types": ("idrac", "ilo", "oob"),
            "owner_pk": 1,
            "object_type": "device",
        },
        "server_key": "primary",
        "active_sync_tab": "modules",
        "csrf_token": "browser-token",
    }
    return engine.get_template("netbox_librenms_plugin/inc/_server_mapping_editor.html").render(
        Context(context, use_l10n=False)
    )


def test_each_row_posts_only_its_own_fields_without_javascript(page):
    """The inputs join their row's form through the form attribute, so a plain submit posts that row."""
    submissions = []

    def handle_route(route):
        if route.request.url == ACTION:
            submissions.append(parse_qs(route.request.post_data or "", keep_blank_values=True))
            route.fulfill(body="<p id='saved'>saved</p>", content_type="text/html")
            return
        if route.request.url == DESTINATION:
            route.fulfill(body=f"<html><body>{_render_editor()}</body></html>", content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.route("**/*", handle_route)
    page.goto(DESTINATION)
    assert page.locator("script").count() == 0

    page.locator("#librenms-mapping-editor summary").click()
    page.get_by_label("LibreNMS ID on Secondary").fill("77")
    page.get_by_label("OOB controller ID on Secondary").fill("78")
    page.get_by_label("OOB type on Secondary").select_option("ilo")
    page.get_by_label("Save the mapping on Secondary").click()
    page.locator("#saved").wait_for()

    assert submissions == [
        {
            "csrfmiddlewaretoken": ["browser-token"],
            "own_id": ["77"],
            "oob_id": ["78"],
            "oob_type": ["ilo"],
            "server_key": ["secondary"],
            "object_type": ["device"],
            "active_server_key": ["primary"],
            "tab": ["modules"],
            "shown_own_id": [""],
            "shown_oob_id": [""],
            "shown_oob_type": [""],
        }
    ]


def test_a_long_server_name_fits_a_phone_width_page(page):
    """Without a scroll wrapper the editor must wrap a long server name, or the page scrolls sideways."""
    page.set_viewport_size({"width": PHONE_WIDTH, "height": 900})
    page.set_content(
        f"<style>{NETBOX_PAGE_CSS}</style><div class='page-body'><div class='card'>"
        f"{_render_editor(LONG_SERVER_NAME)}</div></div>"
    )
    page.add_style_tag(path=str(STYLE_PATH))
    page.locator("#librenms-mapping-editor summary").click()

    state = page.evaluate(
        """() => ({
            scrollWidth: document.documentElement.scrollWidth,
            innerWidth: window.innerWidth,
            editorRight: document.querySelector('#librenms-mapping-editor').getBoundingClientRect().right,
        })"""
    )

    assert page.get_by_text(LONG_SERVER_NAME).is_visible()
    assert state["scrollWidth"] <= state["innerWidth"], state
    assert state["editorRight"] <= state["innerWidth"], state


CONFIRM_ACTION = "https://plugin.example.com/device/1/mapping-lifecycle/"
CONFIRM_DESTINATION = "https://plugin.example.com/device/1/edit-server-mapping/"
CONFIRM_TOKEN = "mapping_lifecycle:signed-host-change-token"


def _render_host_confirmation():
    """Render the production confirmation card of a relink that clears two chassis interfaces."""
    from django.template import Context, Engine

    engine = Engine(dirs=[str(TEMPLATES_DIR)])
    context = {
        "object": SimpleNamespace(name="sw1"),
        "action": "relink",
        "server_key": "production",
        "server_name": "Production",
        "before": SimpleNamespace(own_id=101, oob_id=205, oob_type="idrac"),
        "after": SimpleNamespace(own_id=102, oob_id=205, oob_type="idrac"),
        "interfaces": [{"name": "ge-0/0/1", "parent": "sw1"}, {"name": "ge-1/0/1", "parent": "sw2"}],
        "members": ["sw1", "sw2"],
        "token": CONFIRM_TOKEN,
        "action_url": CONFIRM_ACTION,
        "cancel_url": DESTINATION,
        "tab": "interfaces",
        "csrf_token": "browser-token",
    }
    card = engine.get_template("netbox_librenms_plugin/inc/_host_change_confirmation.html").render(
        Context(context, use_l10n=False)
    )
    return f"<html><body>{card}</body></html>"


def test_a_host_change_posts_the_offered_token_without_javascript(page):
    submissions = []

    def handle_route(route):
        if route.request.method == "POST" and route.request.url == CONFIRM_ACTION:
            submissions.append(parse_qs(route.request.post_data or "", keep_blank_values=True))
            route.fulfill(body="<p id='changed'>changed</p>", content_type="text/html")
            return
        if route.request.url == CONFIRM_DESTINATION:
            route.fulfill(body=_render_host_confirmation(), content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.route("**/*", handle_route)
    page.goto(CONFIRM_DESTINATION)
    assert page.locator("script").count() == 0
    assert page.locator("#librenms-host-change-interfaces tbody tr").count() == 2
    page.get_by_role("button", name="Confirm the change").click()
    page.locator("#changed").wait_for()

    assert submissions == [
        {
            "csrfmiddlewaretoken": ["browser-token"],
            "server_key": ["production"],
            "tab": ["interfaces"],
            "confirmation_intent": [CONFIRM_TOKEN],
        }
    ]
