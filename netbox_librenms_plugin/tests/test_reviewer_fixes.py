"""Regression tests for reviewer-requested fixes.

Covers: _load_vc_member_name_pattern validation, _normalize_librenms_mapping
guards, all_server_mappings did validation, render_device_selection XSS escape.
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _load_vc_member_name_pattern
# ---------------------------------------------------------------------------
class TestLoadVcMemberNamePattern:
    """_load_vc_member_name_pattern must return valid string or default."""

    DEFAULT = "-M{position}"

    def _call(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        return _load_vc_member_name_pattern()

    def _patch_settings(self, settings_obj):
        """Patch the deferred import of LibreNMSSettings inside the function."""
        return patch(
            "netbox_librenms_plugin.models.LibreNMSSettings.objects",
            **{"order_by.return_value.first.return_value": settings_obj},
        )

    def test_returns_valid_pattern(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = "-SW{position}"
        with self._patch_settings(settings):
            assert self._call() == "-SW{position}"

    def test_returns_default_for_none_pattern(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = None
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_empty_string(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = ""
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_whitespace_only(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = "   "
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_boolean(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = True
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_when_no_settings(self):
        with self._patch_settings(None):
            assert self._call() == self.DEFAULT

    def test_returns_default_on_exception(self):
        with patch(
            "netbox_librenms_plugin.models.LibreNMSSettings.objects",
        ) as mock_objs:
            mock_objs.order_by.side_effect = RuntimeError("db error")
            assert self._call() == self.DEFAULT


# ---------------------------------------------------------------------------
# _normalize_librenms_mapping
# ---------------------------------------------------------------------------
class TestNormalizeLibreNMSMapping:
    """_normalize_librenms_mapping must reject booleans and non-digit strings."""

    def _call(self, value):
        # Instantiate the view class minimally to access the method
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        view = object.__new__(RemoveServerMappingView)
        return view._normalize_librenms_mapping(value)

    def test_int_becomes_default_dict(self):
        assert self._call(42) == {"default": 42}

    def test_bool_true_returns_empty(self):
        assert self._call(True) == {}

    def test_bool_false_returns_empty(self):
        assert self._call(False) == {}

    def test_digit_string_coerced(self):
        assert self._call("42") == {"default": 42}

    def test_non_digit_string_returns_empty(self):
        assert self._call("not-a-number") == {}

    def test_plus_prefix_rejected(self):
        """'+1' is not strictly digit-only."""
        assert self._call("+1") == {}

    def test_space_padded_rejected(self):
        """' 42 ' is not strictly digit-only."""
        assert self._call(" 42 ") == {}

    def test_dict_passed_through(self):
        d = {"production": 7}
        assert self._call(d) is d

    def test_none_returns_empty(self):
        assert self._call(None) == {}

    def test_list_returns_empty(self):
        assert self._call([1, 2]) == {}


# ---------------------------------------------------------------------------
# all_server_mappings — did validation
# ---------------------------------------------------------------------------
class TestAllServerMappingsDidValidation:
    """all_server_mappings must skip invalid device IDs in the cf_value dict."""

    def _call(self, obj, active_server_key="default"):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        return BaseLibreNMSSyncView._build_all_server_mappings(obj, active_server_key)

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_boolean_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": True, "prod": 42}}
        result = self._call(obj)
        # Only prod=42 should survive
        assert len(result) == 1
        assert result[0]["device_id"] == 42

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_none_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": None}}
        result = self._call(obj)
        assert result is None  # empty list → returns None

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_coerces_digit_string_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"prod": "99"}}
        result = self._call(obj)
        assert len(result) == 1
        assert result[0]["device_id"] == 99

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_non_digit_string_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": "bogus"}}
        result = self._call(obj)
        assert result is None

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_valid_int_passes_through(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 5, "secondary": 10}}
        result = self._call(obj)
        assert len(result) == 2
        ids = {e["device_id"] for e in result}
        assert ids == {5, 10}


# ---------------------------------------------------------------------------
# render_device_selection — XSS escape
# ---------------------------------------------------------------------------
class TestRenderDeviceSelectionEscape:
    """render_device_selection must HTML-escape member.name."""

    def test_member_name_is_escaped(self):
        from netbox_librenms_plugin.tables.cables import VCCableTable

        device = MagicMock()
        device.id = 1
        vc = MagicMock()
        member = MagicMock()
        member.id = 1
        member.name = '<script>alert("xss")</script>'
        vc.members.all.return_value = [member]
        device.virtual_chassis = vc

        table = VCCableTable([], device=device)
        record = {"local_port": "eth0", "local_port_id": "42"}

        with patch(
            "netbox_librenms_plugin.tables.cables.get_virtual_chassis_member",
            return_value=member,
        ):
            html = str(table.render_device_selection(None, record))

        # The raw <script> tag must NOT appear — it should be escaped
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
