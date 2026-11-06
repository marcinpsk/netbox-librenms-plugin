"""The IP row read uses the interface-name option of the rendered table."""

import pytest


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("interface_name_field", "can_create"),
    [("ifDescr", True), ("ifName", False), (None, False), ("unsupported", False)],
)
def test_the_row_read_uses_the_validated_interface_name_option(client, live_librenms, interface_name_field, can_create):
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
    from netbox_librenms_plugin.tests.view_test_helpers import read_ip_row

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

    query = {} if interface_name_field is None else {"interface_name_field": interface_name_field}

    row = read_ip_row(client, device, "198.18.70.1/24", **query)

    assert ('name="sync_one"' in row.status) == can_create
