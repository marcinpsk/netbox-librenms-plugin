"""End-to-end coverage for per-row VLAN synchronization."""

from netbox_librenms_plugin.assessment.vlans import compare_rows

import json
from html.parser import HTMLParser

import pytest

from netbox_librenms_plugin.tests.vlan_intent_helpers import (
    assessed_tokens,
    message_texts,
    reassess,
    row_tokens,
    sync,
    tab_tokens,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping


class _VLANControlParser(HTMLParser):
    """Collect the VLAN controls that a rendered table would submit."""

    def __init__(self):
        super().__init__()
        self.selected_vids = []
        self.group_fields = {}
        self._select_name = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "select":
            self.selected_vids.append(attributes.get("value"))
        elif tag == "select" and attributes.get("name", "").startswith("vlan_group_"):
            self._select_name = attributes["name"]
        elif tag == "option" and self._select_name and "selected" in attributes:
            self.group_fields[self._select_name] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select_name = None


def _sync_url(device):
    """Return the public VLAN synchronization endpoint for a device."""
    from django.urls import reverse

    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )


def _seed_vlan_snapshot(device, rows, server_key="default"):
    """Store a VLAN source snapshot holding *rows*."""
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

    seed_source_snapshot(device, Resource.VLANS, server_key, vlans=rows)


@pytest.mark.django_db
@pytest.mark.parametrize("source_vid", [501, "501", " 501 ", "0501"], ids=["int", "string", "padded", "leading-zero"])
def test_usable_vlan_vid_spellings_round_trip_through_rendered_controls(client, settings, source_vid):
    """Every accepted source spelling must offer one canonical grouped VLAN identity."""
    from django.urls import reverse
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    suffix = str(source_vid).strip().replace(" ", "-") or "empty"
    device = seed_mapping(make_device(f"vid-round-trip-{suffix}"), "default", own=42)
    group = VLANGroup.objects.create(name=f"VID group {suffix}", slug=f"vid-group-{suffix}")
    existing = VLAN.objects.create(vid=501, group=group, name="Application")
    _seed_vlan_snapshot(device, [{"vlan_vlan": source_vid, "vlan_name": existing.name}])
    client.force_login(make_superuser(f"vid-round-trip-{suffix}-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "vlans"},
    )

    html = client.get(f"{url}?server_key=default").content.decode()

    assert 'data-vlan-id="501"' in html
    assert 'name="vlan_group_501"' in html
    assert "Synced" in html
    parser = _VLANControlParser()
    parser.feed(html)
    assert parser.selected_vids == ["501"]
    assert parser.group_fields == {"vlan_group_501": str(group.pk)}
    verify_response = reassess(client, device, source_vid, group)
    response = sync(client, device, [row_tokens(html)[501]], htmx=False)

    assert verify_response.status_code == 200
    assert verify_response.json()["exists_in_netbox"] is True
    assert verify_response.json()["name_matches"] is True
    assert "Synced" in verify_response.json()["status_html"]
    assert response.status_code == 302
    assert "VLANs synced: 1 unchanged." in message_texts(response)
    assert VLAN.objects.filter(vid=501).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("source_vid", "displayed_vid"),
    [
        (None, "Missing"),
        ("", "Empty"),
        ("abc", "abc"),
        (0, "0"),
        (5000, "5000"),
        (501.0, "501.0"),
        (True, "True"),
        (
            """<script data-test="vid">alert(1)</script>""",
            "&lt;script data-test=&quot;vid&quot;&gt;alert(1)&lt;/script&gt;",
        ),
    ],
    ids=["missing", "empty", "text", "zero", "too-large", "float", "boolean", "html"],
)
def test_invalid_vlan_vid_rows_render_as_inert_escaped_diagnostics(source_vid, displayed_vid):
    """An unusable VID must stay visible without exposing any row control or identity."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from ipam.models import VLAN

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    VLAN.objects.create(vid=1, name="Boolean alias guard")
    VLAN.objects.create(vid=501, name="Float alias guard")
    comparison_view = DeviceVLANTableView()
    compared_rows = compare_rows(
        [
            {
                "vlan_vlan": source_vid,
                "vlan_name": "Invalid source row",
                "vlan_type": "ethernet",
                "vlan_state": "active",
            }
        ],
        lookup_maps=comparison_view._build_vlan_lookup_maps([]),
        subject=None,
    )
    table = LibreNMSVLANTable(compared_rows)
    request = RequestFactory().get("/")
    request.user = AnonymousUser()

    html = table.as_html(request)

    assert displayed_vid in html
    assert "Invalid VID" in html
    assert "Invalid source row" in html
    assert "ethernet" in html
    assert "Active" in html
    assert "<script" not in html
    assert "data-vlan-id" not in html
    assert 'name="select"' not in html
    assert 'name="vlan_group_' not in html
    assert 'name="sync_one"' not in html


@pytest.mark.django_db
def test_duplicate_canonical_vlan_vid_rows_are_blocked_on_render_and_post(client, settings):
    """Two source rows for one canonical VID must not expose controls or select a winner."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from ipam.models import VLAN

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("duplicate-canonical-vid"), "default", own=42)
    source_rows = [
        {"vlan_vlan": "501", "vlan_name": "First duplicate"},
        {"vlan_vlan": "0501", "vlan_name": "Second duplicate"},
    ]
    comparison_view = DeviceVLANTableView()
    compared_rows = compare_rows(source_rows, lookup_maps=comparison_view._build_vlan_lookup_maps([]), subject=None)
    table = LibreNMSVLANTable(compared_rows)
    request = RequestFactory().get("/")
    request.user = AnonymousUser()

    html = table.as_html(request)

    assert "First duplicate" in html
    assert "Second duplicate" in html
    assert html.count("Duplicate VID") == 2
    assert "data-vlan-id" not in html
    assert 'name="select"' not in html
    assert 'name="vlan_group_' not in html
    assert 'name="sync_one"' not in html

    client.force_login(make_superuser("duplicate-canonical-vid-user"))
    _seed_vlan_snapshot(device, source_rows[:1])
    token = tab_tokens(client, device)[501]
    _seed_vlan_snapshot(device, source_rows)

    assert 501 not in tab_tokens(client, device)
    response = sync(client, device, [token], htmx=False)

    assert response.status_code == 302
    assert not VLAN.objects.filter(vid=501).exists()
    assert any(text.startswith("VLAN 501: ") and "source.row" in text for text in message_texts(response))


