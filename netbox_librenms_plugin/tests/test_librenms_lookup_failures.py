"""
A failed LibreNMS lookup must say which failure it was.

A device that LibreNMS does not have, a device LibreNMS errors on, and a server that cannot be
reached are three different problems with three different fixes. Reporting all of them as
"device not found" sends the user to remove a custom field that is correct.
"""

from html import unescape

import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_device,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.mapping_fixtures import mapping_summary, seed_mapping

ABSENT_DEVICE_ID = 4041
ERRORING_DEVICE_ID = 1255
CONFLICTING_DEVICE_ID = 1266
AMBIGUOUS_DEVICE_ID = 1277


def _point_plugin_at(settings, url):
    """Configure one server and return the key, so no test hardcodes an environment's key."""
    configure_librenms_servers(
        settings, {"default": {"librenms_url": url, "api_token": "test-token", "verify_ssl": False}}
    )
    return "default"


def _api_for(server_key):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    return LibreNMSAPI(server_key=server_key)


@pytest.mark.django_db
def test_a_missing_device_is_still_reported_as_missing(librenms_server, settings):
    """The existing contract: LibreNMS answering 404 means the device is genuinely absent."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ABSENT_DEVICE_ID}",
        {"status": "error", "message": "Device does not exist"},
        status=404,
    )

    assert _api_for(server_key).get_device_info(ABSENT_DEVICE_ID) == (False, None)


@pytest.mark.django_db
def test_a_server_error_is_not_reported_as_a_missing_device(librenms_server, settings):
    """LibreNMS answering 500 says nothing about whether the device exists."""
    from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ERRORING_DEVICE_ID}",
        {"message": "Server Error"},
        status=500,
    )

    success, failure = _api_for(server_key).get_device_info(ERRORING_DEVICE_ID)

    assert success is False
    assert isinstance(failure, LibreNMSLookupError)
    assert failure.status_code == 500


@pytest.mark.django_db
def test_an_unreachable_server_is_not_reported_as_a_missing_device(librenms_server, settings):
    """Nothing answered at all, so the device's existence is unknown rather than denied."""
    from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

    # Take a port that was really bound, then stop listening on it, so the refusal is not a
    # guess about which ports happen to be free.
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.stop()

    success, failure = _api_for(server_key).get_device_info(ERRORING_DEVICE_ID)

    assert success is False
    assert isinstance(failure, LibreNMSLookupError)
    assert failure.status_code is None, "an HTTP answer arrived, so this did not test a transport failure"


