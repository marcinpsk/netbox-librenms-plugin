"""End-to-end coverage for per-row VLAN synchronization, and for the VLANs tab on the selection draft."""

import json
import re
from html import unescape
from html.parser import HTMLParser

import pytest
from dcim.models import Device, Site
from django.urls import reverse
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin.assessment.vlans import compare_rows
from netbox_librenms_plugin.selection_draft import read_evidence
from netbox_librenms_plugin.tests._html_helpers import open_tags
from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
from netbox_librenms_plugin.tests.view_test_helpers import (
    answers_in_place,
    draft_command,
    draft_tab,
    grant,
    make_user_with_perms,
    row_action_fields,
    select_rows,
    submit_with_draft,
)
from netbox_librenms_plugin.views.base.vlan_table_view import VLAN_TAB


class _VLANControlParser(HTMLParser):
    """Collect the VLAN controls that a rendered table shows: the checkboxes and the group each row shows."""

    def __init__(self):
        super().__init__()
        self.selected_vids = []
        self.group_fields = {}
        self._select_vid = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "select":
            self.selected_vids.append(attributes.get("value"))
        elif tag == "select" and attributes.get("data-draft-choice") == "group":
            self._select_vid = attributes["data-vlan-id"]
        elif tag == "option" and self._select_vid and "selected" in attributes:
            self.group_fields[self._select_vid] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select_vid = None


def _sync_url(device):
    """Return the public VLAN synchronization endpoint for a device."""

    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )


def _seed_vlan_snapshot(device, rows, server_key="default"):
    """Store a VLAN source snapshot holding *rows*."""
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

    seed_source_snapshot(device, Resource.VLANS, server_key, vlans=rows)


def _post(client, device, fields, **headers):
    """POST one VLAN sync request with *fields*."""
    return client.post(_sync_url(device), {"server_key": "default", "action": "create_vlans", **fields}, **headers)


def _messages(response):
    return [str(message) for message in response.wsgi_request._messages]


def _status_cell(row_html):
    """Return the inner markup of the status cell of one rendered VLAN row."""
    import re

    return re.search(r'<td[^>]*data-col="status"[^>]*>(.*?)</td>', row_html, re.S).group(1).strip()


def _checked(shown, vid):
    """Return whether the rendered tab *shown* checks the row of *vid*."""

    boxes = [attrs for attrs in open_tags(shown.html, "input") if attrs.get("name") == "select"]
    return any(attrs.get("value") == str(vid) and "checked" in attrs for attrs in boxes)


@pytest.mark.django_db
@pytest.mark.parametrize("source_vid", [501, "501", " 501 ", "0501"], ids=["int", "string", "padded", "leading-zero"])
def test_usable_vlan_vid_spellings_round_trip_through_rendered_controls(client, settings, source_vid):
    """Every accepted source spelling must select and write one canonical grouped VLAN identity."""
    from html import unescape

    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.selection_draft import read_evidence
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import draft_tab, submit_with_draft
    from netbox_librenms_plugin.views.base.vlan_table_view import VLAN_TAB

    configure_default_librenms_server(settings)
    suffix = str(source_vid).strip().replace(" ", "-") or "empty"
    device = make_device(f"vid-round-trip-{suffix}", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name=f"VID group {suffix}", slug=f"vid-group-{suffix}")
    existing = VLAN.objects.create(vid=501, group=group, name="Application")
    _seed_vlan_snapshot(device, [{"vlan_vlan": source_vid, "vlan_name": existing.name}])
    client.force_login(make_superuser(f"vid-round-trip-{suffix}-user"))

    shown = draft_tab(client, device, "vlans")

    assert 'data-vlan-id="501"' in shown.html
    assert list(shown.rows) == ["501"]
    parser = _VLANControlParser()
    parser.feed(shown.html)
    assert parser.selected_vids == ["501"]
    assert parser.group_fields == {"501": str(group.pk)}
    evidence = read_evidence(VLAN_TAB, shown.rows["501"]["data-evidence"])
    assert (evidence["group"], evidence["vlan"]) == (group.pk, existing.pk)
    row_read = client.get(unescape(shown.rows["501"]["hx-get"]))
    assert row_read.status_code == 200
    assert "Synced" in _status_cell(row_read.content.decode())

    response = submit_with_draft(client, device, "vlans", _sync_url(device), [501], action="create_vlans")

    assert response.status_code == 302
    assert VLAN.objects.filter(vid=501, group=group, pk=existing.pk).exists()
    assert not VLAN.objects.filter(vid=501, group__isnull=True).exists()
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
    assert "data-row-key" not in html
    assert 'name="select"' not in html
    assert "data-draft-choice" not in html
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
    from netbox_librenms_plugin.tests.view_test_helpers import draft_tab
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    configure_default_librenms_server(settings)
    device = make_device("duplicate-canonical-vid", librenms_cf={"default": {"id": 42}})
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
    assert "data-draft-choice" not in html
    assert 'name="sync_one"' not in html

    # A rename confirmation signed before the VID became a duplicate writes nothing.
    existing = VLAN.objects.create(vid=501, name="Current name")
    intent = SyncVLANsView()._build_conflict(
        vlan=existing, proposed_name="First duplicate", obj=device, object_type="device", server_key="default"
    )["intent"]
    _seed_vlan_snapshot(device, source_rows)
    client.force_login(make_superuser("duplicate-canonical-vid-user"))
    assert draft_tab(client, device, "vlans").rows == {}

    response = _post(client, device, {"force_all": "1", "conflict_intent": intent})

    assert response.status_code == 302
    existing.refresh_from_db()
    assert existing.name == "Current name"
    assert VLAN.objects.filter(vid=501).count() == 1
    assert any("duplicate source rows" in message.lower() for message in _messages(response))


