"""Tests for device mismatch detection in get_librenms_device_info.

Covers the hostname/IP matching logic that determines whether a
mismatched_device warning is shown on the LibreNMS Sync page.
"""

from unittest.mock import MagicMock, patch


def _make_view(librenms_id, device_info, librenms_url="https://librenms.example.com"):
    """Create a minimal BaseLibreNMSSyncView instance with mocked dependencies."""
    from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

    view = object.__new__(BaseLibreNMSSyncView)
    view.librenms_id = librenms_id
    api = MagicMock()
    api.librenms_url = librenms_url
    api.get_device_info.return_value = (True, device_info)
    api.get_device_inventory.return_value = (True, [])
    view._librenms_api = api
    return view


def _make_obj(name, primary_ip=None, virtual_chassis=None, cf=None):
    """Create a mock NetBox device object."""
    obj = MagicMock()
    obj.name = name
    obj.cf = cf or {}
    if primary_ip:
        obj.primary_ip = MagicMock()
        obj.primary_ip.address.ip = primary_ip
    else:
        obj.primary_ip = None
    obj.virtual_chassis = virtual_chassis
    return obj


class TestMismatchDetection:
    """Tests for hostname/IP mismatch detection logic."""

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_librenms_id_returns_not_found(self, mock_hw):
        """No librenms_id means device is not found."""
        view = _make_view(librenms_id=None, device_info=None)
        result = view.get_librenms_device_info(_make_obj("sw01"))

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_api_failure_returns_not_found(self, mock_hw):
        """API failure (success=False) means device is not found."""
        view = _make_view(librenms_id=42, device_info=None)
        view.librenms_api.get_device_info.return_value = (False, None)
        result = view.get_librenms_device_info(_make_obj("sw01"))

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_ip_match_no_mismatch(self, mock_hw):
        """Matching IP addresses — no mismatch."""
        view = _make_view(42, {"sysName": "different-name", "ip": "10.0.0.1"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_exact_hostname_match(self, mock_hw):
        """Exact hostname match (case-insensitive) — no mismatch."""
        view = _make_view(42, {"sysName": "SW01", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_fqdn_match(self, mock_hw):
        """Full FQDN match — no mismatch."""
        view = _make_view(42, {"sysName": "sw01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01.example.net", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_fqdn_domain_differs_is_mismatch(self, mock_hw):
        """Different FQDN domains — mismatch detected."""
        view = _make_view(42, {"sysName": "sw01.other.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01.example.net", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_short_vs_fqdn_is_mismatch(self, mock_hw):
        """Short name vs FQDN — mismatch (strict comparison)."""
        view = _make_view(42, {"sysName": "sw01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_fqdn_vs_short_is_mismatch(self, mock_hw):
        """FQDN vs short name — mismatch (strict comparison)."""
        view = _make_view(42, {"sysName": "sw01", "ip": "10.0.0.2"})
        obj = _make_obj("sw01.example.net", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_completely_different_names_is_mismatch(self, mock_hw):
        """Completely different hostnames — mismatch."""
        view = _make_view(42, {"sysName": "router-01", "ip": "10.0.0.2"})
        obj = _make_obj("switch-05", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_found_in_librenms_always_true_with_valid_id(self, mock_hw):
        """found_in_librenms is True even when hostname mismatches."""
        view = _make_view(42, {"sysName": "totally-different", "ip": "10.0.0.2"})
        obj = _make_obj("my-device", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_vc_suffix_stripped(self, mock_hw):
        """VC member suffix ' (1)' is stripped before comparison."""
        view = _make_view(42, {"sysName": "switch-1", "ip": "10.0.0.2"})
        obj = _make_obj("switch-1 (1)", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_vc_base_hostname_match(self, mock_hw):
        """VC member with explicit librenms_id matches base hostname."""
        vc = MagicMock()
        view = _make_view(42, {"sysName": "switch-1", "ip": "10.0.0.2"})
        obj = _make_obj("switch-2 (2)", primary_ip="10.0.0.1", virtual_chassis=vc, cf={"librenms_id": 42})
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_vc_different_base_hostname_is_mismatch(self, mock_hw):
        """VC member with completely different base hostname — mismatch."""
        vc = MagicMock()
        view = _make_view(42, {"sysName": "router-1", "ip": "10.0.0.2"})
        obj = _make_obj("switch-2 (2)", primary_ip="10.0.0.1", virtual_chassis=vc, cf={"librenms_id": 42})
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_netbox_hostname(self, mock_hw):
        """No NetBox hostname — mismatch (cannot compare)."""
        view = _make_view(42, {"sysName": "sw01", "ip": "10.0.0.2"})
        obj = _make_obj(None, primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_librenms_sysname(self, mock_hw):
        """No LibreNMS sysName — mismatch (cannot compare)."""
        view = _make_view(42, {"sysName": None, "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_ip_no_hostname_both_none(self, mock_hw):
        """No IP and no hostnames — None==None IP match, no mismatch."""
        view = _make_view(42, {"sysName": None, "ip": None})
        obj = _make_obj(None, primary_ip=None)
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False