@pytest.mark.django_db
def test_the_sync_page_does_not_tell_the_user_to_clear_a_correct_custom_field(client, librenms_server, settings):
    """End to end: the page must not blame the custom field for a LibreNMS server error."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ERRORING_DEVICE_ID}",
        {"message": "Server Error"},
        status=500,
    )
    device = seed_mapping(make_device("librenms-500-device"), server_key, own=ERRORING_DEVICE_ID)
    client.force_login(make_superuser("librenms-500-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]))
    body = response.content.decode()

    assert response.status_code == 200
    assert "LibreNMS lookup failed" in body
    assert str(ERRORING_DEVICE_ID) in body
    # Every place the page could still call the device missing, not just the banner.
    assert "Device not found" not in body, "a server error was reported as a missing device"
    assert "Remove the custom field value" not in body, "the page told the user to clear a correct value"
    assert "Not found" not in body, "the status row still called the device missing"
    # The control itself, not the phrase: the phrase also appears in a template comment.
    assert 'data-bs-target="#add-device-modal"' not in body, "the page offered to add a device it could not look up"
    assert 'id="add-device-modal"' not in body, "the add-device modal was rendered for a failed lookup"


@pytest.mark.django_db
def test_the_sync_page_still_reports_a_genuinely_missing_device(client, librenms_server, settings):
    """The 404 path must keep its message, including the advice to clear the custom field."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ABSENT_DEVICE_ID}",
        {"status": "error", "message": "Device does not exist"},
        status=404,
    )
    device = seed_mapping(make_device("librenms-404-device"), server_key, own=ABSENT_DEVICE_ID)
    client.force_login(make_superuser("librenms-404-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]))
    body = response.content.decode()

    assert response.status_code == 200
    assert "Device not found" in body
    assert "Remove the custom field value" in body


def _match_by_name(server, target, librenms_id):
    """Make the LibreNMS fake answer a lookup of *target* by its name with *librenms_id*."""
    server.register(
        f"/api/v0/devices/{target.name}",
        {"status": "ok", "devices": [{"device_id": librenms_id}]},
        method="GET",
    )


@pytest.mark.django_db
def test_the_sync_page_names_the_owner_of_a_matched_id_and_offers_no_link(client, librenms_server, settings):
    """A match that another object owns is shown with its owner, and nothing is stored."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    owner = seed_mapping(make_device("librenms-conflict-owner"), server_key, own=CONFLICTING_DEVICE_ID)
    target = make_device("librenms-conflict-target.example.com")
    _match_by_name(librenms_server, target, CONFLICTING_DEVICE_ID)
    client.force_login(make_superuser("librenms-conflict-page-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[target.pk]))
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert response.context["link"]["state"] == "owned"
    assert response.context["link"]["token"] is None
    assert f"device '{owner.name}'" in body
    target.refresh_from_db()
    assert mapping_summary(target) == {}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name",
    ["update_device_name", "update_device_serial", "update_device_type", "update_device_platform"],
)
def test_the_device_field_handlers_refuse_an_unlinked_device_without_asking_librenms(
    client, librenms_server, settings, url_name
):
    """A device-field sync reads the stored link only, so a device that LibreNMS would match is still refused."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    target = make_device("librenms-field-unlinked-target.example.com")
    _match_by_name(librenms_server, target, CONFLICTING_DEVICE_ID)
    client.force_login(make_superuser("librenms-field-unlinked-user"))

    response = client.post(
        reverse(f"plugins:netbox_librenms_plugin:{url_name}", args=[target.pk]),
        {"server_key": server_key},
    )
    rendered_messages = [str(message) for message in get_messages(response.wsgi_request)]

    assert response.status_code == 302
    assert rendered_messages == [f"Not linked to {server_key}."]
    assert librenms_server.requests == []
    target.refresh_from_db()
    assert mapping_summary(target) == {}


@pytest.mark.django_db
def test_a_match_owner_outside_the_view_scope_is_not_named(client, librenms_server, settings):
    """The owner search is unrestricted, so naming its result would disclose an unviewable object."""
    from dcim.models import Device

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    server_key = _point_plugin_at(settings, librenms_server.url)
    owner = seed_mapping(make_device("librenms-hidden-conflict-owner"), server_key, own=CONFLICTING_DEVICE_ID)
    target = make_device("librenms-hidden-conflict-target.example.com")
    _match_by_name(librenms_server, target, CONFLICTING_DEVICE_ID)
    # Device view is constrained to the target, so this user cannot see the owner at all.
    viewer = make_user_with_perms("librenms-hidden-conflict-viewer", [("view", Device)], constraints={"pk": target.pk})
    client.force_login(viewer)

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[target.pk]))
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert response.context["link"]["state"] == "owned"
    assert owner.name not in body, "the page named an object this user may not view"


@pytest.mark.django_db
def test_a_match_of_an_id_with_two_owners_names_neither_and_offers_no_link(client, librenms_server, settings):
    """Two NetBox owners of one LibreNMS ID make the match owned; the page names no owner and no pk."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    owner_a = seed_mapping(make_device("librenms-ambiguous-owner-a"), server_key, own=AMBIGUOUS_DEVICE_ID)
    owner_b = seed_mapping(make_device("librenms-ambiguous-owner-b"), server_key, own=AMBIGUOUS_DEVICE_ID)
    target = make_device("librenms-ambiguous-target.example.com")
    _match_by_name(librenms_server, target, AMBIGUOUS_DEVICE_ID)
    client.force_login(make_superuser("librenms-ambiguous-page-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[target.pk]))
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert response.context["link"]["state"] == "owned"
    assert response.context["link"]["token"] is None
    for owner in (owner_a, owner_b):
        assert owner.name not in body
        assert f"pk={owner.pk}" not in body


@pytest.mark.django_db
def test_a_location_update_of_an_unlinked_device_makes_no_librenms_request(client, librenms_server, settings):
    """A POST action on an unlinked device redirects with the refusal before any LibreNMS request."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    target = make_device("librenms-action-unlinked-target.example.com")
    _match_by_name(librenms_server, target, CONFLICTING_DEVICE_ID)
    client.force_login(make_superuser("librenms-unlinked-action-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:update_device_location", args=[target.pk]),
        {"server_key": server_key},
    )

    assert response.status_code == 302
    assert [str(message) for message in get_messages(response.wsgi_request)] == [f"Not linked to {server_key}."]
    assert librenms_server.requests == []