@pytest.mark.django_db
def test_an_explicit_global_choice_writes_the_vlan_without_a_group(client, settings):
    """A row that shows a group writes to Global when the user chooses Global ("") for it."""
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import submit_with_draft

    configure_default_librenms_server(settings)
    device = make_device("explicit-global-vlan", librenms_cf={"default": {"id": 43}})
    group = VLANGroup.objects.create(name="Explicit global", slug="explicit-global")
    VLAN.objects.create(vid=612, group=group, name="Explicit global")
    _seed_vlan_snapshot(device, [{"vlan_vlan": "0612", "vlan_name": "Explicit global"}])
    client.force_login(make_superuser("canonical-group-field-user"))

    response = submit_with_draft(
        client,
        device,
        "vlans",
        _sync_url(device),
        [612],
        choices=[("data-choice-url", {"row": "612", "field": "group", "value": ""})],
        action="create_vlans",
    )

    assert response.status_code == 302
    assert VLAN.objects.filter(vid=612, group__isnull=True, name="Explicit global").exists()
    assert VLAN.objects.filter(vid=612, group=group).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("sync_one", ["", "not-a-vid"], ids=["empty", "invalid"])
def test_present_unusable_sync_one_rejects_the_request_instead_of_running_bulk_rows(client, settings, sync_one):
    """A malformed per-row action must not fall through to the selected bulk rows."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import draft_tab, row_action_fields, select_rows

    configure_default_librenms_server(settings)
    device = make_device(f"invalid-sync-one-{sync_one or 'empty'}", librenms_cf={"default": {"id": 42}})
    _seed_vlan_snapshot(device, [{"vlan_vlan": 701, "vlan_name": "Checked bulk row"}])
    client.force_login(make_superuser(f"invalid-sync-one-{sync_one or 'empty'}-user"))
    select_rows(client, device, "vlans", [701])

    response = _post(client, device, {"sync_one": sync_one, **row_action_fields(client, device, "vlans", 701)})

    assert response.status_code == 302
    assert not VLAN.objects.filter(vid=701).exists()
    # The row action leaves the draft as it is: the bulk row stays selected.
    assert _checked(draft_tab(client, device, "vlans"), 701)


def test_confirmed_vlan_vids_are_canonical_deduplicated_and_report_invalid_values():
    """Confirmation parsing must return one decimal identity and explicit errors for rejected values."""
    from django.test import RequestFactory

    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    request = RequestFactory().post(
        "/",
        {
            "confirm_conflicts": "1",
            "force_conflict": ["501", "0501", " 501 ", "5000", "not-a-vid"],
        },
    )

    selection = SyncVLANsView._confirmed_vlan_vids(request, {"501": {}})

    assert selection.vids == ["501"]
    assert selection.errors == [
        "VLAN 5000: the LibreNMS VID is invalid; skipped.",
        "VLAN not-a-vid: the LibreNMS VID is invalid; skipped.",
    ]


@pytest.mark.django_db
def test_per_row_group_selection_requires_vlan_group_view_permission(client, settings):
    """A user without the VLAN-group view permission cannot choose a group for a row."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
    from netbox_librenms_plugin.tests.view_test_helpers import draft_command, draft_tab, make_user_with_perms

    configure_default_librenms_server(settings)
    device = make_device("per-row-permission", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Per-row permission", slug="per-row-permission")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 101, "vlan_name": "Proposed name"}])
    user = make_user_with_perms(
        "per-row-permission-user",
        [("view", Device), ("add", VLAN), ("change", VLAN)],
    )
    client.force_login(user)
    shown = draft_tab(client, device, "vlans")

    response = draft_command(client, shown, "data-choice-url", row="101", field="group", value=str(group.pk))

    assert response.status_code == 404
    assert draft_tab(client, device, "vlans").table["data-draft-revision"] == shown.table["data-draft-revision"]
    assert not VLAN.objects.filter(vid=101).exists()


@pytest.mark.django_db
def test_force_all_intent_group_requires_vlan_group_view_permission(client, settings):
    """A token-only force-all request must include its VLAN group in the permission pass."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    configure_default_librenms_server(settings)
    device = make_device("force-all-permission", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Force-all permission", slug="force-all-permission")
    vlan = VLAN.objects.create(vid=101, group=group, name="Current name")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 101, "vlan_name": "Proposed name"}])
    intent = SyncVLANsView()._build_conflict(
        vlan=vlan,
        proposed_name="Proposed name",
        obj=device,
        object_type="device",
        server_key="default",
    )["intent"]
    user = make_user_with_perms(
        "force-all-permission-user",
        [("view", Device), ("add", VLAN), ("change", VLAN)],
    )
    client.force_login(user)

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "force_all": "1",
            "conflict_intent": intent,
        },
    )

    assert response.status_code == 302
    assert [str(message) for message in response.wsgi_request._messages] == ["Missing permissions: ipam.view_vlangroup"]
    vlan.refresh_from_db()
    assert vlan.name == "Current name"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("selection_branch", "expected_work", "expected_errors"),
    [
        ("confirm-conflicts", ["102"], ["VLAN not-a-vid: the LibreNMS VID is invalid; skipped."]),
        ("force-all", ["101", "102"], []),
    ],
)
def test_permission_inputs_cover_every_confirmation_work_list(selection_branch, expected_work, expected_errors):
    """The group permission check reads every signed intent, so it covers each VID that a confirmation can write."""
    from django.test import RequestFactory
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    device = make_device(f"selection-superset-{selection_branch}")
    group = VLANGroup.objects.create(
        name=f"Selection superset {device.pk}",
        slug=f"selection-superset-{device.pk}",
    )
    view = SyncVLANsView()
    conflicts = [
        view._build_conflict(
            vlan=VLAN.objects.create(vid=vid, group=group, name=f"Old {vid}"),
            proposed_name=f"New {vid}",
            obj=device,
            object_type="device",
            server_key="default",
        )
        for vid in (101, 102)
    ]
    intents = [conflict["intent"] for conflict in conflicts]
    selection_forms = {
        "confirm-conflicts": {
            "confirm_conflicts": "1",
            "force_conflict": ["102", "not-a-vid", "102"],
            "conflict_intent": intents,
        },
        "force-all": {"force_all": "1", "conflict_intent": intents},
    }
    request = RequestFactory().post("/", selection_forms[selection_branch])
    force_intents, errors = view._load_force_intents(request, device, "device", "default")

    assert errors == []
    work_list = view._confirmed_vlan_vids(request, force_intents)
    assert work_list.vids == expected_work
    assert work_list.errors == expected_errors
    assert set(work_list.vids) <= set(force_intents)


@pytest.mark.django_db
def test_per_row_create_ignores_other_ticked_rows(client, settings):
    """A per-row create synchronizes only the button's VLAN, and the other selected rows stay selected."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import draft_tab, row_action_fields, select_rows

    configure_default_librenms_server(settings)
    device = make_device("per-row-create", librenms_cf={"default": {"id": 42}})
    _seed_vlan_snapshot(
        device,
        [
            {"vlan_vlan": 201, "vlan_name": "Create this"},
            {"vlan_vlan": 202, "vlan_name": "Ignore tick one"},
            {"vlan_vlan": 203, "vlan_name": "Ignore tick two"},
        ],
    )
    client.force_login(make_superuser("per-row-create-user"))
    select_rows(client, device, "vlans", [202, 203])

    response = _post(client, device, {"sync_one": "201", **row_action_fields(client, device, "vlans", 201)})

    assert response.status_code == 302
    assert list(VLAN.objects.filter(vid__in=[201, 202, 203]).values_list("vid", "name")) == [(201, "Create this")]
    after = draft_tab(client, device, "vlans")
    assert _checked(after, 202) and _checked(after, 203)


@pytest.mark.django_db
def test_per_row_update_keeps_one_row_through_confirmation(client, settings):
    """A per-row rename conflict stays scoped to that row through confirmation."""
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import row_action_fields, select_rows

    configure_default_librenms_server(settings)
    device = make_device("per-row-update", librenms_cf={"default": {"id": 42}})
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
    select_rows(client, device, "vlans", [302])

    disclosure = _post(
        client,
        device,
        {"sync_one": "301", **row_action_fields(client, device, "vlans", 301)},
        HTTP_HX_REQUEST="true",
    )

    assert disclosure.status_code == 200
    assert [conflict["vid"] for conflict in disclosure.context["conflicts"]] == [301]
    selected.refresh_from_db()
    ticked.refresh_from_db()
    assert selected.name == "Current selected"
    assert ticked.name == "Current ticked"

    conflict = disclosure.context["conflicts"][0]
    confirmation = _post(
        client,
        device,
        {"confirm_conflicts": "1", "force_conflict": "301", "conflict_intent": conflict["intent"]},
        HTTP_HX_REQUEST="true",
    )

    assert answers_in_place(confirmation)
    selected.refresh_from_db()
    ticked.refresh_from_db()
    assert selected.name == "Updated selected"
    assert ticked.name == "Current ticked"


@pytest.mark.django_db
def test_vlan_table_renders_each_per_row_action_state():
    """The VLAN table renders create, update, and synchronized row states."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from ipam.models import VLAN

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    VLAN.objects.create(vid=402, name="Current update")
    VLAN.objects.create(vid=403, name="Already synced")
    comparison_view = DeviceVLANTableView()
    compared_rows = compare_rows(
        [
            {"vlan_vlan": 401, "vlan_name": "Create row"},
            {"vlan_vlan": 402, "vlan_name": "Updated name"},
            {"vlan_vlan": 403, "vlan_name": "Already synced"},
        ],
        lookup_maps=comparison_view._build_vlan_lookup_maps([]),
        subject=None,
    )
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    table = LibreNMSVLANTable(compared_rows)

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
def test_a_row_read_after_a_group_choice_returns_the_shared_status_cell(client, settings):
    """The row read renders the status of the chosen group through the shared server-side renderer."""
    from html import unescape

    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import draft_command, draft_tab
    from netbox_librenms_plugin.utils import render_vlan_sync_action

    configure_default_librenms_server(settings)
    device = make_device("row-read-status", librenms_cf={"default": {"id": 42}})
    current = VLANGroup.objects.create(name="Row read current", slug="row-read-current")
    empty = VLANGroup.objects.create(name="Row read empty", slug="row-read-empty")
    VLAN.objects.create(vid=501, group=current, name="Current name")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 501, "vlan_name": "Updated name"}])
    client.force_login(make_superuser("row-read-status-user"))
    shown = draft_tab(client, device, "vlans")
    row_url = unescape(shown.rows["501"]["hx-get"])
    statuses = []

    for group in (empty, current):
        response = draft_command(
            client, shown, "data-choice-url", str(len(statuses)), row="501", field="group", value=str(group.pk)
        )
        assert response.status_code == 204
        statuses.append(_status_cell(client.get(row_url).content.decode()))

    assert statuses == [
        str(render_vlan_sync_action(501, False, False)),
        str(render_vlan_sync_action(501, True, False)),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "row",
    [None, "", " ", "abc", "0", "5000", "501.0", "True", "0501", " 501 "],
    ids=["missing", "empty", "whitespace", "text", "zero", "too-large", "float", "boolean", "leading-zero", "padded"],
)
def test_a_group_choice_rejects_every_unusable_vlan_identity(client, settings, row):
    """The choice command must apply the same VLAN identity boundary as render and write paths."""
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import draft_command, draft_tab

    configure_default_librenms_server(settings)
    device = make_device("choice-invalid-vid", librenms_cf={"default": {"id": 42}})
    _seed_vlan_snapshot(device, [{"vlan_vlan": 501, "vlan_name": "Usable"}])
    client.force_login(make_superuser("choice-invalid-vid-user"))
    shown = draft_tab(client, device, "vlans")
    fields = {"field": "group", "value": ""} | ({} if row is None else {"row": row})

    response = draft_command(client, shown, "data-choice-url", **fields)

    assert response.status_code == 400
    assert draft_tab(client, device, "vlans").table["data-draft-revision"] == shown.table["data-draft-revision"]


@pytest.mark.django_db
def test_invalid_only_selection_still_reports_the_batch_summary(client, settings):
    """A confirmation whose every VID is unusable must keep its skip summary."""
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device("invalid-only-summary", librenms_cf={"default": {"id": 77}})
    _seed_vlan_snapshot(device, [{"vlan_vlan": 0, "vlan_name": "Invalid VID"}])
    client.force_login(make_superuser("invalid-only-summary-user"))

    response = _post(client, device, {"confirm_conflicts": "1", "force_conflict": "0"})

    assert response.status_code == 302
    rendered = _messages(response)
    assert "VLAN 0: the LibreNMS VID is invalid; skipped." in rendered
    assert "No VLANs synced: 1 skipped (invalid VLAN VID)." in rendered
    from ipam.models import VLAN

    assert not VLAN.objects.filter(vid=0).exists()


@pytest.mark.django_db
def test_unusable_selection_diagnostic_does_not_echo_an_unbounded_value(client, settings):
    """An oversized POST value must be reported as a bounded preview, not echoed whole."""
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device("unbounded-vid-diagnostic", librenms_cf={"default": {"id": 78}})
    _seed_vlan_snapshot(device, [{"vlan_vlan": 3201, "vlan_name": "Valid VLAN"}])
    client.force_login(make_superuser("unbounded-vid-diagnostic-user"))
    oversized = "x" * 5000

    response = _post(client, device, {"confirm_conflicts": "1", "force_conflict": oversized})

    assert response.status_code == 302
    diagnostics = _messages(response)
    assert any("the LibreNMS VID is invalid" in message for message in diagnostics)
    assert all(len(message) < 200 for message in diagnostics)
    assert not any(oversized in message for message in diagnostics)


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["force-all", "confirm-conflicts"])
def test_intent_only_group_requires_vlan_group_view_permission(client, settings, mode):
    """A signed intent that names a group must reach the permission gate without a POST field."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    configure_default_librenms_server(settings)
    device = make_device(f"intent-only-permission-{mode}", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name=f"Intent only {mode}", slug=f"intent-only-{mode}")
    vlan = VLAN.objects.create(vid=101, group=group, name="Current name")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 101, "vlan_name": "Proposed name"}])
    intent = SyncVLANsView()._build_conflict(
        vlan=vlan,
        proposed_name="Proposed name",
        obj=device,
        object_type="device",
        server_key="default",
    )["intent"]
    user = make_user_with_perms(
        f"intent-only-permission-{mode}-user",
        [("view", Device), ("add", VLAN), ("change", VLAN)],
    )
    client.force_login(user)
    payload = {"server_key": "default", "action": "create_vlans", "conflict_intent": intent}
    if mode == "force-all":
        payload["force_all"] = "1"
    else:
        payload["confirm_conflicts"] = "1"
        payload["force_conflict"] = "101"

    response = client.post(_sync_url(device), payload)

    assert response.status_code == 302
    assert [str(message) for message in response.wsgi_request._messages] == ["Missing permissions: ipam.view_vlangroup"]
    vlan.refresh_from_db()
    assert vlan.name == "Current name"