@pytest.mark.django_db
def test_a_posted_group_field_cannot_move_a_signed_choice(client, settings):
    """The token's choice decides the scope; a row field is routing only."""
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("signed-group-choice"), "default", own=42)
    other = VLANGroup.objects.create(name="Not chosen", slug="not-chosen")
    _seed_vlan_snapshot(device, [{"vlan_vlan": "0612", "vlan_name": "Explicit global"}])
    client.force_login(make_superuser("signed-group-choice-user"))
    token = tab_tokens(client, device)[612]

    response = sync(client, device, [token], htmx=False, vlan_group_612=str(other.pk), select="612")

    assert response.status_code == 302
    assert VLAN.objects.filter(vid=612, group__isnull=True, name="Explicit global").exists()
    assert not VLAN.objects.filter(vid=612, group=other).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fields", "names_review"),
    [
        ({"sync_one": "701", "select": "701", "vlan_group_701": ""}, True),
        ({"sync_one": "701"}, True),
        ({"select": "701"}, True),
        ({}, False),
    ],
    ids=["row-and-ticked", "row-button", "ticked-rows", "nothing"],
)
def test_unsigned_selection_fields_write_nothing(client, settings, fields, names_review):
    """Without a token, neither ticked rows nor a per-row button reach the writer; a selection gets the review step."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("unsigned-selection"), "default", own=42)
    _seed_vlan_snapshot(device, [{"vlan_vlan": 701, "vlan_name": "Checked bulk row"}])
    client.force_login(make_superuser("unsigned-selection-user"))

    response = sync(client, device, [], htmx=False, **fields)

    assert response.status_code == 302
    (text,) = message_texts(response)
    assert ("Review choices" in text) is names_review
    assert not VLAN.objects.filter(vid=701).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("confirm", [False, True], ids=["table", "confirmation"])
def test_a_token_that_names_a_group_requires_vlan_group_view_permission(client, settings, confirm):
    """A signed group choice must reach the permission gate without a POST field."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device(f"group-permission-{confirm}"), "default", own=42)
    group = VLANGroup.objects.create(name=f"Group permission {confirm}", slug=f"group-permission-{confirm}")
    vlan = VLAN.objects.create(vid=101, group=group, name="Current name")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 101, "vlan_name": "Proposed name"}])
    user = make_user_with_perms(
        f"group-permission-{confirm}-user",
        [("view", Device), ("view", VLAN), ("add", VLAN), ("change", VLAN)],
    )
    token = assessed_tokens(device, user, choices={101: group.pk})[101]
    client.force_login(user)

    response = sync(client, device, [token], confirm=confirm, htmx=False)

    assert response.status_code == 302
    assert message_texts(response) == ["Missing permissions: ipam.view_vlangroup"]
    vlan.refresh_from_db()
    assert vlan.name == "Current name"


