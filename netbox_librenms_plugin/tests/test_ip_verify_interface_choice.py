"""IP verification uses the interface-name option of the rendered table."""

import pytest


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("interface_name_field", "can_create"),
    [("ifDescr", True), ("ifName", False), (None, False), ("unsupported", False), ([], False)],
)
def test_verify_uses_the_validated_interface_name_option(client, live_librenms, interface_name_field, can_create):
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser

    device = make_device("ip-verify-choice", librenms_cf={"default": {"id": 4501}})
    make_interface(device, "Ethernet1", iface_type="1000base-t")
    live_librenms.server.register(
        "/api/v0/devices/4501/ip",
        {"status": "ok", "addresses": [{"ipv4_address": "198.18.70.1", "ipv4_prefixlen": 24, "port_id": 7101}]},
    )
    live_librenms.server.register(
        "/api/v0/devices/4501/ports",
        {"status": "ok", "ports": [{"port_id": 7101, "ifName": "unmatched", "ifDescr": "Ethernet1"}]},
    )
    live_librenms.server.register("/api/v0/devices/4501", {"status": "ok", "devices": [{"device_id": 4501, "ip": ""}]})
    client.force_login(make_superuser("ip-verify-choice-user"))
    refreshed = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
        {"server_key": "default", "interface_name_field": "ifDescr"},
    )
    assert refreshed.status_code == 200

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_ipaddress"),
        {
            "device_id": device.pk,
            "object_type": "device",
            "ip_address": "198.18.70.1/24",
            "row_id": "198.18.70.1/24",
            "server_key": "default",
            "interface_name_field": interface_name_field,
        },
        content_type="application/json",
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "success"
    assert ('name="sync_one"' in result["formatted_row"]["status"]) == can_create