# ============================================
# THE VLANS TAB ON THE SELECTION DRAFT
# ============================================
# The group choice, the row read, and the submit that claims the draft. Each test seeds a VLAN snapshot and
# drives the real endpoints with a signed-in client; NetBox VLANs, VLAN groups and object permissions are real.

SERVER_KEY = "default"
HTMX = {"HTTP_HX_REQUEST": "true"}


def _tab(client, device, **query):
    return draft_tab(client, device, "vlans", **query)


def _submit(client, device, rows, **kwargs):
    kwargs.setdefault("headers", HTMX)
    return submit_with_draft(client, device, "vlans", _sync_url(device), rows, action="create_vlans", **kwargs)


def _row_html(html, row_key):
    for match in re.finditer(r"<tr\b[^>]*>.*?</tr>", html, re.S):
        if f'data-row-key="{row_key}"' in unescape(match.group(0)):
            return match.group(0)
    raise AssertionError(f"no row {row_key}")


def _shown_group(row_html):
    chosen = re.search(r'<option value="(\d*)"[^>]*selected', row_html)
    return chosen.group(1) if chosen else ""


def _normalized_row(html):
    def payload(match):
        return f'data-evidence="{json.dumps(read_evidence(VLAN_TAB, unescape(match.group(1))), sort_keys=True)}"'

    return re.sub(r"\s+", " ", re.sub(r'data-evidence="([^"]+)"', payload, html)).strip()


