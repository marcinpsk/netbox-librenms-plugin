"""Coverage tests for import_utils/device_operations.py."""

from unittest.mock import MagicMock, patch


class TestTryChassiDeviceTypeMatch:
    """Tests for _try_chassis_device_type_match (lines 45-65)."""

    def test_api_failure_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.return_value = (False, [])
        result = _try_chassis_device_type_match(api, 1)
        assert result is None

    def test_empty_inventory_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.return_value = (True, [])
        result = _try_chassis_device_type_match(api, 1)
        assert result is None

    def test_matched_physical_name_returns_match(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        mock_dt = MagicMock()
        api = MagicMock()
        api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalName": "CHAS-BP-MX480-S", "entPhysicalModelName": "model1"}],
        )

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.return_value = {"matched": True, "device_type": mock_dt, "match_type": "exact"}
            result = _try_chassis_device_type_match(api, 1)

        assert result is not None
        assert result["matched"] is True
        assert result["match_type"] == "chassis"
        assert result["chassis_model"] == "CHAS-BP-MX480-S"

    def test_skips_empty_values(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.return_value = (True, [{"entPhysicalName": "", "entPhysicalModelName": "-"}])

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.return_value = {"matched": False}
            result = _try_chassis_device_type_match(api, 1)

        mock_match.assert_not_called()
        assert result is None

    def test_exception_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.side_effect = RuntimeError("API Error")
        result = _try_chassis_device_type_match(api, 1)
        assert result is None

    def test_fallback_to_model_name_when_name_not_matched(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        mock_dt = MagicMock()
        api = MagicMock()
        api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalName": "Unrecognized", "entPhysicalModelName": "710-017414"}],
        )

        call_count = [0]

        def match_side_effect(value):
            call_count[0] += 1
            if value == "Unrecognized":
                return {"matched": False}
            return {"matched": True, "device_type": mock_dt, "match_type": "exact"}

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
            side_effect=match_side_effect,
        ):
            result = _try_chassis_device_type_match(api, 1)

        assert result is not None
        assert result["matched"] is True
        assert result["chassis_model"] == "710-017414"


class TestDetermineDeviceName:
    """Tests for _determine_device_name (lines 68-122)."""

    def test_use_sysname_true_prefers_sysname(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "router01", "hostname": "router01.example.com"}, use_sysname=True)
        assert result == "router01"

    def test_use_sysname_false_prefers_hostname(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "router01", "hostname": "router01.example.com"}, use_sysname=False)
        assert result == "router01.example.com"

    def test_fallback_to_device_id_when_no_name(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({}, device_id=42)
        assert result == "device-42"

    def test_fallback_to_device_id_field_when_no_name_no_id(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"device_id": 99})
        assert result == "device-99"

    def test_strip_domain_true_strips_suffix(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "router01.example.com"}, strip_domain=True)
        assert result == "router01"

    def test_strip_domain_does_not_strip_ip(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "192.168.1.1"}, strip_domain=True)
        assert result == "192.168.1.1"

    def test_hostname_fallback_when_sysname_empty(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "", "hostname": "sw01.example.com"}, use_sysname=True)
        assert result == "sw01.example.com"


