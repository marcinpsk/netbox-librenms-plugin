"""Cable blocker alerts appear once in a partial and survive a redirect."""

import pytest


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["refresh", "sync-partial", "sync-redirect"])
def test_cable_blockers_use_the_notification_channel_of_the_response(client, live_librenms, action):
    from django.contrib import messages
    from django.contrib.messages import get_messages
    from django.urls import reverse

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
    from netbox_librenms_plugin.tests.view_test_helpers import make_request

    device = make_device(
        "cable-blocker-channels",
        librenms_cf={"default": {"id": 5501, "oob": {"id": 5502, "type": "idrac"}}},
    )
    user = make_superuser("cable-blocker-channels-user")
    server = live_librenms.server
    server.register("/api/v0/devices/5501/links", {"status": "ok", "links": []})
    server.register("/api/v0/devices/5501/ports", {"status": "ok", "ports": []})
    server.register("/api/v0/devices/5502/links", {"status": "ok", "links": []})
    server.register("/api/v0/devices/5502/ports", {"status": "error"}, status=500)
    source_cache.refresh(make_request("post", user=user), device, Resource.CABLES, "default")
    client.force_login(user)
    route = "device_cable_sync" if action == "refresh" else "sync_device_cables"
    headers = {} if action == "sync-redirect" else {"HTTP_HX_REQUEST": "true"}

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:" + route, args=[device.pk]),
        {"server_key": "default"},
        **headers,
    )

    warnings = [message for message in get_messages(response.wsgi_request) if message.level == messages.WARNING]
    if action == "sync-redirect":
        assert response.status_code == 302
        assert len(warnings) == 1
    else:
        assert response.status_code == 200
        assert len(response.context["cable_sync"]["blockers"]) == 1
        assert not warnings
        assert response.content.count(b'class="alert alert-warning"') == 1