@pytest.fixture
def device(client, settings):
    """
    Return a device whose VLAN 300 exists as "Users" in group A and nowhere else, and whose VLAN 310 is new.

    Group B is a second device group. The client is signed in as a superuser.
    """
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("vlan-draft-user"))
    owner = make_device("vlan-draft", librenms_cf={SERVER_KEY: {"id": 4700}})
    group_a = VLANGroup.objects.create(name="vlan-draft-a", slug="vlan-draft-a", scope=owner.site)
    group_b = VLANGroup.objects.create(name="vlan-draft-b", slug="vlan-draft-b", scope=owner.site)
    VLAN.objects.create(vid=300, name="Users", group=group_a)
    _seed_vlan_snapshot(owner, [{"vlan_vlan": 300, "vlan_name": "Users"}, {"vlan_vlan": 310, "vlan_name": "Voice"}])
    owner.groups = (group_a, group_b)
    return owner


@pytest.mark.django_db
class TestTheTab:
    """The tab renders every row from the user's draft, with the evidence of its group and its proposed name."""

    def test_each_row_names_its_group_and_name_in_its_evidence(self, client, device):
        group_a, _group_b = device.groups
        shown = _tab(client, device)

        assert read_evidence(VLAN_TAB, shown.rows["300"]["data-evidence"])["group"] == group_a.pk
        assert read_evidence(VLAN_TAB, shown.rows["310"]["data-evidence"])["name"] == "Voice"
        assert shown.table["data-choice-url"].startswith("/plugins/")

    def test_a_selected_row_renders_checked_on_the_next_render(self, client, device):
        select_rows(client, device, "vlans", [310])

        shown = _tab(client, device)

        assert _checked(shown, "310") and not _checked(shown, "300")

    def test_a_row_read_is_the_row_of_the_tab_render(self, client, device):
        _group_a, group_b = device.groups
        select_rows(
            client,
            device,
            "vlans",
            [300],
            choices=[("data-choice-url", {"row": "310", "field": "group", "value": str(group_b.pk)})],
        )
        shown = _tab(client, device)

        for row_key, attrs in shown.rows.items():
            response = client.get(unescape(attrs["hx-get"]))

            assert response.status_code == 200
            assert _normalized_row(response.content.decode()) == _normalized_row(_row_html(shown.html, row_key))