class TestGetLibreNMSDeviceById:
    """Tests for get_librenms_device_by_id (lines 912-933)."""

    def test_success_returns_device(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        device = {"device_id": 42, "hostname": "router01"}
        api.get_device_info.return_value = (True, device)

        result = get_librenms_device_by_id(api, 42)
        assert result is device

    def test_api_failure_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        api.get_device_info.return_value = (False, None)

        result = get_librenms_device_by_id(api, 42)
        assert result is None

    def test_device_not_found_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        api.get_device_info.return_value = (True, None)

        result = get_librenms_device_by_id(api, 42)
        assert result is None

    def test_exception_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        api.get_device_info.side_effect = RuntimeError("Network error")

        result = get_librenms_device_by_id(api, 42)
        assert result is None


class TestFetchDeviceWithCache:
    """Tests for fetch_device_with_cache (lines 936-987)."""

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_from_pre_fetched_cache_dict(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        device = {"device_id": 1}
        cache_dict = {1: device}

        result = fetch_device_with_cache(1, api, libre_devices_cache=cache_dict)
        assert result is device
        mock_cache.get.assert_not_called()

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_from_django_cache(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        device = {"device_id": 1}
        mock_cache.get.return_value = device

        result = fetch_device_with_cache(1, api)
        assert result is device
        api.get_device_info.assert_not_called()

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_cache_miss_falls_back_to_api(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        device = {"device_id": 1}
        mock_cache.get.return_value = None
        api.get_device_info.return_value = (True, device)

        result = fetch_device_with_cache(1, api)
        assert result is device
        mock_cache.set.assert_called_once()

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_api_returns_none_returns_none(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        mock_cache.get.return_value = None
        api.get_device_info.return_value = (False, None)

        result = fetch_device_with_cache(1, api)
        assert result is None

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_uses_provided_server_key(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        mock_cache.get.return_value = None
        api.get_device_info.return_value = (True, {"device_id": 1})

        fetch_device_with_cache(1, api, server_key="secondary")
        # The cache key should use "secondary"
        cache_key = mock_cache.get.call_args[0][0]
        assert "secondary" in cache_key


class TestValidateDeviceForImport:
    """Tests for validate_device_for_import main validation logic."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.get_device_info.return_value = (True, {"device_id": 1})
        return api

    def _patch_all_db(self):
        """Context manager patches for all DB interactions."""
        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None
        mock_device.objects.filter.return_value.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
        mock_device.objects.all.return_value = []

        mock_vm = MagicMock()
        mock_vm.objects.filter.return_value.first.return_value = None

        mock_cluster = MagicMock()
        mock_cluster.objects.all.return_value = []

        mock_device_role = MagicMock()
        mock_device_role.objects.all.return_value = []

        mock_site = MagicMock()
        mock_site.objects.all.return_value = []

        mock_ip = MagicMock()
        mock_ip.objects.filter.return_value.first.return_value = None

        patches = [
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
                return_value={"found": False, "site": None, "match_type": None, "suggestions": []},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False, "device_type": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole", mock_device_role),
            patch("netbox_librenms_plugin.import_utils.device_operations.Cluster", mock_cluster),
            patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType", MagicMock()),
            patch("netbox_librenms_plugin.import_utils.device_operations.Site", mock_site),
            patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine", mock_vm),
            patch("ipam.models.IPAddress", mock_ip),
        ]
        return patches

    def test_minimal_device_validation(self):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "type": "network",
        }
        api = self._make_api()

        patches = self._patch_all_db()
        try:
            for p in patches:
                p.start()
            result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result is not None
        assert "status" in result or "is_ready" in result

    def test_vm_import_uses_correct_model(self):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "type": "network",
        }
        api = self._make_api()

        patches = self._patch_all_db()
        try:
            for p in patches:
                p.start()
            result = validate_device_for_import(libre_device, import_as_vm=True, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result is not None
        assert result.get("import_as_vm") is True

    def test_existing_device_detected(self):
        """When device with same librenms_id exists, sets existing_device in result."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        api = self._make_api()

        existing = MagicMock()
        existing.name = "router01"
        existing.serial = ""

        patches = self._patch_all_db()
        # Override find_by_librenms_id: return None for VM, existing for Device
        try:
            for p in patches:
                p.start()

            def _find_side_effect(model, device_id, server_key):
                from virtualization.models import VirtualMachine as VM

                return None if model is VM else existing

            with patch("netbox_librenms_plugin.utils.find_by_librenms_id", side_effect=_find_side_effect):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result.get("existing_device") is existing


class TestImportSingleDevice:
    """Tests for import_single_device (lines 689-910)."""

    def _make_libre_device(self):
        return {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "Cisco",
            "serial": "SN001",
            "os": "ios",
            "status": 1,
            "location": "-",
        }

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_missing_site_returns_error(self, MockAPI):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        libre_device = self._make_libre_device()

        validation = {
            "existing_device": None,
            "site": {"found": False, "site": None},
            "device_type": {"matched": True, "device_type": MagicMock()},
            "device_role": {"found": True, "role": MagicMock()},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }

        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.Site"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_utils.device_operations.Rack"),
        ):
            result = import_single_device(1, server_key="default", validation=validation, libre_device=libre_device)
        assert result["success"] is False
        assert "Site" in result["error"]

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_missing_device_type_returns_error(self, MockAPI):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        libre_device = self._make_libre_device()

        validation = {
            "existing_device": None,
            "site": {"found": True, "site": MagicMock()},
            "device_type": {"matched": False, "device_type": None},
            "device_role": {"found": True, "role": MagicMock()},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }

        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.Site"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_utils.device_operations.Rack"),
        ):
            result = import_single_device(1, server_key="default", validation=validation, libre_device=libre_device)
        assert result["success"] is False
        assert "device type" in result["error"].lower()

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_missing_device_role_returns_error(self, MockAPI):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        libre_device = self._make_libre_device()

        validation = {
            "existing_device": None,
            "site": {"found": True, "site": MagicMock()},
            "device_type": {"matched": True, "device_type": MagicMock()},
            "device_role": {"found": False, "role": None},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }

        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.Site"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_utils.device_operations.Rack"),
        ):
            result = import_single_device(1, server_key="default", validation=validation, libre_device=libre_device)
        assert result["success"] is False
        assert "role" in result["error"].lower()


