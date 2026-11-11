"""Browser check for the unlink confirmation: one plain form posts the offered token without JavaScript."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

TEMPLATES_DIR = Path(__file__).parents[2] / "templates"
ACTION = "https://plugin.example.com/plugins/librenms_plugin/device/1/mapping-lifecycle/"
DESTINATION = "https://plugin.example.com/plugins/librenms_plugin/device/1/mapping-lifecycle/?action=unlink"
TOKEN = "mapping_lifecycle:signed-unlink-token"


def _render_confirmation():
    """Render the production confirmation card of an unlink that clears two interfaces of a chassis."""
    from django.template import Context, Engine

    engine = Engine(dirs=[str(TEMPLATES_DIR)])
    context = {
        "object": SimpleNamespace(name="sw1"),
        "action": "unlink",
        "server_key": "production",
        "server_name": "Production",
        "before": SimpleNamespace(own_id=101, oob_id=205, oob_type="idrac"),
        "after": SimpleNamespace(own_id=None, oob_id=None, oob_type=None),
        "interfaces": [{"name": "ge-0/0/1", "parent": "sw1"}, {"name": "ge-1/0/1", "parent": "sw2"}],
        "members": ["sw1", "sw2"],
        "token": TOKEN,
        "action_url": ACTION,
        "cancel_url": "https://plugin.example.com/device/1/librenms-sync/",
        "tab": "interfaces",
        "csrf_token": "browser-token",
    }
    card = engine.get_template("netbox_librenms_plugin/inc/_host_change_confirmation.html").render(
        Context(context, use_l10n=False)
    )
    return f"<html><body>{card}</body></html>"


def test_unlink_posts_the_offered_token_without_javascript(page):
    submissions = []

    def handle_route(route):
        if route.request.method == "POST" and route.request.url == ACTION:
            submissions.append(parse_qs(route.request.post_data or "", keep_blank_values=True))
            route.fulfill(body="<p id='unlinked'>unlinked</p>", content_type="text/html")
            return
        if route.request.url == DESTINATION:
            route.fulfill(body=_render_confirmation(), content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.route("**/*", handle_route)
    page.goto(DESTINATION)
    assert page.locator("script").count() == 0
    assert page.locator("#librenms-host-change-interfaces tbody tr").count() == 2

    page.get_by_role("button", name="Unlink").click()
    page.locator("#unlinked").wait_for()

    assert submissions == [
        {
            "csrfmiddlewaretoken": ["browser-token"],
            "server_key": ["production"],
            "tab": ["interfaces"],
            "confirmation_intent": [TOKEN],
        }
    ]