def _hidden_selection(html):
    """Return the row keys that the off-page notice of *html* names, or None when the page has no notice."""
    clears = [attrs for attrs in open_tags(html, "button") if "data-clear-offpage" in attrs]
    if not [attrs for attrs in open_tags(html, "div") if "data-offpage-selection" in attrs]:
        return None
    (clear,) = clears
    return sorted(json.loads(clear["data-clear-offpage"]))


@pytest.mark.django_db
class TestTheHiddenSelection:
    """The page states the selected rows that it does not show, because the submit writes them too."""

    def test_the_selected_rows_that_the_page_does_not_show_are_counted_above_the_table(self, client, device):
        select_rows(client, device, "vlans", [300, 310])

        # The filter shows only VLAN 310, so the selected VLAN 300 is on no shown page.
        assert _hidden_selection(_tab(client, device, vlans_q="Voice").html) == ["300"]
        assert _hidden_selection(_tab(client, device).html) is None

    def test_an_invalid_filter_counts_every_selected_row_as_hidden_and_keeps_the_draft_on_the_page(
        self, client, device
    ):
        select_rows(client, device, "vlans", [300, 310])
        url = reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "vlans"},
        )

        # The search is longer than a filter takes, so the tab shows no row.
        html = client.get(url, {"server_key": SERVER_KEY, "vlans_q": "x" * 300}).content.decode()

        assert "data-filter-invalid" in html
        assert not [attrs for attrs in open_tags(html, "tr") if attrs.get("data-row-key")]
        assert _hidden_selection(html) == ["300", "310"]
        # The page keeps the draft that Clear and the submit name.
        (table,) = [attrs for attrs in open_tags(html, "table") if attrs.get("data-draft-id")]
        assert table["data-draft-id"] == _tab(client, device).table["data-draft-id"]
        assert table["data-selection-url"]


