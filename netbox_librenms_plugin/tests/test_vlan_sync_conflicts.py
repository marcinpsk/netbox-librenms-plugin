"""End-to-end checks for confirmation-required VLAN synchronization changes."""

import re
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_superuser,
)
from netbox_librenms_plugin.tests.vlan_intent_helpers import message_texts, seed_vlans, sync, tab_tokens
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping


def _disclosed_tokens(response, form_id):
    """Return the tokens that one form of the rename disclosure posts."""
    html = response.content.decode()
    form = re.search(rf'<form[^>]*id="{form_id}".*?</form>', html, re.S).group(0)
    return re.findall(r'name="confirmation_intent"\s+value="([^"]*)"', form)


def _grouped_rename(tag, vid, current, proposed):
    from ipam.models import VLAN, VLANGroup

    device = seed_mapping(make_device(f"vlan-{tag}-device"), "default", own=42)
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
    configure_default_librenms_server(settings)
    device, _group, _existing = _grouped_rename("tabbar", 112, "Current name", "Proposed name")
    client.force_login(make_superuser("vlan-tabbar-user"))

    response = sync(client, device, [tab_tokens(client, device)[112]], htmx=False)

    assert response.status_code == 200
    assert _active_tab_urls(response) == [reverse("dcim:device_librenms_sync", kwargs={"pk": device.pk})]


@pytest.mark.django_db
def test_native_vlan_review_page_marks_the_sync_tab_active(client, settings):
    """The full review page belongs to the LibreNMS Sync tab, not to the device tab."""
    configure_default_librenms_server(settings)
    device, group, _existing = _grouped_rename("review-tabbar", 113, "Current name", "Proposed name")
    client.force_login(make_superuser("vlan-review-tabbar-user"))

    response = _review(client, device, ["113"], {113: group})

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
    device = seed_mapping(make_device("vlan-bulk-device"), "default", own=42)
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


def _review(client, device, selected, groups, **fields):
    """POST the read-only review that a page without JavaScript submits: the ticked rows and every group field."""
    from netbox_librenms_plugin.tests.vlan_intent_helpers import sync_url

    data = {"server_key": "default", "action": "create_vlans", "review_choices": "1", "select": selected, **fields}
    data.update({f"vlan_group_{vid}": "" if group is None else str(group.pk) for vid, group in groups.items()})
    return client.post(sync_url(device), data)


@pytest.mark.django_db
def test_a_review_without_javascript_offers_the_chosen_operations_and_writes_nothing(client, settings):
    """The review reassesses the posted choices; an explicit apply of its tokens writes them."""
    from django.core import signing
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.confirmation_intent import SALT_NAMESPACE
    from netbox_librenms_plugin.tests.view_test_helpers import domain_state

    configure_default_librenms_server(settings)
    device, group, existing = _grouped_rename("review", 110, "Current name", "Proposed name")
    chosen = VLANGroup.objects.create(name="Review chosen", slug="review-chosen")
    seed_vlans(
        device,
        [{"vlan_vlan": 110, "vlan_name": "Proposed name"}, {"vlan_vlan": 111, "vlan_name": "New VLAN"}],
    )
    client.force_login(make_superuser("vlan-review-user"))
    before = domain_state()

    review = _review(client, device, ["110", "111"], {110: group, 111: chosen})

    assert domain_state() == before
    assert review.status_code == 200
    assert any(template.name == "netbox_librenms_plugin/vlan_conflicts_page.html" for template in review.templates)
    tokens = _disclosed_tokens(review, "vlan-conflicts-all-form")
    operations = {
        signing.loads(token.partition(":")[2], salt=f"{SALT_NAMESPACE}:vlan")["operation"]["identity"]["vid"]: (
            signing.loads(token.partition(":")[2], salt=f"{SALT_NAMESPACE}:vlan")["operation"]
        )
        for token in tokens
    }
    assert operations[111]["choices"] == {"group": chosen.pk}
    assert operations[110]["effects"] == {"vlan": existing.pk, "name": "Proposed name"}

    applied = sync(client, device, tokens, confirm=True, htmx=False)

    assert applied.status_code == 302
    existing.refresh_from_db()
    assert existing.name == "Proposed name"
    assert VLAN.objects.filter(vid=111, group=chosen, name="New VLAN").exists()