@pytest.mark.django_db
def test_per_row_create_ignores_other_ticked_rows(client, settings):
    """A per-row create synchronizes only the VLAN whose token the request carries."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("per-row-create"), "default", own=42)
    _seed_vlan_snapshot(
        device,
        [
            {"vlan_vlan": 201, "vlan_name": "Create this"},
            {"vlan_vlan": 202, "vlan_name": "Ignore tick one"},
            {"vlan_vlan": 203, "vlan_name": "Ignore tick two"},
        ],
    )
    client.force_login(make_superuser("per-row-create-user"))
    tokens = tab_tokens(client, device)

    response = sync(client, device, [tokens[201]], htmx=False, sync_one="201", select=["202", "203"])

    assert response.status_code == 302
    assert list(VLAN.objects.filter(vid__in=[201, 202, 203]).values_list("vid", "name")) == [(201, "Create this")]


@pytest.mark.django_db
def test_per_row_update_keeps_one_row_through_confirmation(client, settings):
    """A per-row rename conflict stays scoped to that row through confirmation."""
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("per-row-update"), "default", own=42)
    group = VLANGroup.objects.create(name="Per-row update", slug="per-row-update")
    selected = VLAN.objects.create(vid=301, group=group, name="Current selected")
    ticked = VLAN.objects.create(vid=302, group=group, name="Current ticked")
    _seed_vlan_snapshot(
        device,
        [
            {"vlan_vlan": 301, "vlan_name": "Updated selected"},
            {"vlan_vlan": 302, "vlan_name": "Updated ticked"},
        ],
    )
    client.force_login(make_superuser("per-row-update-user"))
    tokens = tab_tokens(client, device)

    disclosure = sync(client, device, [tokens[301]])

    assert disclosure.status_code == 200
    assert [conflict["vid"] for conflict in disclosure.context["conflicts"]] == [301]
    assert row_tokens_of_disclosure(disclosure) == [tokens[301]]
    selected.refresh_from_db()
    ticked.refresh_from_db()
    assert selected.name == "Current selected"
    assert ticked.name == "Current ticked"

    confirmation = sync(client, device, row_tokens_of_disclosure(disclosure), confirm=True)

    assert confirmation.status_code == 200
    assert confirmation.headers["HX-Redirect"]
    selected.refresh_from_db()
    ticked.refresh_from_db()
    assert selected.name == "Updated selected"
    assert ticked.name == "Current ticked"


def row_tokens_of_disclosure(response):
    """Return the tokens that the rename disclosure's checkboxes carry, in order."""
    import re

    pattern = r'type="checkbox"\s+name="confirmation_intent"\s+value="([^"]*)"'
    return re.findall(pattern, response.content.decode())