@pytest.mark.django_db
class TestTheGroupChoice:
    """A group choice is a draft command; the row read shows the row compared in that group."""

    def test_a_choice_names_the_row_and_its_read_compares_the_vlan_in_that_group(self, client, device):
        _group_a, group_b = device.groups
        shown = _tab(client, device)
        assert "Synced" in _status_cell(_row_html(shown.html, "300"))

        response = draft_command(client, shown, "data-choice-url", row="300", field="group", value=str(group_b.pk))

        assert response.status_code == 204
        assert json.loads(response["HX-Trigger"]) == {"lnms-row-stale": {"key": "300"}}
        row = client.get(unescape(shown.rows["300"]["hx-get"])).content.decode()
        assert _shown_group(row) == str(group_b.pk)
        assert 'name="sync_one" value="300"' in _status_cell(row)

    def test_a_choice_on_a_selected_row_asks_to_select_it_again(self, client, device):
        _group_a, group_b = device.groups
        draft = select_rows(client, device, "vlans", [310])
        shown = _tab(client, device)

        draft_command(
            client,
            shown,
            "data-choice-url",
            draft["expected_revision"],
            row="310",
            field="group",
            value=str(group_b.pk),
        )

        after = _tab(client, device)
        assert not _checked(after, "310")
        assert after.rows["310"]["data-evidence-changed"] == "true"

    def test_choosing_the_group_the_row_shows_stores_no_choice(self, client, device):
        group_a, _group_b = device.groups
        shown = _tab(client, device)

        assert (
            draft_command(client, shown, "data-choice-url", row="300", field="group", value=str(group_a.pk)).status_code
            == 204
        )

        # The group stays the shown one: a later VLAN of the row decides it again.
        after = _tab(client, device)
        assert after.table["data-draft-revision"] == "1"
        assert _shown_group(_row_html(after.html, "300")) == str(group_a.pk)

    def test_a_group_the_user_cannot_view_answers_404(self, client, device):
        elsewhere = Site.objects.create(name="vlan-draft-elsewhere", slug="vlan-draft-elsewhere")
        foreign = VLANGroup.objects.create(name="vlan-draft-foreign", slug="vlan-draft-foreign", scope=elsewhere)
        shown = _tab(client, device)

        response = draft_command(client, shown, "data-choice-url", row="300", field="group", value=str(foreign.pk))

        assert response.status_code == 404
        assert _tab(client, device).table["data-draft-revision"] == "0"