@pytest.mark.django_db
def test_a_review_never_applies_in_the_same_request(client, settings):
    """A review that also carries tokens reassesses only; its tokens write nothing."""
    from netbox_librenms_plugin.tests.view_test_helpers import domain_state

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("vlan-review-only-device"), "default", own=42)
    seed_vlans(device, [{"vlan_vlan": 112, "vlan_name": "Users"}])
    client.force_login(make_superuser("vlan-review-only-user"))
    token = tab_tokens(client, device)[112]
    before = domain_state()

    review = _review(client, device, ["112"], {112: None}, confirmation_intent=token, confirm_conflicts="1")

    assert review.status_code == 200
    assert domain_state() == before


@pytest.mark.django_db
def test_a_review_refuses_a_choice_it_cannot_assess(client, settings):
    """A missing group field, an unknown group or a VID without a source row offers nothing for that row."""
    from netbox_librenms_plugin.tests.vlan_intent_helpers import message_texts

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("vlan-review-refusal-device"), "default", own=42)
    seed_vlans(device, [{"vlan_vlan": 113, "vlan_name": "A"}, {"vlan_vlan": 114, "vlan_name": "B"}])
    client.force_login(make_superuser("vlan-review-refusal-user"))

    response = _review(client, device, ["113", "114", "115"], {115: None}, vlan_group_114="2147483647")

    texts = message_texts(response)
    assert response.status_code == 302
    assert [text.split(":")[0] for text in texts if text.startswith("VLAN ")] == ["VLAN 113", "VLAN 114", "VLAN 115"]


@pytest.mark.django_db
def test_a_review_that_names_a_group_requires_vlan_group_view_permission(client, settings):
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.vlan_intent_helpers import message_texts
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("vlan-review-permission-device"), "default", own=42)
    group = VLANGroup.objects.create(name="Review permission", slug="review-permission")
    seed_vlans(device, [{"vlan_vlan": 116, "vlan_name": "A"}])
    client.force_login(
        make_user_with_perms("vlan-review-permission-user", [("view", Device), ("add", VLAN), ("change", VLAN)])
    )

    response = _review(client, device, ["116"], {116: group})

    assert response.status_code == 302
    assert message_texts(response) == ["Missing permissions: ipam.view_vlangroup"]


class _NoScriptForm(HTMLParser):
    """Collect what the VLAN sync form posts without JavaScript: enabled fields, ticked rows, the review button."""

    def __init__(self, tick):
        super().__init__()
        self.tick, self.fields, self.review, self._select, self._in_form = tick, [], None, None, False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        name = attributes.get("name")
        if tag == "form":
            self._in_form = attributes.get("id") == "vlan-sync-form"
        if not self._in_form:
            return
        if tag == "input" and name and "disabled" not in attributes:
            if attributes.get("type") != "checkbox" or attributes.get("value") in self.tick:
                self.fields.append((name, attributes.get("value", "")))
        elif tag == "select" and name:
            self._select, self._options, self._chosen = name, [], None
        elif tag == "option" and self._select:
            self._options.append(attributes.get("value", ""))
            if "selected" in attributes:
                self._chosen = attributes.get("value", "")
        elif tag == "button" and name == "review_choices":
            self.review = (name, attributes["value"])

    def handle_endtag(self, tag):
        if tag == "select" and self._select:
            # A browser posts the first option of a select that marks none selected.
            self.fields.append((self._select, self._chosen if self._chosen is not None else self._options[0]))
            self._select = None
        elif tag == "form":
            self._in_form = False


@pytest.mark.django_db
def test_the_rendered_tab_reviews_and_applies_without_javascript(client, settings):
    """The page posts no token without JavaScript; its Review choices button leads to an explicit apply."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.vlan_intent_helpers import sync_url

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("vlan-noscript-device"), "default", own=42)
    seed_vlans(device, [{"vlan_vlan": 117, "vlan_name": "Users"}, {"vlan_vlan": 118, "vlan_name": "Voice"}])
    client.force_login(make_superuser("vlan-noscript-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "vlans"},
    )
    form = _NoScriptForm(tick={"117"})
    form.feed(client.get(f"{url}?server_key=default").content.decode())

    assert not any(name == "confirmation_intent" for name, _value in form.fields)
    review = client.post(
        sync_url(device), urlencode([*form.fields, form.review]), content_type="application/x-www-form-urlencoded"
    )

    assert not VLAN.objects.filter(vid__in=[117, 118]).exists()
    tokens = _disclosed_tokens(review, "vlan-conflicts-all-form")
    assert len(tokens) == 1
    sync(client, device, tokens, confirm=True, htmx=False)
    assert list(VLAN.objects.filter(vid__in=[117, 118]).values_list("vid", flat=True)) == [117]
