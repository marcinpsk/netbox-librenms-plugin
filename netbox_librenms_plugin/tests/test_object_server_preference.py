"""Request-level coverage for an object's preferred LibreNMS server."""

import re
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.mapping_fixtures import (
    mapping_from_change_record,
    mapping_summary,
    seed_malformed_preference,
    seed_mapping,
    seed_mapping_row,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import assert_titled_icons_are_named, grant, make_user_with_perms


def _message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _configure_servers(settings, primary, secondary):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "primary": {
            "display_name": "Primary LibreNMS",
            "librenms_url": primary.url,
            "api_token": "preference-test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
        "secondary": {
            "display_name": "Secondary LibreNMS",
            "librenms_url": secondary.url,
            "api_token": "preference-test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def servers(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        primary = stack.enter_context(librenms_mock_server())
        secondary = stack.enter_context(librenms_mock_server())
        _configure_servers(settings, primary, secondary)
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
        yield SimpleNamespace(primary=primary, secondary=secondary)


def _register_device(server, device_id, name):
    server.register(
        f"/api/v0/devices/{device_id}",
        {
            "status": "ok",
            "devices": [
                {
                    "device_id": device_id,
                    "sysName": name,
                    "hostname": name,
                    "hardware": "Test appliance",
                }
            ],
        },
    )


def _preference_url(obj):
    return reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[obj.pk])


def _sync_url(obj):
    name = "vm_librenms_sync" if obj._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", args=[obj.pk])


@pytest.mark.django_db
def test_preference_post_changes_only_preference_and_keeps_transient_server(client, servers):
    device = make_device("set-object-preference")
    seed_mapping(device, "primary", own=13501, save=False)
    seed_mapping(device, "secondary", own=13502)
    client.force_login(make_superuser("object-preference-writer"))

    response = client.post(
        _preference_url(device),
        {
            "object_type": "device",
            "server_key": "secondary",
            "active_server_key": "primary",
            "tab": "modules",
        },
    )

    assert response.status_code == 302
    assert response.url == f"{_sync_url(device)}?tab=modules&server_key=primary"
    device.refresh_from_db()
    assert mapping_summary(device) == {
        "primary": {"own": 13501},
        "secondary": {"own": 13502},
        "preferred": "secondary",
    }
    assert _message_texts(response) == ["Preferred LibreNMS server changed to 'secondary'."]


@pytest.mark.django_db
def test_preference_post_records_the_mapping_change_with_its_before_state(client, servers):
    """The owner is saved with only its custom field data, so the save must also move last_updated."""
    from core.models import ObjectChange
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType

    mapping = {"primary": {"id": 13511}, "secondary": {"id": 13512}}
    device = make_device("set-object-preference-log", librenms_cf=mapping)
    old_last_updated = Device.objects.values_list("last_updated", flat=True).get(pk=device.pk)
    client.force_login(make_superuser("object-preference-log-writer"))

    client.post(_preference_url(device), {"object_type": "device", "server_key": "secondary"})

    device.refresh_from_db()
    assert device.last_updated > old_last_updated
    change = ObjectChange.objects.get(
        changed_object_type=ContentType.objects.get_for_model(Device), changed_object_id=device.pk, action="update"
    )
    assert mapping_from_change_record(change, before=True) == mapping
    assert mapping_from_change_record(change, before=False) == {**mapping, "_preferred_server": "secondary"}


@pytest.mark.django_db
def test_preference_post_keeps_the_active_server_when_the_key_is_rejected(client, servers):
    """A malformed server_key must not drop a non-default page back to the default server."""
    device = make_device("preference-invalid-key")
    seed_mapping(device, "primary", own=13521, save=False)
    seed_mapping(device, "secondary", own=13522)
    client.force_login(make_superuser("object-preference-invalid-key"))

    response = client.post(
        _preference_url(device),
        {
            "object_type": "device",
            "server_key": "   ",
            "active_server_key": "primary",
            "tab": "modules",
        },
    )

    assert response.status_code == 302
    assert response.url == f"{_sync_url(device)}?tab=modules&server_key=primary"
    assert _message_texts(response) == ["LibreNMS server key must be a non-empty string."]
    device.refresh_from_db()
    assert "preferred" not in mapping_summary(device)


@pytest.mark.django_db
def test_preference_post_ignores_unrelated_legacy_validation_errors(client, servers):
    owner = make_device("preference-with-legacy-rack-fields")
    seed_mapping(owner, "primary", own=13532, save=False)
    seed_mapping(owner, "secondary", own=13533)
    type(owner).objects.filter(pk=owner.pk).update(face="front", status="obsolete")
    client.force_login(make_superuser("legacy-validation-preference-writer"))

    response = client.post(
        _preference_url(owner),
        {"object_type": "device", "server_key": "secondary"},
    )

    assert response.status_code == 302
    assert _message_texts(response) == ["Preferred LibreNMS server changed to 'secondary'."]
    owner.refresh_from_db()
    assert mapping_summary(owner)["preferred"] == "secondary"
    assert owner.face == "front"


@pytest.mark.django_db
def test_preference_post_supports_virtual_machines(client, servers):
    vm = make_vm("set-vm-preference")
    seed_mapping(vm, "primary", own=13503, save=False)
    seed_mapping(vm, "secondary", own=13504)
    client.force_login(make_superuser("vm-preference-writer"))

    response = client.post(
        _preference_url(vm),
        {
            "object_type": "virtualmachine",
            "server_key": "secondary",
            "active_server_key": "primary",
        },
    )

    assert response.status_code == 302
    assert response.url == f"{_sync_url(vm)}?server_key=primary"
    vm.refresh_from_db()
    assert mapping_summary(vm)["preferred"] == "secondary"


@pytest.mark.django_db
def test_preference_post_requires_change_scope_on_mapping_owner(client, servers):
    owner = make_device("preference-outside-scope")
    seed_mapping(owner, "primary", own=13505, save=False)
    seed_mapping(owner, "secondary", own=13506)
    allowed = make_device("preference-inside-scope")
    user = make_user_with_perms(
        "scoped-preference-writer",
        [("change", type(owner))],
        constraints={"pk": allowed.pk},
    )
    client.force_login(user)

    response = client.post(
        _preference_url(owner),
        {"object_type": "device", "server_key": "secondary"},
    )

    assert response.status_code == 404
    owner.refresh_from_db()
    assert "preferred" not in mapping_summary(owner)


@pytest.mark.django_db
def test_preference_post_requires_plugin_write_permission(client, servers):
    owner = make_device("preference-without-plugin-write")
    seed_mapping(owner, "primary", own=13524, save=False)
    seed_mapping(owner, "secondary", own=13525)
    user = get_user_model().objects.create_user(username="object-only-preference-writer", password="x")
    user = grant(user, "view", apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"))
    user = grant(user, "change", type(owner))
    client.force_login(user)

    response = client.post(
        _preference_url(owner),
        {"object_type": "device", "server_key": "secondary"},
        HTTP_REFERER=_sync_url(owner),
    )

    assert response.status_code == 302
    assert response.url == _sync_url(owner)
    assert _message_texts(response) == ["You do not have permission to perform this action."]
    owner.refresh_from_db()
    assert "preferred" not in mapping_summary(owner)


@pytest.mark.django_db
def test_preference_post_revalidates_mapping_after_lock(client, servers):
    owner = make_device("preference-locked-state")
    seed_mapping(owner, "primary", own=13507, save=False)
    seed_mapping(owner, "secondary", own=13508)
    client.force_login(make_superuser("locked-preference-writer"))

    class RemoveTargetBeforeLock:
        fired = False

        def __call__(self, execute, sql, params, many, context):
            if not self.fired and 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql.upper():
                self.fired = True
                seed_mapping_row(owner, seed_mapping(type(owner)(), "primary", own=13507, save=False))
            return execute(sql, params, many, context)

    lock_hook = RemoveTargetBeforeLock()
    with connection.execute_wrapper(lock_hook):
        response = client.post(
            _preference_url(owner),
            {"object_type": "device", "server_key": "secondary"},
        )

    assert response.status_code == 302
    assert lock_hook.fired
    assert _message_texts(response) == ["A preferred server requires at least two usable object mappings."]
    owner.refresh_from_db()
    assert mapping_summary(owner) == {"primary": {"own": 13507}}


@pytest.mark.django_db
def test_single_mapping_remains_implicit_without_stored_preference(client, servers):
    owner = seed_mapping(make_device("implicit-single-server"), "primary", own=13509)
    _register_device(servers.primary, 13509, owner.name)
    client.force_login(make_superuser("single-preference-writer"))

    response = client.post(
        _preference_url(owner),
        {"object_type": "device", "server_key": "primary"},
    )

    assert _message_texts(response) == ["A preferred server requires at least two usable object mappings."]
    owner.refresh_from_db()
    assert mapping_summary(owner) == {"primary": {"own": 13509}}

    page = client.get(_sync_url(owner))

    assert page.status_code == 200
    assert _preference_url(owner) not in page.content.decode()
    assert b'id="librenms-server-preference-warning"' not in page.content


@pytest.mark.django_db
def test_active_and_preferred_servers_render_as_distinct_states(client, servers):
    owner = make_device("active-versus-preferred")
    seed_mapping(owner, "primary", own=13510, save=False)
    seed_mapping(owner, "secondary", own=13511, preferred=True)
    _register_device(servers.primary, 13510, owner.name)
    client.force_login(make_superuser("preference-render-writer"))

    response = client.get(_sync_url(owner), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert 'data-active-server-key="primary"' in html
    assert 'data-preferred-server-key="secondary"' in html
    assert 'name="active_server_key" value="primary"' in html
    assert _preference_url(owner) in html
    card_start = html.index('id="librenms-connections"')
    assert "confirm(" not in html[card_start : html.index("</table>", card_start)]


@pytest.mark.django_db
def test_the_server_state_and_name_check_icons_are_named_or_hidden(client, servers):
    owner = make_device("named-server-icons")
    seed_mapping(owner, "primary", own=13512, save=False)
    seed_mapping(owner, "secondary", own=13513, preferred=True)
    seed_mapping(owner, "retired", own=13514)
    _register_device(servers.primary, 13512, owner.name)
    client.force_login(make_superuser("named-server-icons-viewer"))

    response = client.get(_sync_url(owner), {"server_key": "primary"})

    assert response.status_code == 200
    html = response.content.decode()
    # NetBox's own page chrome is outside the plugin: check the server card through the device sync table.
    start = html.index('id="librenms-connections"')
    icons = assert_titled_icons_are_named(html[start : html.index("</table>", html.index("librenms-info-sync-table"))])
    by_class = {next(c for c in icon["class"].split() if c.startswith("mdi-")): icon for icon in icons}
    assert by_class["mdi-server-off"]["aria-hidden"] == "true"
    for named in ("mdi-star", "mdi-check-circle", "mdi-information-outline"):
        assert by_class[named]["role"] == "img"


@pytest.mark.django_db
def test_the_sync_tabs_name_the_active_server_for_the_page_scripts(client, servers):
    """The scripts read the page server from the sync tabs, not from another server's form."""
    owner = make_device("sync-tabs-active-server")
    seed_mapping(owner, "primary", own=13601, save=False)
    seed_mapping(owner, "secondary", own=13602, preferred=True)
    _register_device(servers.primary, 13601, owner.name)
    client.force_login(make_superuser("sync-tabs-active-server-writer"))

    html = client.get(_sync_url(owner), {"server_key": "primary"}).content.decode()

    tabs = re.search(r'<div id="librenms-sync-tabs"[^>]*>', html)
    assert tabs, "the sync tabs did not render"
    assert 'data-server-key="primary"' in tabs.group(0)


@pytest.mark.django_db
def test_remove_mapping_form_preserves_the_active_server(client, servers):
    owner = make_device("remove-mapping-active-server")
    seed_mapping(owner, "primary", own=13599, save=False)
    seed_mapping(owner, "retired", own=13600)
    _register_device(servers.primary, 13599, owner.name)
    client.force_login(make_superuser("remove-mapping-active-server-writer"))

    response = client.get(_sync_url(owner), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[owner.pk]) in html
    assert html.count('name="active_server_key" value="primary"') == 1


@pytest.mark.django_db
def test_view_only_user_sees_preference_but_not_star_controls(client, servers):
    owner = make_device("view-only-preference")
    seed_mapping(owner, "primary", own=13512, save=False)
    seed_mapping(owner, "secondary", own=13513, preferred=True)
    _register_device(servers.primary, 13512, owner.name)
    user = get_user_model().objects.create_user(username="preference-viewer", password="x")
    user = grant(user, "view", apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"))
    user = grant(user, "view", type(owner))
    client.force_login(user)

    response = client.get(_sync_url(owner), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert 'data-preferred-server-key="secondary"' in html
    assert _preference_url(owner) not in html
    assert "server_key=secondary" in html


@pytest.mark.django_db
def test_vc_member_page_uses_mapping_owner_for_preference_form(client, servers):
    _chassis, (owner, viewed_member) = make_virtual_chassis_members("preference-owner", count=2)
    seed_mapping(owner, "primary", own=13514, save=False)
    seed_mapping(owner, "secondary", own=13515)
    _register_device(servers.primary, 13514, viewed_member.name)
    servers.primary.register(
        "/api/v0/inventory/13514/all",
        {"status": "ok", "inventory": []},
    )
    client.force_login(make_superuser("vc-preference-writer"))

    response = client.get(_sync_url(viewed_member), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert _preference_url(owner) in html
    assert _preference_url(viewed_member) not in html


@pytest.mark.django_db
@pytest.mark.parametrize("stored_preference", [None, "malformed", "retired"])
def test_invalid_or_missing_preference_warns_and_get_does_not_mutate(
    client,
    servers,
    stored_preference,
):
    owner = make_device("invalid-preference-warning")
    seed_mapping(owner, "primary", own=13517, save=False)
    seed_mapping(owner, "secondary", own=13518)
    if stored_preference == "malformed":
        seed_malformed_preference(owner)
    elif stored_preference is not None:
        seed_mapping(owner, stored_preference, preferred=True)
    original = mapping_summary(owner)
    _register_device(servers.primary, 13517, owner.name)
    client.force_login(make_superuser(f"invalid-preference-{stored_preference}-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    assert b'id="librenms-server-preference-warning"' in response.content
    assert b"Using installation default" in response.content
    owner.refresh_from_db()
    assert mapping_summary(owner) == original


@pytest.mark.django_db
def test_unconfigured_preferred_mapping_warns_and_falls_back_without_mutation(client, servers):
    owner = make_device("unconfigured-preference-warning")
    seed_mapping(owner, "primary", own=13526, save=False)
    seed_mapping(owner, "secondary", own=13527)
    seed_mapping(owner, "retired", own=13528, preferred=True)
    mapping = mapping_summary(owner)
    _register_device(servers.primary, 13526, owner.name)
    client.force_login(make_superuser("unconfigured-preference-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    assert b"not configured or usable" in response.content
    assert b"Using installation default server &#x27;primary&#x27;" in response.content
    owner.refresh_from_db()
    assert mapping_summary(owner) == mapping


@pytest.mark.django_db
def test_an_unusable_configured_mapping_shows_where_to_repair_it(client, settings, servers):
    """RemoveServerMappingView refuses a configured server, so the blocked page must point elsewhere."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"]["incomplete"] = {
        "display_name": "Incomplete LibreNMS",
        "librenms_url": servers.primary.url,
        "api_token": "",
        "verify_ssl": False,
    }
    settings.PLUGINS_CONFIG = plugin_config
    owner = seed_mapping(make_device("unusable-configured-mapping"), "incomplete", own=13540)
    mapping = mapping_summary(owner)
    client.force_login(make_superuser("unusable-configured-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Select a LibreNMS server to continue." in html
    assert 'id="librenms-server-selection-recovery"' in html
    assert reverse("plugins:netbox_librenms_plugin:settings") in html
    assert "Incomplete LibreNMS" in html
    assert "Repair this server in the plugin configuration" in html
    # The page must still refuse to run against a server this object is not mapped on.
    assert 'data-active-server-key=""' in html
    assert servers.primary.requests == []
    assert servers.secondary.requests == []
    owner.refresh_from_db()
    assert mapping_summary(owner) == mapping


@pytest.mark.django_db
def test_a_blocked_vc_member_links_to_the_sync_device_that_owns_the_mapping(client, settings, servers):
    """The mapping actions live on the sync device, so a blocked member page must link there."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"]["incomplete"] = {
        "display_name": "Incomplete LibreNMS",
        "librenms_url": servers.primary.url,
        "api_token": "",
        "verify_ssl": False,
    }
    settings.PLUGINS_CONFIG = plugin_config
    _chassis, (owner, viewed_member) = make_virtual_chassis_members("blocked-vc-owner", count=2)
    seed_mapping(owner, "incomplete", own=13541)
    client.force_login(make_superuser("blocked-vc-viewer"))

    response = client.get(_sync_url(viewed_member))

    assert response.status_code == 200
    html = response.content.decode()
    start = html.index('id="librenms-server-selection-recovery"')
    recovery_html = html[start : html.index("</div>", start)]
    assert _sync_url(owner) in recovery_html
    assert servers.primary.requests == []


@pytest.mark.django_db
def test_a_preference_without_any_mapping_reports_the_installation_default(client, servers):
    """The page already runs on the installation default here, so it must not ask for a selection."""
    owner = seed_mapping(make_device("preference-without-mapping"), "secondary", preferred=True)
    mapping = mapping_summary(owner)
    client.force_login(make_superuser("preference-without-mapping-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    body = response.content
    assert b"has no object mapping." in body
    assert b"Using installation default server &#x27;primary&#x27;." in body
    assert b"The installation default is not mapped, so select a server." not in body
    assert b"Select a LibreNMS server to continue." not in body
    owner.refresh_from_db()
    assert mapping_summary(owner) == mapping


@pytest.mark.django_db
def test_only_unusable_mapping_requires_selection_without_get_mutation(client, servers):
    owner = seed_mapping(make_device("unusable-preference-selection"), "retired", own=13529, preferred=True)
    mapping = mapping_summary(owner)
    client.force_login(make_superuser("unusable-preference-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    assert b"The installation default is not mapped, so select a server." in response.content
    assert b"Select a LibreNMS server to continue." in response.content
    owner.refresh_from_db()
    assert mapping_summary(owner) == mapping


@pytest.mark.django_db
@pytest.mark.parametrize("preferred_removed", [True, False])
def test_removing_mapping_clears_preference_when_removed_or_only_one_mapping_remains(
    client,
    servers,
    preferred_removed,
):
    preferred_key = "retired" if preferred_removed else "primary"
    owner = make_device(f"remove-preferred-{preferred_removed}")
    seed_mapping(owner, "primary", own=13519, save=False)
    seed_mapping(owner, "retired", own=13520, save=False)
    seed_mapping(owner, preferred_key, preferred=True)
    client.force_login(make_superuser(f"remove-preferred-{preferred_removed}-writer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[owner.pk]),
        {"object_type": "device", "server_key": "retired"},
    )

    assert response.status_code == 302
    owner.refresh_from_db()
    assert mapping_summary(owner) == {"primary": {"own": 13519}}


@pytest.mark.django_db
@pytest.mark.parametrize("stored_preference", ["secondary", "retired", "malformed"])
def test_preference_without_mappings_does_not_block_default_server(client, servers, stored_preference):
    """Orphaned preference metadata must leave an unmapped object usable."""
    from netbox_librenms_plugin.server_selection import ServerSelectionState, resolve_object_server

    device = make_device("preference-only-device")
    if stored_preference == "malformed":
        seed_malformed_preference(device)
    else:
        seed_mapping(device, stored_preference, preferred=True)
    expected = mapping_summary(device)
    client.force_login(make_superuser("preference-only-user"))
    response = client.get(_sync_url(device))
    assert response.status_code == 200
    selection = resolve_object_server(device)
    assert selection.state == ServerSelectionState.RESOLVED
    assert selection.active_key == "primary"
    device.refresh_from_db()
    assert mapping_summary(device) == expected