@pytest.mark.django_db
class TestTheSubmit:
    """A bulk submit claims the draft, writes the approved rows and puts back every row it did not write."""

    def test_the_submit_creates_the_vlan_in_the_chosen_group_and_consumes_the_row(self, client, device):
        _group_a, group_b = device.groups

        response = _submit(
            client,
            device,
            [310],
            choices=[("data-choice-url", {"row": "310", "field": "group", "value": str(group_b.pk)})],
        )

        assert response.status_code == 200
        assert json.loads(response["HX-Trigger"]) == {"lnms-results-stale": None}
        assert list(VLAN.objects.filter(vid=310).values_list("group_id", "name")) == [(group_b.pk, "Voice")]
        after = _tab(client, device)
        assert response["X-Draft-Revision"] == after.table["data-draft-revision"]
        assert not _checked(after, "310")

    def test_a_row_that_changed_after_its_selection_is_refused_and_put_back(self, client, device):
        _group_a, group_b = device.groups
        draft = select_rows(
            client,
            device,
            "vlans",
            [310],
            choices=[("data-choice-url", {"row": "310", "field": "group", "value": str(group_b.pk)})],
        )
        # Another user creates the VLAN in the group the row writes to.
        VLAN.objects.create(vid=310, name="Phones", group=group_b)

        response = _post(client, device, draft, **HTMX)

        assert list(VLAN.objects.filter(vid=310).values_list("name", flat=True)) == ["Phones"]
        assert any("310" in text and "changed" in text for text in _messages(response))
        assert _tab(client, device).rows["310"]["data-evidence-changed"] == "true"

    def test_a_submit_of_an_older_revision_writes_nothing(self, client, device):
        draft = select_rows(client, device, "vlans", [310])
        select_rows(client, device, "vlans", [300])

        response = _post(client, device, draft, **HTMX)

        assert not VLAN.objects.filter(vid=310).exists()
        assert any("selection changed" in text for text in _messages(response))

    def test_a_rename_disclosure_keeps_the_row_and_names_the_revision(self, client, device):
        VLAN.objects.filter(vid=300).update(name="Staff")

        response = _submit(client, device, [300])

        assert "conflict_intent" in response.content.decode()
        assert json.loads(response["HX-Trigger"]) == {"lnms-results-stale": None}
        after = _tab(client, device)
        assert response["X-Draft-Revision"] == after.table["data-draft-revision"]
        assert _checked(after, "300")

    def test_a_row_whose_group_the_user_can_no_longer_view_is_refused_and_put_back(self, client, device):
        from users.models import ObjectPermission

        _group_a, group_b = device.groups
        user = make_user_with_perms("vlan-draft-group-grant", [("view", Device), ("add", VLAN), ("change", VLAN)])
        grant(user, "view", VLAN)
        grant(user, "view", VLANGroup, name="vlan-draft-group-view")
        client.force_login(user)
        draft = select_rows(
            client,
            device,
            "vlans",
            [310],
            choices=[("data-choice-url", {"row": "310", "field": "group", "value": str(group_b.pk)})],
        )
        # The group grant goes away after the user selected the row in that group.
        ObjectPermission.objects.filter(name="vlan-draft-group-view").delete()

        response = _post(client, device, draft, **HTMX)

        assert not VLAN.objects.filter(vid=310).exists()
        assert any("310" in text and "changed" in text for text in _messages(response))
        # The submit put the row back; without the group it asks to be selected again.
        assert _tab(client, device).rows["310"]["data-evidence-changed"] == "true"


@pytest.mark.django_db
class TestTheRowAction:
    """``sync_one`` writes the row as its page shows it, and leaves the draft as it is."""

    def test_sync_one_writes_the_shown_row_and_keeps_the_selection(self, client, device):
        select_rows(client, device, "vlans", [300])

        _post(client, device, {"sync_one": "310", **row_action_fields(client, device, "vlans", 310)}, **HTMX)

        assert VLAN.objects.filter(vid=310, name="Voice").exists()
        assert _checked(_tab(client, device), "300")

    def test_a_row_action_of_another_revision_writes_nothing(self, client, device):
        fields = row_action_fields(client, device, "vlans", 310)
        select_rows(client, device, "vlans", [300])

        response = _post(client, device, {"sync_one": "310", **fields}, **HTMX)

        assert not VLAN.objects.filter(vid=310).exists()
        assert any("changed after the page showed it" in text for text in _messages(response))