@pytest.mark.django_db
def test_vlan_table_renders_each_per_row_action_state():
    """The VLAN table renders create, update, and synchronized row states for offered operations."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from ipam.models import VLAN

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.assessment.vlans import assess
    from netbox_librenms_plugin.confirmation_intent import IntentContext
    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    VLAN.objects.create(vid=402, name="Current update")
    VLAN.objects.create(vid=403, name="Already synced")
    device = make_device("per-row-action-states")
    _seed_vlan_snapshot(
        device,
        [
            {"vlan_vlan": 401, "vlan_name": "Create row"},
            {"vlan_vlan": 402, "vlan_name": "Updated name"},
            {"vlan_vlan": 403, "vlan_name": "Already synced"},
        ],
    )
    snapshot = source_cache.read(device, Resource.VLANS, "default")
    lookup_maps = DeviceVLANTableView()._build_vlan_lookup_maps([])
    context = IntentContext(make_superuser().pk, ("dcim.device", device.pk), "default")
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    table = LibreNMSVLANTable(assess(snapshot, subject=device, lookup_maps=lookup_maps, context=context).rows)
    unoffered = LibreNMSVLANTable(assess(snapshot, subject=device, lookup_maps=lookup_maps, context=None).rows)

    html = table.as_html(request)

    assert 'name="sync_one" value="401"' in html
    assert 'title="Create this VLAN in NetBox"' in html
    assert "mdi-plus-thick" in html
    assert "> Create</button>" in html
    assert 'name="sync_one" value="402"' in html
    assert 'title="Update this VLAN\'s name in NetBox"' in html
    assert "mdi-pencil" in html
    assert "> Update</button>" in html
    assert "mdi-check-circle" in html
    assert "Synced" in html

    table.migrated_to_marker = True
    migrated_html = table.as_html(request)

    assert 'name="sync_one"' not in migrated_html
    assert "Not in NetBox" in migrated_html
    assert "Name differs" in migrated_html
    assert "Synced" in migrated_html
    # A row control maps an offered operation; a row without an offer has none.
    assert 'name="sync_one"' not in unoffered.as_html(request)


@pytest.mark.django_db
@pytest.mark.parametrize("vid", [None, ""], ids=["none", "empty-string"])
def test_vlan_table_row_without_usable_vid_has_no_action(vid):
    """A row without a usable VID renders status text instead of a submit button."""
    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    comparison_view = DeviceVLANTableView()
    compared_row = compare_rows(
        [{"vlan_vlan": vid, "vlan_name": "Missing identity"}],
        lookup_maps=comparison_view._build_vlan_lookup_maps([]),
        subject=None,
    )[0]
    table = LibreNMSVLANTable([compared_row])

    vid_html = str(table.render_vlan_id(None, compared_row))
    status_html = str(table.render_status(None, compared_row))

    assert 'name="sync_one"' not in status_html
    assert "Invalid VID" in vid_html


@pytest.mark.django_db
def test_vlan_status_action_escapes_the_reported_vid():
    """The table escapes an unusable reported VID and does not render a submit button."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    DeviceVLANTableView()
    compared_rows = compare_rows(
        [{"vlan_vlan": """"><script>alert(1)</script>""", "vlan_name": "Unsafe identity"}],
        lookup_maps={"vid_to_groups": {}, "vid_to_vlans": {}},
        subject=None,
    )
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    html = LibreNMSVLANTable(compared_rows).as_html(request)

    assert "<script>" not in html
    assert "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Invalid VID" in html
    assert """name="sync_one""" not in html


