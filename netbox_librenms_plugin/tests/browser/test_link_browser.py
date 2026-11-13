"""Browser checks for the Link control that the sync page shows for a proposed match."""

from pathlib import Path
from urllib.parse import parse_qs

TEMPLATES_DIR = Path(__file__).parents[2] / "templates"
ACTION = "https://plugin.example.com/plugins/librenms_plugin/device/1/mapping-lifecycle/"
DESTINATION = "https://plugin.example.com/device/1/librenms-sync/"


def _render_match(state="match", token="mapping_lifecycle:signed-token"):
    """Render the production match rows for one proposal."""
    from django.template import Context, Engine

    engine = Engine(dirs=[str(TEMPLATES_DIR)])
    link = {
        "state": state,
        "server_key": "primary",
        "server_name": "Primary",
        "librenms_id": 42,
        "hostname": "core-1.example.net",
        "sysname": "core-1",
        "matched_by": "primary IP",
        "owner_label": None,
        "token": token,
        "action_url": ACTION,
    }
    rows = engine.get_template("netbox_librenms_plugin/inc/_link_match_rows.html").render(
        Context({"link": link, "active_sync_tab": "cables", "csrf_token": "browser-token"}, use_l10n=False)
    )
    return f"<html><body><table><tbody>{rows}</tbody></table></body></html>"


def test_link_posts_the_offered_token_without_javascript(page):
    """A plain form submit carries the token, the server of the match and the tab to return to."""
    submissions = []

    def handle_route(route):
        if route.request.url == ACTION:
            submissions.append(parse_qs(route.request.post_data or "", keep_blank_values=True))
            route.fulfill(body="<p id='linked'>linked</p>", content_type="text/html")
            return
        if route.request.url == DESTINATION:
            route.fulfill(body=_render_match(), content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.route("**/*", handle_route)
    page.goto(DESTINATION)
    assert page.locator("script").count() == 0

    page.get_by_role("button", name="Link").click()
    page.locator("#linked").wait_for()

    assert submissions == [
        {
            "csrfmiddlewaretoken": ["browser-token"],
            "server_key": ["primary"],
            "tab": ["cables"],
            "confirmation_intent": ["mapping_lifecycle:signed-token"],
        }
    ]


def test_a_match_without_a_token_offers_no_link(page):
    page.set_content(_render_match(state="owned", token=None))

    assert page.get_by_role("button", name="Link").count() == 0
    assert page.locator("form").count() == 0
