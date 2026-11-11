"""Browser checks for the server mapping editor on the sync page."""

from pathlib import Path
from urllib.parse import parse_qs

TEMPLATES_DIR = Path(__file__).parents[2] / "templates"
ACTION = "https://plugin.example.com/devices/1/edit-server-mapping/"
DESTINATION = "https://plugin.example.com/device/1/librenms-sync/"


def _render_editor():
    """Render the production editor include for one mapped and one unmapped server."""
    from django.template import Context, Engine, Library

    stubs = Library()
    stubs.simple_tag(lambda *args, **kwargs: ACTION, name="url")
    engine = Engine(dirs=[str(TEMPLATES_DIR)])
    engine.template_builtins.append(stubs)
    rows = [
        {"server_key": "primary", "display_name": "Primary", "own_id": 42, "oob_id": 43, "oob_type": "idrac"},
        {"server_key": "secondary", "display_name": "Secondary", "own_id": None, "oob_id": None, "oob_type": None},
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