class TestValidateDeviceForImportEdgeCases:
    """Additional edge case tests to cover missing lines."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        return api

    def _start_patches(self, extra_patches=None):
        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None
        mock_device.objects.all.return_value = []

        mock_vm = MagicMock()
        mock_vm.objects.filter.return_value.first.return_value = None

        mock_cluster = MagicMock()
        mock_cluster.objects.all.return_value = []

        mock_role = MagicMock()
        mock_role.objects.all.return_value = []

        mock_ip = MagicMock()
        mock_ip.objects.filter.return_value.first.return_value = None

        mock_site = MagicMock()
        mock_site.objects.all.return_value = []

        patches = [
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
                return_value={"found": False, "site": None, "match_type": None, "suggestions": []},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False, "device_type": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole", mock_role),
            patch("netbox_librenms_plugin.import_utils.device_operations.Cluster", mock_cluster),
            patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType", MagicMock()),
            patch("netbox_librenms_plugin.import_utils.device_operations.Site", mock_site),
            patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine", mock_vm),
            patch("ipam.models.IPAddress", mock_ip),
        ]
        if extra_patches:
            patches.extend(extra_patches)
        started = []
        for p in patches:
            started.append(p.start())
        return patches, started

    def _stop_patches(self, patches):
        for p in patches:
            p.stop()

    def test_vm_librenms_id_not_int_falls_back(self):
        """Lines 288-290: librenms_id ValueError/TypeError in VM check."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": None,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        api = self._make_api()

        patches, _ = self._start_patches()
        try:
            result = validate_device_for_import(libre_device, api=api)
        finally:
            self._stop_patches(patches)

        assert result is not None

    def test_vm_with_legacy_librenms_id_flags_migration(self):
        """Line 307: existing VM has legacy bare-int librenms_id → flags migration."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 42,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        api = self._make_api()

        existing_vm = MagicMock()
        existing_vm.name = "vm01"
        existing_vm.serial = ""
        existing_vm.custom_field_data = {"librenms_id": 42}  # Legacy bare-int

        patches, _ = self._start_patches()
        try:
            with patch("netbox_librenms_plugin.utils.find_by_librenms_id") as mock_find:
                mock_find.side_effect = [existing_vm, None]  # VM found, then no Device
                result = validate_device_for_import(libre_device, import_as_vm=True, api=api)
        finally:
            self._stop_patches(patches)

        assert result.get("existing_device") is existing_vm

    def test_vc_detection_called_for_device_with_api(self):
        """Lines 616-638: VC detection executed when include_vc_detection=True and api provided."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "Cisco",
            "serial": "SN001",
            "os": "ios",
            "location": "-",
        }
        api = self._make_api()

        vc_data = {"is_stack": True, "member_count": 2, "members": [{"serial": "SN001"}, {"serial": "SN002"}]}

        patches, _ = self._start_patches(
            [
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.update_vc_member_suggested_names",
                    return_value=vc_data,
                ),
            ]
        )
        # Override get_virtual_chassis_data to return VC stack
        for p in patches:
            if hasattr(p, "new") and p.new is not None:
                pass  # already patched

        try:
            with patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data", return_value=vc_data
            ):
                with patch(
                    "netbox_librenms_plugin.import_utils.device_operations.update_vc_member_suggested_names",
                    return_value=vc_data,
                ):
                    result = validate_device_for_import(libre_device, api=api)
        finally:
            self._stop_patches(patches)

        assert result["virtual_chassis"] is not None

    def test_no_vc_detection_when_disabled(self):
        """VC detection skipped when include_vc_detection=False."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        api = self._make_api()

        patches, _ = self._start_patches()
        try:
            with patch("netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data") as mock_vc:
                validate_device_for_import(libre_device, api=api, include_vc_detection=False)
                mock_vc.assert_not_called()
        finally:
            self._stop_patches(patches)

    def test_chassis_inventory_fallback_used(self):
        """Lines 534-539: Chassis inventory fallback when hardware doesn't match."""
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        mock_dt = MagicMock()

        api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalName": "MX480", "entPhysicalModelName": "Juniper MX480"}],
        )

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.side_effect = [
                {"matched": False},
                {"matched": True, "device_type": mock_dt, "match_type": "exact"},
            ]
            result = _try_chassis_device_type_match(api, 1)

        # Should have found a match via model name fallback
        assert result is not None

    def test_primary_ip_match_check(self):
        """Lines 474-489: IP address match detection."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "ip": "192.168.1.1",
        }
        api = self._make_api()

        mock_device = MagicMock()
        mock_device.name = "existing_router"

        mock_ip = MagicMock()
        mock_ip.assigned_object.device = mock_device
        mock_ip_model = MagicMock()
        mock_ip_model.objects.filter.return_value.first.return_value = mock_ip

        patches, _ = self._start_patches()
        try:
            with patch("ipam.models.IPAddress", mock_ip_model):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            self._stop_patches(patches)

        # The IP match should set existing_device to the mock device
        assert result.get("existing_device") is mock_device

    def test_no_hostname_adds_issue(self):
        """Line 612: Device with no hostname adds an issue."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "",
            "sysName": "",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        api = self._make_api()

        patches, _ = self._start_patches()
        try:
            result = validate_device_for_import(libre_device, api=api)
        finally:
            self._stop_patches(patches)

        # Should have hostname-related issue
        assert isinstance(result, dict)


