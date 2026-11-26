"""
Server-key scoping for the device-field update views, driven by real plugin settings.

``UpdateDeviceNameView.post`` rebinds the LibreNMS client to the POSTed ``server_key``
before it reads the ID of the Virtual-Chassis master. The ID must come from that same
server, otherwise a multi-server VC member is renamed from the WRONG LibreNMS device.

These tests drive the real owner rule against a real DB Virtual Chassis, the real
``settings.PLUGINS_CONFIG`` path, and a loopback LibreNMS server.
"""

import copy

import pytest
from django.conf import settings
from django.test import override_settings

from netbox_librenms_plugin.tests.conftest import make_device, map_device_to_librenms
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_request
from netbox_librenms_plugin.tests.view_test_helpers import post as _post


TWO_SERVERS = {
    "default": {"librenms_url": "https://default.example.com", "api_token": "default-token"},
    "siteB": {"librenms_url": "https://siteb.example.com", "api_token": "siteb-token"},
}


@pytest.fixture
def librenms_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        yield server


def _plugins_config_with_servers(servers):
    """Return a PLUGINS_CONFIG copy with the plugin's ``servers`` set to ``servers``."""
    config = copy.deepcopy(settings.PLUGINS_CONFIG)
    plugin_config = dict(config.get("netbox_librenms_plugin", {}))
    plugin_config["servers"] = servers
    config["netbox_librenms_plugin"] = plugin_config
    return config


@pytest.mark.django_db
class TestUpdateDeviceNameServerScoping:
    def test_vc_master_mapping_resolved_for_posted_server(self, librenms_server):
        """
        A POST scoped to ``siteB`` must read the siteB ID of the chassis master, not its default ID.

        VC layout (the viewed member has no own librenms_id):
          * master -> {"default": 10, "siteB": 20}
        """
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        vc = VirtualChassis.objects.create(name="vcscope")
        master = make_device("vcscope-a-master")
        master.virtual_chassis = vc
        master.vc_position = 1
        master.save()
        map_device_to_librenms(master, 10, server_key="default")
        map_device_to_librenms(master, 20, server_key="siteB")
        vc.master = master
        vc.save()

        viewed = make_device("vcscope-c-viewed")
        viewed.virtual_chassis = vc
        viewed.vc_position = 3
        viewed.save()  # no librenms_id -> takes the name from the chassis master

        servers = {key: {"librenms_url": librenms_server.url, "api_token": "test-token"} for key in TWO_SERVERS}
        librenms_server.device_info_response(device_id=20, hostname="siteb-source", serial="SITEB20")
        request = make_request("post", {"server_key": "siteB"})

        with override_settings(PLUGINS_CONFIG=_plugins_config_with_servers(servers)):
            response = _post(UpdateDeviceNameView(), request, pk=viewed.pk)

        assert response.status_code == 302
        assert [entry["path"] for entry in librenms_server.requests] == ["/api/v0/devices/20"]
        viewed.refresh_from_db()
        assert viewed.name == "siteb-source-M3"
