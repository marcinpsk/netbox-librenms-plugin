"""End-to-end checks for confirmation-required VLAN synchronization changes."""

import re

import pytest

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_superuser,
)
from netbox_librenms_plugin.tests.vlan_intent_helpers import message_texts, seed_vlans, sync, tab_tokens


def _disclosed_tokens(response, form_id):
    """Return the tokens that one form of the rename disclosure posts."""
    html = response.content.decode()
    form = re.search(rf'<form[^>]*id="{form_id}".*?</form>', html, re.S).group(0)
    return re.findall(r'name="confirmation_intent"\s+value="([^"]*)"', form)


def _grouped_rename(tag, vid, current, proposed):
    from ipam.models import VLAN, VLANGroup

    device = make_device(f"vlan-{tag}-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name=f"{tag.title()} group", slug=f"{tag}-group")
    existing = VLAN.objects.create(vid=vid, group=group, name=current, status="active")
    seed_vlans(device, [{"vlan_vlan": vid, "vlan_name": proposed}])
    return device, group, existing


@pytest.mark.django_db
def test_selected_grouped_vlan_requires_confirmation_before_rename(client, settings):
    """A selected VID must not silently replace an existing VLAN name in its group."""
    configure_default_librenms_server(settings)
    device, group, existing = _grouped_rename("conflict", 100, "Current name", "Proposed name")
    client.force_login(make_superuser("vlan-conflict-user"))

    response = sync(client, device, [tab_tokens(client, device)[100]])

    assert response.status_code == 200
    assert b"Confirm VLAN changes" in response.content
    assert b"Current name" in response.content
    assert b"Proposed name" in response.content
    assert group.name.encode() in response.content
    existing.refresh_from_db()
    assert existing.name == "Current name"


@pytest.mark.django_db
def test_confirmed_grouped_vlan_rename_applies_the_disclosed_name(client, settings):
    """A valid confirmation must rename only the VLAN named by its signed operation."""
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import VLAN

    configure_default_librenms_server(settings)
    device, _group, existing = _grouped_rename("confirm", 101, "Current name", "Confirmed name")
    client.force_login(make_superuser("vlan-confirm-user"))
    token = tab_tokens(client, device)[101]
    disclosure = sync(client, device, [token])

    assert _disclosed_tokens(disclosure, "vlan-conflicts-selected-form") == [token]
    response = sync(client, device, [token], confirm=True)

    assert response.status_code == 200
    assert response.headers["HX-Redirect"]
    existing.refresh_from_db()
    assert existing.name == "Confirmed name"
    change = ObjectChange.objects.get(
        changed_object_type=ContentType.objects.get_for_model(VLAN),
        changed_object_id=existing.pk,
    )
    assert change.prechange_data["name"] == "Current name"
    assert change.postchange_data["name"] == "Confirmed name"


@pytest.mark.django_db
def test_stale_vlan_confirmation_fails_closed(client, settings):
    """A confirmation must not replace a VLAN name changed after disclosure."""
    from ipam.models import VLAN

    configure_default_librenms_server(settings)
    device, _group, existing = _grouped_rename("stale", 102, "Original name", "Proposed name")
    client.force_login(make_superuser("vlan-stale-user"))
    disclosure = sync(client, device, [tab_tokens(client, device)[102]])
    VLAN.objects.filter(pk=existing.pk).update(name="Concurrent name")

    response = sync(client, device, _disclosed_tokens(disclosure, "vlan-conflicts-selected-form"), confirm=True)

    assert response.status_code == 200
    assert response.headers["HX-Redirect"]
    assert any(text.startswith("VLAN 102: ") and "assumptions.target.name" in text for text in message_texts(response))
    existing.refresh_from_db()
    assert existing.name == "Concurrent name"


@pytest.mark.django_db
def test_tampered_vlan_confirmation_syncs_nothing(client, settings):
    """An invalid token must not fall back to any unsigned row field."""
    configure_default_librenms_server(settings)
    device, group, existing = _grouped_rename("tampered", 103, "Current name", "Proposed name")
    client.force_login(make_superuser("vlan-tampered-user"))

    response = sync(
        client,
        device,
        ["invalid-token"],
        confirm=True,
        select="103",
        vlan_group_103=str(group.pk),
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"]
    assert message_texts(response) == [
        "VLAN confirmation is invalid or has expired. Refresh the VLAN data and try again.",
        "No VLANs synced: 1 skipped (confirmation no longer current).",
    ]
    existing.refresh_from_db()
    assert existing.name == "Current name"


@pytest.mark.django_db
def test_native_vlan_conflict_renders_a_complete_page(client, settings):
    """A native conflict response must render a full page around the confirmation form."""
    configure_default_librenms_server(settings)
    device, _group, existing = _grouped_rename("native", 104, "Current name", "Proposed name")
    client.force_login(make_superuser("vlan-native-user"))

    response = sync(client, device, [tab_tokens(client, device)[104]], htmx=False)

    assert response.status_code == 200
    assert any(template.name == "netbox_librenms_plugin/vlan_conflicts_page.html" for template in response.templates)
    assert b"Confirm VLAN changes" in response.content
    existing.refresh_from_db()
    assert existing.name == "Current name"


def _active_tab_urls(response):
    """Return the href of every active link in the object tab bar of a full page."""
    html = response.content.decode()
    tab_bar = re.search(r'<ul class="nav nav-tabs".*?</ul>', html, re.S).group(0)
    links = re.findall(r"<a\s[^>]*>", tab_bar)
    return [
        re.search(r'href="([^"]*)"', link).group(1)
        for link in links
        if "active" in re.search(r'class="([^"]*)"', link).group(1).split()
    ]


@pytest.mark.django_db
def test_native_vlan_conflict_page_marks_the_sync_tab_active(client, settings):
    """The full conflict page belongs to the LibreNMS Sync tab, not to the device tab."""
    from django.urls import reverse

    configure_default_librenms_server(settings)
    device, _group, _existing = _grouped_rename("tabbar", 112, "Current name", "Proposed name")
    client.force_login(make_superuser("vlan-tabbar-user"))

    response = sync(client, device, [tab_tokens(client, device)[112]], htmx=False)

    assert response.status_code == 200
    assert _active_tab_urls(response) == [reverse("dcim:device_librenms_sync", kwargs={"pk": device.pk})]


@pytest.mark.django_db
def test_bulk_vlan_sync_commits_safe_rows_before_confirmation(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A safe row must commit while a conflicting rename remains pending, and Apply all confirms it."""
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.source_snapshot_helpers import stored_source_value

    configure_default_librenms_server(settings)
    device = make_device("vlan-bulk-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Bulk group", slug="bulk-group")
    existing = VLAN.objects.create(vid=105, group=group, name="Current name", status="active")
    snapshot = [
        {"vlan_vlan": 105, "vlan_name": "Proposed name"},
        {"vlan_vlan": 107, "vlan_name": "Safe name"},
    ]
    seed_vlans(device, snapshot)
    client.force_login(make_superuser("vlan-bulk-user"))
    tokens = tab_tokens(client, device)

    with django_capture_on_commit_callbacks(execute=True):
        response = sync(client, device, [tokens[105], tokens[107]])

    assert response.status_code == 200
    assert b"Confirm VLAN changes" in response.content
    assert VLAN.objects.filter(vid=107, group__isnull=True, name="Safe name").exists()
    existing.refresh_from_db()
    assert existing.name == "Current name"
    assert stored_source_value(device, Resource.VLANS, "vlans") == snapshot

    confirm_response = sync(client, device, _disclosed_tokens(response, "vlan-conflicts-all-form"), confirm=True)

    assert confirm_response.status_code == 200
    assert confirm_response.headers["HX-Redirect"]
    existing.refresh_from_db()
    assert existing.name == "Proposed name"