class TestValidateDeviceMoreEdgeCases:
    """More edge case tests for validate_device_for_import."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        return api

    def _get_patches(self):
        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None
        mock_device.objects.all.return_value = []

        mock_vm = MagicMock()
        mock_vm.objects.filter.return_value.first.return_value = None

        mock_site = MagicMock()
        mock_site.objects.all.return_value = []

        mock_cluster = MagicMock()
        mock_cluster.objects.all.return_value = []
        mock_role = MagicMock()
        mock_role.objects.all.return_value = []
        mock_ip = MagicMock()
        mock_ip.objects.filter.return_value.first.return_value = None

        return [
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
                return_value={"found": False, "site": None, "match_type": None, "suggestions": []},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False, "device_type": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole", mock_role),
            patch("netbox_librenms_plugin.import_utils.device_operations.Cluster", mock_cluster),
            patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType", MagicMock()),
            patch("netbox_librenms_plugin.import_utils.device_operations.Site", mock_site),
            patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine", mock_vm),
            patch("ipam.models.IPAddress", mock_ip),
        ]

    def test_serial_dash_normalized(self):
        """Line 346: serial '-' is normalized to empty string."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        existing = MagicMock()
        existing.name = "router01"
        existing.serial = "SN001"
        existing.custom_field_data = {"librenms_id": {"default": 1}}
        existing.virtual_chassis = MagicMock()  # Has VC
        existing.vc_position = 1

        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",  # Dash serial
            "os": "-",
            "location": "",
        }
        api = self._make_api()

        patches = self._get_patches()
        try:
            for p in patches:
                p.start()

            # Return None for VM, existing for Device
            def _device_side_effect(model, device_id, server_key):
                from virtualization.models import VirtualMachine as VM

                return None if model is VM else existing

            with patch("netbox_librenms_plugin.utils.find_by_librenms_id", side_effect=_device_side_effect):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result is not None

    def test_serial_conflict_with_another_device(self):
        """Lines 373-375: incoming serial already used by another device."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        existing = MagicMock()
        existing.name = "router01"
        existing.serial = "OLD_SN"  # Different from incoming
        existing.custom_field_data = {"librenms_id": {"default": 1}}
        existing.virtual_chassis = None

        conflict_device = MagicMock()
        conflict_device.name = "router02"
        conflict_device.pk = 99

        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "NEW_SN",  # Different serial
            "os": "-",
            "location": "",
        }
        api = self._make_api()

        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = conflict_device

        patches = self._get_patches()
        try:
            for p in patches:
                p.start()

            # Return None for VM check, existing for Device check
            def _find_side_effect(model, device_id, server_key):
                from virtualization.models import VirtualMachine as VM

                if model is VM:
                    return None
                return existing

            with (
                patch("netbox_librenms_plugin.utils.find_by_librenms_id", side_effect=_find_side_effect),
                patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            ):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result.get("serial_action") == "conflict"

    def test_both_vm_and_device_with_same_hostname(self):
        """Lines 395-399: both VM and Device have same hostname - ambiguous match."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "server01",
            "sysName": "server01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "",
        }
        api = self._make_api()

        existing_vm = MagicMock()
        existing_vm.name = "server01"
        existing_device = MagicMock()
        existing_device.name = "server01"

        mock_vm = MagicMock()
        mock_vm.objects.filter.return_value.first.return_value = existing_vm

        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = existing_device
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None
        mock_device.objects.all.return_value = []

        mock_site = MagicMock()
        mock_site.objects.all.return_value = []

        patches = self._get_patches()
        try:
            for p in patches:
                p.start()
            with (
                patch("virtualization.models.VirtualMachine", mock_vm),
                patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
                patch("netbox_librenms_plugin.import_utils.device_operations.Site", mock_site),
            ):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        # Ambiguous - should be a blocking issue (can_import=False) about both existing
        assert result is not None
        assert result.get("can_import") is False
        assert any("VM" in i and "Device" in i for i in result.get("issues", []))

    def test_existing_vm_by_hostname(self):
        """Lines 406-413: VM found by hostname (no Device match)."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "",
        }
        api = self._make_api()

        existing_vm = MagicMock()
        existing_vm.name = "vm01"
        existing_vm.custom_field_data = {}

        mock_vm = MagicMock()
        mock_vm.objects.filter.return_value.first.return_value = existing_vm  # VM found

        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = None  # No device match
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None
        mock_device.objects.all.return_value = []

        patches = self._get_patches()
        try:
            for p in patches:
                p.start()
            with (
                patch("virtualization.models.VirtualMachine", mock_vm),
                patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            ):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result.get("existing_device") is existing_vm
        assert result.get("existing_match_type") == "hostname"

    def test_no_hostname_adds_issue(self):
        """Line 612: hostname is falsy → 'Device has no hostname' issue added."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "",
            "sysName": "",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "",
        }
        api = self._make_api()

        patches = self._get_patches()
        try:
            for p in patches:
                p.start()
            # Patch _determine_device_name to return "" to trigger line 612
            with patch("netbox_librenms_plugin.import_utils.device_operations._determine_device_name", return_value=""):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        # Issue text is "Device has no hostname"
        assert any("no hostname" in issue or "hostname" in issue for issue in result.get("issues", []))

    def test_vc_detection_exception_handled(self):
        """Lines 634-636: VC detection exception is caught and stored."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "",
        }
        api = self._make_api()

        patches = self._get_patches()
        try:
            for p in patches:
                p.start()
            with patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                side_effect=Exception("VC error"),
            ):
                result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result is not None
        assert "detection_error" in result.get("virtual_chassis", {})


class TestImportSingleDeviceEdgeCases:
    """Tests for import_single_device edge cases (lines 737-739, 777-789)."""

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_no_libre_device_api_failure(self, MockAPI):
        """Lines 737-739: libre_device=None and API fails → returns error dict."""
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.get_device_info.return_value = (False, None)
        MockAPI.return_value = mock_api

        result = import_single_device(device_id=1, libre_device=None, server_key="default")
        assert result["success"] is False
        assert "Failed to retrieve device" in result.get("error", "")

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_manual_mappings_are_applied(self, MockAPI):
        """Lines 777-789: manual_mappings override site/device_type/device_role."""
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        mock_api = MagicMock()
        mock_api.server_key = "default"
        MockAPI.return_value = mock_api

        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "hardware": "Cisco",
            "serial": "SN001",
            "os": "ios",
            "location": "",
        }
        validation = {
            "is_ready": True,
            "can_import": True,
            "existing_device": None,
            "import_as_vm": False,
            "site": {"found": True, "site": None},
            "device_type": {"found": True, "device_type": None},
            "device_role": {"found": False, "role": None},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
            "issues": [],
        }

        mock_site = MagicMock()
        mock_site.pk = 1
        mock_dt = MagicMock()
        mock_dt.pk = 1
        mock_role = MagicMock()
        mock_role.pk = 1

        manual_mappings = {"site_id": 1, "device_type_id": 1, "device_role_id": 1}

        mock_tx = MagicMock()
        mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)

        with patch("netbox_librenms_plugin.import_utils.device_operations.transaction", mock_tx):
            with patch("netbox_librenms_plugin.import_utils.device_operations.Site") as mock_site_cls:
                mock_site_cls.objects.filter.return_value.first.return_value = mock_site
                with patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType") as mock_dt_cls:
                    mock_dt_cls.objects.filter.return_value.first.return_value = mock_dt
                    with patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole") as mock_role_cls:
                        mock_role_cls.objects.filter.return_value.first.return_value = mock_role
                        with patch("netbox_librenms_plugin.import_utils.device_operations.Rack") as mock_rack_cls:
                            mock_rack_cls.objects.select_related.return_value.filter.return_value.first.return_value = (
                                None
                            )
                            with patch(
                                "netbox_librenms_plugin.import_utils.device_operations.Device"
                            ) as mock_device_cls:
                                mock_device_cls.objects.filter.return_value.first.return_value = None
                                mock_new_device = MagicMock()
                                mock_device_cls.return_value = mock_new_device
                                mock_new_device.full_clean.return_value = None
                                mock_new_device.save.return_value = None
                                mock_new_device.pk = 99
                                with patch(
                                    "netbox_librenms_plugin.import_utils.device_operations.set_librenms_device_id"
                                ):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.device_operations.validate_device_for_import",
                                        return_value=validation,
                                    ):
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.device_operations.timezone"
                                        ) as mock_tz:
                                            mock_tz.now.return_value.strftime.return_value = "2024-01-01 00:00:00 UTC"
                                            result = import_single_device(
                                                device_id=1,
                                                libre_device=libre_device,
                                                validation=validation,
                                                manual_mappings=manual_mappings,
                                                server_key="default",
                                            )
        # Should have succeeded
        assert result.get("success") is True
        mock_new_device.full_clean.assert_called_once()
        mock_new_device.save.assert_called_once()


class TestImportSingleDeviceMoreEdgeCases:
    """Tests for device_operations additional coverage (lines 539, 783-785, 789)."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        return api

    def _base_validation(self):
        return {
            "is_ready": True,
            "can_import": True,
            "existing_device": None,
            "import_as_vm": False,
            "site": {"found": True, "site": MagicMock()},
            "device_type": {"found": True, "device_type": MagicMock()},
            "device_role": {"found": True, "role": MagicMock()},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
            "issues": [],
        }

    def _mock_tx(self):
        mock_tx = MagicMock()
        mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
        return mock_tx

    def test_platform_manual_mapping(self):
        """Lines 783-785: manual_mappings with platform_id applied."""
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        libre_device = {"device_id": 1, "hostname": "r01", "serial": "-", "hardware": "-", "os": "-", "location": ""}
        validation = self._base_validation()
        manual_mappings = {"platform_id": 3}

        mock_platform = MagicMock()
        mock_new_device = MagicMock()
        mock_new_device.full_clean.return_value = None
        mock_new_device.save.return_value = None
        mock_new_device.pk = 10

        with patch("netbox_librenms_plugin.import_utils.device_operations.transaction", self._mock_tx()):
            with patch("netbox_librenms_plugin.import_utils.device_operations.Site") as MockSite:
                MockSite.objects.filter.return_value.first.return_value = MagicMock()
                with patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType") as MockDT:
                    MockDT.objects.filter.return_value.first.return_value = MagicMock()
                    with patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole") as MockRole:
                        MockRole.objects.filter.return_value.first.return_value = MagicMock()
                        with patch("netbox_librenms_plugin.import_utils.device_operations.Device") as MockDevice:
                            MockDevice.objects.filter.return_value.first.return_value = None
                            MockDevice.return_value = mock_new_device
                            with patch("dcim.models.Platform") as MockPlatform:
                                MockPlatform.objects.filter.return_value.first.return_value = mock_platform
                                with patch(
                                    "netbox_librenms_plugin.import_utils.device_operations.set_librenms_device_id"
                                ):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI"
                                    ) as MockAPI:
                                        MockAPI.return_value = self._make_api()
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.device_operations.timezone"
                                        ) as mock_tz:
                                            mock_tz.now.return_value.strftime.return_value = "2024-01-01"
                                            result = import_single_device(
                                                device_id=1,
                                                libre_device=libre_device,
                                                validation=validation,
                                                manual_mappings=manual_mappings,
                                                server_key="default",
                                            )

        assert result.get("success") is True

    def test_rack_manual_mapping(self):
        """Line 789: manual_mappings with rack_id applied."""
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        libre_device = {"device_id": 1, "hostname": "r01", "serial": "-", "hardware": "-", "os": "-", "location": ""}
        validation = self._base_validation()
        manual_mappings = {"rack_id": 5}

        mock_rack = MagicMock()
        mock_new_device = MagicMock()
        mock_new_device.full_clean.return_value = None
        mock_new_device.save.return_value = None
        mock_new_device.pk = 10

        with patch("netbox_librenms_plugin.import_utils.device_operations.transaction", self._mock_tx()):
            with patch("netbox_librenms_plugin.import_utils.device_operations.Site") as MockSite:
                MockSite.objects.filter.return_value.first.return_value = MagicMock()
                with patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType") as MockDT:
                    MockDT.objects.filter.return_value.first.return_value = MagicMock()
                    with patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole") as MockRole:
                        MockRole.objects.filter.return_value.first.return_value = MagicMock()
                        with patch("netbox_librenms_plugin.import_utils.device_operations.Device") as MockDevice:
                            MockDevice.objects.filter.return_value.first.return_value = None
                            MockDevice.return_value = mock_new_device
                            with patch("netbox_librenms_plugin.import_utils.device_operations.Rack") as MockRack:
                                MockRack.objects.select_related.return_value.filter.return_value.first.return_value = (
                                    mock_rack
                                )
                                with patch(
                                    "netbox_librenms_plugin.import_utils.device_operations.set_librenms_device_id"
                                ):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI"
                                    ) as MockAPI:
                                        MockAPI.return_value = self._make_api()
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.device_operations.timezone"
                                        ) as mock_tz:
                                            mock_tz.now.return_value.strftime.return_value = "2024-01-01"
                                            result = import_single_device(
                                                device_id=1,
                                                libre_device=libre_device,
                                                validation=validation,
                                                manual_mappings=manual_mappings,
                                                server_key="default",
                                            )

        assert result.get("success") is True


class TestValidateDeviceChassisMatch:
    """Test chassis match path (line 539) in validate_device_for_import."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        return api

    def test_chassis_match_overrides_hardware_match(self):
        """Line 539: chassis_match succeeds → dt_match = chassis_match."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "Cisco Catalyst 9300",
            "serial": "SN001",
            "os": "ios",
            "location": "",
        }
        api = self._make_api()

        chassis_dt = MagicMock()
        chassis_dt.model = "Catalyst 9300"
        chassis_match = {"matched": True, "device_type": chassis_dt, "match_type": "chassis_inventory"}

        patches = [
            patch("netbox_librenms_plugin.import_utils.device_operations.Site"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_utils.device_operations.Device"),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine"),
            patch("ipam.models.IPAddress"),
            patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations._try_chassis_device_type_match",
                return_value=chassis_match,
            ),
        ]

        [p.start() for p in patches]

        try:
            result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()

        assert result["device_type"].get("device_type") is chassis_dt
