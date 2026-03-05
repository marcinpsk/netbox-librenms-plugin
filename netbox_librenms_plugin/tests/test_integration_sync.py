"""Integration tests using the mock LibreNMS HTTP server.

These tests verify that LibreNMSAPI correctly parses responses from a real
(but local, mocked) HTTP server, and that the full request/response cycle works.
No Django database access is used; NetBox model interactions are mocked.
"""

import json
import pytest

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


@pytest.fixture
def mock_server():
    with librenms_mock_server() as server:
        yield server


def _make_api(url, token="test-token"):
    """Create a LibreNMSAPI instance pointed at the mock server."""
    from unittest.mock import patch

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    servers_config = {
        "test": {
            "librenms_url": url,
            "api_token": token,
            "cache_timeout": 0,
            "verify_ssl": False,
        }
    }

    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
        mock_cfg.side_effect = lambda _plugin, key: servers_config if key == "servers" else None
        api = LibreNMSAPI(server_key="test")

    api.librenms_url = url
    api.api_token = token
    return api


class TestMockServerSanity:
    """The mock server itself must start, serve, and stop cleanly."""

    def test_server_starts_and_responds(self, mock_server):
        import urllib.request

        mock_server.register("/api/v0/test", {"status": "ok"})
        with urllib.request.urlopen(f"{mock_server.url}/api/v0/test") as resp:
            data = json.loads(resp.read())
        assert data["status"] == "ok"

    def test_404_for_unregistered_path(self, mock_server):
        import urllib.request
        from urllib.error import HTTPError

        try:
            urllib.request.urlopen(f"{mock_server.url}/api/v0/nonexistent")
        except HTTPError as e:
            assert e.code == 404
        else:
            pytest.fail("Expected 404 HTTPError")


class TestLibreNMSAPIPortsFetch:
    """LibreNMSAPI.get_ports() correctly parses mock server responses."""

    def test_get_ports_returns_dict_with_ports_key(self, mock_server):
        mock_server.ports_response(device_id=1)
        api = _make_api(mock_server.url)

        success, data = api.get_ports(1)

        assert success is True
        assert isinstance(data, dict)
        assert "ports" in data
        assert data["ports"][0]["ifName"] == "GigabitEthernet0/1"

    def test_get_ports_returns_false_on_auth_error(self, mock_server):
        mock_server.auth_error_response(path="/api/v0/devices/1/ports")
        api = _make_api(mock_server.url)

        success, _ = api.get_ports(1)

        assert success is False

    def test_get_ports_empty_list_when_no_ports(self, mock_server):
        mock_server.register("/api/v0/devices/99/ports", {"status": "ok", "ports": []})
        api = _make_api(mock_server.url)

        success, data = api.get_ports(99)

        assert success is True
        assert data["ports"] == []


class TestLibreNMSAPIDeviceInfo:
    """LibreNMSAPI.get_device_info() correctly parses device details."""

    def test_returns_device_info_dict(self, mock_server):
        mock_server.device_info_response(device_id=5, hostname="rtr01", hardware="ISR4351")
        api = _make_api(mock_server.url)

        success, info = api.get_device_info(5)

        assert success is True
        assert isinstance(info, dict)
        assert info["hostname"] == "rtr01"

    def test_returns_false_on_404(self, mock_server):
        # /api/v0/devices/999 not registered → 404
        api = _make_api(mock_server.url)

        success, _ = api.get_device_info(999)

        assert success is False


class TestLibreNMSAPIAddDevice:
    """LibreNMSAPI.add_device() posts correctly and interprets the response."""

    def test_add_device_success(self, mock_server):
        mock_server.add_device_response(device_id=10)
        api = _make_api(mock_server.url)

        success, message = api.add_device(
            {
                "hostname": "switch1.example.com",
                "snmp_version": "v2c",
                "community": "public",
                "force_add": False,
            }
        )

        assert success is True

    def test_add_device_failure_on_server_error(self, mock_server):
        mock_server.register("/api/v0/devices", {"status": "error", "message": "duplicate"}, status=500)
        api = _make_api(mock_server.url)

        success, message = api.add_device(
            {
                "hostname": "dup.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert success is False


class TestLibreNMSAPIInventory:
    """LibreNMSAPI.get_device_inventory() correctly parses mock server responses."""

    def test_returns_inventory_list(self, mock_server):
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalDescr": "Chassis",
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "SN-CHASSIS-001",
                "entPhysicalModelName": "WS-C4900M",
                "entPhysicalName": "Chassis 1",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalDescr": "Linecard",
                "entPhysicalClass": "module",
                "entPhysicalSerialNum": "SN-CARD-002",
                "entPhysicalModelName": "WS-X4748-RJ45V+E",
                "entPhysicalName": "Slot 1",
                "entPhysicalContainedIn": 1,
            },
        ]
        mock_server.register("/api/v0/inventory/7/all", {"status": "ok", "inventory": inventory})
        api = _make_api(mock_server.url)

        success, data = api.get_device_inventory(7)

        assert success is True
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["entPhysicalClass"] == "chassis"
        assert data[1]["entPhysicalModelName"] == "WS-X4748-RJ45V+E"

    def test_returns_empty_list_when_no_inventory(self, mock_server):
        mock_server.register("/api/v0/inventory/99/all", {"status": "ok", "inventory": []})
        api = _make_api(mock_server.url)

        success, data = api.get_device_inventory(99)

        assert success is True
        assert data == []

    def test_returns_false_on_network_error(self, mock_server):
        # Unregistered path → 404 → raise_for_status → RequestException
        api = _make_api(mock_server.url)

        success, _ = api.get_device_inventory(404)

        assert success is False

    def test_inventory_items_preserve_all_fields(self, mock_server):
        inventory = [
            {
                "entPhysicalIndex": 5,
                "entPhysicalDescr": "10 Gigabit Ethernet Module",
                "entPhysicalClass": "module",
                "entPhysicalSerialNum": "JAE123XYZ",
                "entPhysicalModelName": "X2-10GB-LR",
                "entPhysicalName": "TenGigabitEthernet1/1",
                "entPhysicalContainedIn": 1,
                "entPhysicalParentRelPos": 1,
            }
        ]
        mock_server.register("/api/v0/inventory/3/all", {"status": "ok", "inventory": inventory})
        api = _make_api(mock_server.url)

        success, data = api.get_device_inventory(3)

        assert success is True
        item = data[0]
        assert item["entPhysicalParentRelPos"] == 1
        assert item["entPhysicalSerialNum"] == "JAE123XYZ"