@pytest.mark.django_db
@pytest.mark.parametrize(("object_type", "url_name"), [("device", "device_status_list"), ("vm", "vm_status_list")])
def test_status_lists_show_an_owned_match_without_naming_a_hidden_owner(
    client, librenms_server, settings, object_type, url_name
):
    """Two rows that match an owned ID show it as owned; the list names the owner only to a viewer of it."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    server_key = _point_plugin_at(settings, librenms_server.url)
    prefix = f"duplicate-{object_type}-conflict"
    if object_type == "device":
        model, owner = (
            Device,
            seed_mapping(make_device("duplicate-device-owner"), server_key, own=CONFLICTING_DEVICE_ID),
        )
        targets = [make_device(f"{prefix}-{suffix}") for suffix in ("a", "b")]
    else:
        model, owner = VirtualMachine, make_vm("duplicate-vm-owner")
        seed_mapping(owner, server_key, own=CONFLICTING_DEVICE_ID, save=False)
        owner.save()
        targets = [make_vm(f"{prefix}-{suffix}") for suffix in ("a", "b")]
    for target in targets:
        _match_by_name(librenms_server, target, CONFLICTING_DEVICE_ID)
    viewer = make_user_with_perms(f"{prefix}-viewer", [("view", model)], constraints={"name__startswith": prefix})
    client.force_login(viewer)

    response = client.get(reverse(f"plugins:netbox_librenms_plugin:{url_name}"), {"q": prefix})
    rows = response.context["table"].link_rows

    assert response.status_code == 200
    assert [rows[target.pk].state for target in targets] == ["owned", "owned"]
    assert owner.name not in response.content.decode()


@pytest.mark.django_db
def test_a_cable_refresh_of_an_oob_only_device_reads_the_oob_rows_without_matching(
    client,
    librenms_server,
    settings,
):
    """An OOB link alone acquires cables; the host is not looked up, so a matching LibreNMS device is not asked."""
    oob_id = 1277
    server_key = _point_plugin_at(settings, librenms_server.url)
    target = seed_mapping(make_device("cable-oob-only-target.example.test"), server_key, oob=oob_id, oob_type="bmc")
    _match_by_name(librenms_server, target, CONFLICTING_DEVICE_ID)
    librenms_server.register(
        f"/api/v0/devices/{oob_id}/links",
        {
            "status": "ok",
            "links": [
                {
                    "local_port_id": 71,
                    "local_port": "console0",
                    "remote_port": "console1",
                    "remote_hostname": "peer.example.test",
                    "remote_port_id": 72,
                    "remote_device_id": 1278,
                }
            ],
        },
        method="GET",
    )
    librenms_server.register(
        f"/api/v0/devices/{oob_id}/ports",
        {"status": "ok", "ports": [{"port_id": 71, "ifName": "console0", "ifDescr": "console0"}]},
        method="GET",
    )
    client.force_login(make_superuser("cable-oob-only-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[target.pk]),
        {"server_key": server_key},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert not any(request["path"] == f"/api/v0/devices/{target.name}" for request in librenms_server.requests)
    assert cache.get(sync_snapshot_key(target, TAB_SPECS[SyncTab.CABLES].source.data_type, server_key)) is not None
    target.refresh_from_db()
    assert mapping_summary(target) == {server_key: {"oob": oob_id, "oob_type": "bmc"}}