@pytest.mark.parametrize(
    ("exists_in_netbox", "name_matches", "expected_action", "unexpected_action"),
    [
        (False, False, "Create", "Synced"),
        (True, False, "Update", "Synced"),
        (True, True, "Synced", "Create"),
    ],
)
def test_vlan_status_action_uses_data_facts_when_css_class_is_widened(
    monkeypatch,
    exists_in_netbox,
    name_matches,
    expected_action,
    unexpected_action,
):
    """Additional presentation classes must not change the row action."""
    from netbox_librenms_plugin import utils

    get_css_class = utils.get_vlan_sync_css_class
    monkeypatch.setattr(
        utils,
        "get_vlan_sync_css_class",
        lambda exists, matches: f"{get_css_class(exists, matches)} fw-bold",
    )

    html = str(utils.render_vlan_sync_action(601, exists_in_netbox, name_matches))

    assert expected_action in html
    assert unexpected_action not in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("sync_actions", "expected_actions"),
    [
        pytest.param(True, True, id="actions-enabled"),
        pytest.param(False, False, id="actions-disabled"),
        pytest.param(None, True, id="actions-default-enabled"),
        pytest.param(0, True, id="zero-default-enabled"),
        pytest.param("", True, id="empty-string-default-enabled"),
        pytest.param([], True, id="list-default-enabled"),
        pytest.param({}, True, id="object-default-enabled"),
        pytest.param("false", True, id="string-default-enabled"),
    ],
)
def test_verify_vlan_group_returns_the_shared_status_cell(client, settings, sync_actions, expected_actions):
    """The verify endpoint re-renders status through the shared server-side renderer."""
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
    from netbox_librenms_plugin.utils import render_vlan_sync_action

    configure_default_librenms_server(settings)
    tag = str(sync_actions).lower().replace("[]", "list").replace("{}", "object") or "empty"
    device = seed_mapping(make_device(f"verify-per-row-{tag}"), "default", own=42)
    group = VLANGroup.objects.create(name=f"Verify per-row {tag}", slug=f"verify-per-row-{tag}")
    VLAN.objects.create(vid=501, group=group, name="Current name")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 501, "vlan_name": "Updated name"}])
    client.force_login(make_superuser(f"verify-per-row-{tag}"))

    fields = {} if sync_actions is None else {"sync_actions": sync_actions}
    response = reassess(client, device, "501", group, **fields)

    assert response.status_code == 200
    assert response.json()["vlan_group_id"] == group.pk
    assert response.json()["confirmation_intent"].startswith("vlan:")
    assert response.json()["status_html"] == str(
        render_vlan_sync_action(501, True, False, actions_enabled=expected_actions)
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("vid", "message"),
    [
        (None, "No VID provided"),
        ("", "No VID provided"),
        (" ", "Invalid VID"),
        ("abc", "Invalid VID"),
        (0, "Invalid VID"),
        (5000, "Invalid VID"),
        (501.0, "Invalid VID"),
        (True, "Invalid VID"),
    ],
    ids=["missing", "empty", "whitespace", "text", "zero", "too-large", "float", "boolean"],
)
def test_verify_vlan_group_rejects_every_unusable_vlan_identity(client, vid, message):
    """The verify endpoint must apply the same VLAN identity boundary as render and write paths."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_superuser

    client.force_login(make_superuser(f"verify-invalid-vid-{str(vid).replace(' ', 'space')}"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_vlan_sync_group"),
        data=json.dumps({"vid": vid, "name": "Unusable", "vlan_group_id": ""}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": message}


@pytest.mark.django_db
def test_a_forged_token_still_reports_the_batch_summary(client, settings):
    """A request whose every token is refused keeps its skip summary."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("forged-only-summary"), "default", own=77)
    _seed_vlan_snapshot(device, [{"vlan_vlan": 10, "vlan_name": "Users"}])
    client.force_login(make_superuser("forged-only-summary-user"))

    response = sync(client, device, ["vlan:forged"], htmx=False)

    assert response.status_code == 302
    assert message_texts(response) == [
        "VLAN confirmation is invalid or has expired. Refresh the VLAN data and try again.",
        "No VLANs synced: 1 skipped (confirmation no longer current).",
    ]
    assert not VLAN.objects.filter(vid=10).exists()


@pytest.mark.django_db
def test_an_oversized_token_is_not_echoed(client, settings):
    """An oversized POST value must be refused without echoing it."""
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("unbounded-token-diagnostic"), "default", own=78)
    _seed_vlan_snapshot(device, [{"vlan_vlan": 3201, "vlan_name": "Valid VLAN"}])
    client.force_login(make_superuser("unbounded-token-diagnostic-user"))
    oversized = "vlan:" + "x" * 9000

    response = sync(client, device, [oversized], htmx=False)

    assert response.status_code == 302
    diagnostics = message_texts(response)
    assert diagnostics
    assert all(len(message) < 200 for message in diagnostics)
    assert not any("xxxx" in message for message in diagnostics)


@pytest.mark.django_db
def test_the_row_compares_and_offers_the_scope_its_dropdown_selects(client, settings):
    """The status, the dropdown and the token of a row name one scope, also when two groups tie."""
    from django.core import signing
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.confirmation_intent import SALT_NAMESPACE
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("row-scope-agreement"), "default", own=42)
    first = VLANGroup.objects.create(name="Tie first", slug="tie-first")
    second = VLANGroup.objects.create(name="Tie second", slug="tie-second")
    VLAN.objects.create(vid=801, group=first, name="first", status="active")
    VLAN.objects.create(vid=801, group=second, name="second", status="active")
    global_vlan = VLAN.objects.create(vid=801, name="global name", status="active")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 801, "vlan_name": "users"}])
    client.force_login(make_superuser("row-scope-agreement-user"))

    token = tab_tokens(client, device)[801]
    operation = signing.loads(token.partition(":")[2], salt=f"{SALT_NAMESPACE}:vlan")["operation"]

    # Two groups tie, so the dropdown selects global, and the row compares the global VLAN.
    assert operation["choices"] == {"group": None}
    assert operation["assumptions"]["target"]["pk"] == global_vlan.pk
    assert operation["effects"] == {"vlan": global_vlan.pk, "name": "users"}
