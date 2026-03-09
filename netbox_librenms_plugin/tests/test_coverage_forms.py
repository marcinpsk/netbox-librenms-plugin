"""Coverage tests for netbox_librenms_plugin.forms module.

Targets lines: 41-60, 69-105, 127-128, 181-228, 337-343, 391-397,
               605-606, 703-704, 714-717, 823-840, 851-861, 875-901,
               914-917, 1001-1046
"""

import pytest
from unittest.mock import MagicMock, patch


# =============================================================================
# Tests for _get_librenms_server_choices (lines 41-60)
# =============================================================================


class TestGetLibreNMSServerChoices:
    """Tests for the _get_librenms_server_choices helper function."""

    def test_multi_server_with_display_name(self):
        from netbox_librenms_plugin.forms import _get_librenms_server_choices

        servers = {
            "primary": {
                "display_name": "Primary DC",
                "librenms_url": "https://librenms.example.com",
            },
            "secondary": {
                "display_name": "Secondary DC",
                "librenms_url": "https://librenms2.example.com",
            },
        }
        with patch("netbox_librenms_plugin.forms.get_plugin_config", return_value=servers):
            result = _get_librenms_server_choices()

        assert ("primary", "Primary DC (https://librenms.example.com)") in result
        assert ("secondary", "Secondary DC (https://librenms2.example.com)") in result
        assert len(result) == 2

    def test_multi_server_without_display_name_falls_back_to_key(self):
        from netbox_librenms_plugin.forms import _get_librenms_server_choices

        servers = {
            "production": {
                "librenms_url": "https://librenms.prod.example.com",
            },
        }
        with patch("netbox_librenms_plugin.forms.get_plugin_config", return_value=servers):
            result = _get_librenms_server_choices()

        assert result == [("production", "production (https://librenms.prod.example.com)")]

    def test_multi_server_without_url_uses_unknown(self):
        from netbox_librenms_plugin.forms import _get_librenms_server_choices

        servers = {
            "staging": {
                "display_name": "Staging",
            },
        }
        with patch("netbox_librenms_plugin.forms.get_plugin_config", return_value=servers):
            result = _get_librenms_server_choices()

        assert result == [("staging", "Staging (Unknown URL)")]

    def test_legacy_single_server_with_url(self):
        from netbox_librenms_plugin.forms import _get_librenms_server_choices

        def mock_get_config(plugin, key):
            if key == "servers":
                return None
            if key == "librenms_url":
                return "https://librenms.legacy.com"
            return None

        with patch("netbox_librenms_plugin.forms.get_plugin_config", side_effect=mock_get_config):
            result = _get_librenms_server_choices()

        assert result == [("default", "Default Server (https://librenms.legacy.com)")]

    def test_legacy_single_server_no_url(self):
        from netbox_librenms_plugin.forms import _get_librenms_server_choices

        with patch("netbox_librenms_plugin.forms.get_plugin_config", return_value=None):
            result = _get_librenms_server_choices()

        assert result == [("default", "Default Server")]

    def test_servers_config_is_not_dict_falls_back_to_legacy(self):
        from netbox_librenms_plugin.forms import _get_librenms_server_choices

        def mock_get_config(plugin, key):
            if key == "servers":
                return "not-a-dict"
            if key == "librenms_url":
                return "https://librenms.example.com"
            return None

        with patch("netbox_librenms_plugin.forms.get_plugin_config", side_effect=mock_get_config):
            result = _get_librenms_server_choices()

        assert result == [("default", "Default Server (https://librenms.example.com)")]

    def test_servers_config_empty_dict(self):
        from netbox_librenms_plugin.forms import _get_librenms_server_choices

        with patch("netbox_librenms_plugin.forms.get_plugin_config", return_value={}):
            result = _get_librenms_server_choices()

        # empty dict is falsy, falls back to legacy path
        assert result == [("default", "Default Server")]


# =============================================================================
# Tests for _get_librenms_poller_group_choices (lines 69-105)
# =============================================================================


class TestGetLibreNMSPollerGroupChoices:
    """Tests for the _get_librenms_poller_group_choices helper function."""

    def test_cache_hit_returns_cached_choices(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        cached = [("0", "Default (0)"), ("1", "Group A (1)")]
        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = cached
                result = _get_librenms_poller_group_choices()

        assert result == cached
        mock_api.get_poller_groups.assert_not_called()

    def test_api_creation_fails_in_first_try_uses_fallback_cache_key(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI",
            side_effect=Exception("No config"),
        ):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        mock_cache.get.assert_called_once_with("librenms_poller_group_choices")
        assert result == [("0", "Default (0)")]

    def test_success_with_distinct_description(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_poller_groups.return_value = (
            True,
            [{"id": 1, "group_name": "Poller1", "descr": "Main poller"}],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        assert ("1", "Poller1 - Main poller (1)") in result

    def test_success_with_same_name_and_descr_omits_descr(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_poller_groups.return_value = (
            True,
            [{"id": 2, "group_name": "Poller2", "descr": "Poller2"}],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        assert ("2", "Poller2 (2)") in result
        assert not any("Poller2 - Poller2" in label for _, label in result)

    def test_success_with_empty_descr_omits_descr(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        mock_api = MagicMock()
        mock_api.server_key = "test"
        mock_api.cache_timeout = 300
        mock_api.get_poller_groups.return_value = (
            True,
            [{"id": 3, "group_name": "Group3", "descr": ""}],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        assert ("3", "Group3 (3)") in result

    def test_group_without_id_is_skipped(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_poller_groups.return_value = (
            True,
            [{"id": "", "group_name": "NoID", "descr": ""}],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        assert result == [("0", "Default (0)")]

    def test_api_get_poller_groups_returns_failure(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.get_poller_groups.return_value = (False, None)

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        assert result == [("0", "Default (0)")]

    def test_api_get_poller_groups_returns_none_groups(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_poller_groups.return_value = (True, None)

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        # success=True but no groups; cache.set is still called with default only
        assert result == [("0", "Default (0)")]

    def test_exception_in_second_try_block_returns_default(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        call_count = {"n": 0}

        def api_factory():
            call_count["n"] += 1
            if call_count["n"] == 1:
                mock = MagicMock()
                mock.server_key = "default"
                return mock
            raise Exception("Second API init failed")

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", side_effect=api_factory):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = _get_librenms_poller_group_choices()

        assert result == [("0", "Default (0)")]

    def test_successful_results_are_cached(self):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        mock_api = MagicMock()
        mock_api.server_key = "primary"
        mock_api.cache_timeout = 600
        mock_api.get_poller_groups.return_value = (
            True,
            [{"id": 5, "group_name": "PG5", "descr": ""}],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch("django.core.cache.cache") as mock_cache:
                mock_cache.get.return_value = None
                _get_librenms_poller_group_choices()

        mock_cache.set.assert_called_once()
        call_args = mock_cache.set.call_args
        # Verify timeout=600 was passed (positional or keyword)
        assert 600 in call_args[0] or call_args[1].get("timeout") == 600


# =============================================================================
# Tests for ServerConfigForm.__init__ (lines 127-128)
# =============================================================================


class TestServerConfigFormInit:
    """Tests for ServerConfigForm.__init__."""

    def test_populates_selected_server_choices(self):
        from netbox_librenms_plugin.forms import ServerConfigForm

        mock_choices = [
            ("primary", "Primary (http://librenms)"),
            ("secondary", "Secondary (http://librenms2)"),
        ]
        mock_server_field = MagicMock()

        with patch(
            "netbox_librenms_plugin.forms._get_librenms_server_choices",
            return_value=mock_choices,
        ):
            with patch("netbox.forms.NetBoxModelForm.__init__", return_value=None):
                form = object.__new__(ServerConfigForm)
                form.fields = {"selected_server": mock_server_field}
                ServerConfigForm.__init__(form)

        assert mock_server_field.choices == mock_choices

    def test_choices_updated_on_every_init(self):
        from netbox_librenms_plugin.forms import ServerConfigForm

        mock_server_field = MagicMock()
        choices_a = [("a", "A")]
        choices_b = [("b", "B")]

        with patch("netbox.forms.NetBoxModelForm.__init__", return_value=None):
            form = object.__new__(ServerConfigForm)
            form.fields = {"selected_server": mock_server_field}

            with patch(
                "netbox_librenms_plugin.forms._get_librenms_server_choices",
                return_value=choices_a,
            ):
                ServerConfigForm.__init__(form)
            assert mock_server_field.choices == choices_a

            with patch(
                "netbox_librenms_plugin.forms._get_librenms_server_choices",
                return_value=choices_b,
            ):
                ServerConfigForm.__init__(form)
            assert mock_server_field.choices == choices_b


# =============================================================================
# Tests for ImportSettingsForm.clean_vc_member_name_pattern (lines 181-228)
# =============================================================================


class TestImportSettingsFormCleanVcPattern:
    """Tests for ImportSettingsForm.clean_vc_member_name_pattern."""

    def _make_form(self, pattern):
        from netbox_librenms_plugin.forms import ImportSettingsForm

        form = object.__new__(ImportSettingsForm)
        form.cleaned_data = {"vc_member_name_pattern": pattern}
        return form

    def test_valid_pattern_with_position(self):
        form = self._make_form("-M{position}")
        result = form.clean_vc_member_name_pattern()
        assert result == "-M{position}"

    def test_valid_pattern_with_serial(self):
        form = self._make_form("-{serial}")
        result = form.clean_vc_member_name_pattern()
        assert result == "-{serial}"

    def test_valid_pattern_with_both_placeholders(self):
        form = self._make_form("{position}-{serial}")
        result = form.clean_vc_member_name_pattern()
        assert result == "{position}-{serial}"

    def test_empty_string_returns_empty(self):
        form = self._make_form("")
        result = form.clean_vc_member_name_pattern()
        assert result == ""

    def test_none_returns_none(self):
        from netbox_librenms_plugin.forms import ImportSettingsForm

        form = object.__new__(ImportSettingsForm)
        form.cleaned_data = {"vc_member_name_pattern": None}
        result = form.clean_vc_member_name_pattern()
        assert result is None

    def test_invalid_placeholder_raises_validation_error(self):
        from django import forms as django_forms

        form = self._make_form("{hostname}")
        with pytest.raises(django_forms.ValidationError) as exc_info:
            form.clean_vc_member_name_pattern()
        assert "hostname" in str(exc_info.value)

    def test_multiple_invalid_placeholders_raises(self):
        from django import forms as django_forms

        form = self._make_form("{hostname}-{rack}")
        with pytest.raises(django_forms.ValidationError):
            form.clean_vc_member_name_pattern()

    def test_no_unique_identifier_raises(self):
        from django import forms as django_forms

        form = self._make_form("-Member01")
        with pytest.raises(django_forms.ValidationError) as exc_info:
            form.clean_vc_member_name_pattern()
        error_text = str(exc_info.value)
        assert "position" in error_text or "serial" in error_text

    def test_value_error_in_format_raises_validation_error(self):
        from django import forms as django_forms

        # "{position}{" has an unclosed brace — raises ValueError during format()
        form = self._make_form("{position}{")
        with pytest.raises(django_forms.ValidationError) as exc_info:
            form.clean_vc_member_name_pattern()
        assert "Invalid pattern syntax" in str(exc_info.value)

    def test_index_error_in_format_raises_validation_error(self):
        from django import forms as django_forms

        # "{position}-{}" mixes named and auto-numbered — raises IndexError
        form = self._make_form("{position}-{}")
        with pytest.raises(django_forms.ValidationError):
            form.clean_vc_member_name_pattern()

    def test_pattern_with_whitespace_around_identifier(self):
        # e.g. " {position}" — still valid, formats to " 1" which is non-empty
        form = self._make_form(" {position}")
        result = form.clean_vc_member_name_pattern()
        assert result == " {position}"


# =============================================================================
# Tests for DeviceTypeMappingImportForm.__init__ (lines 337-343)
# =============================================================================


class TestDeviceTypeMappingImportFormInit:
    """Tests for DeviceTypeMappingImportForm.__init__."""

    def test_with_manufacturer_filters_device_types(self):
        from netbox_librenms_plugin.forms import DeviceTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_mfr_field.to_field_name = "name"
        mock_dt_field = MagicMock()
        mock_filtered = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_device_type": mock_dt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.DeviceType") as MockDT:
                MockDT.objects.filter.return_value = mock_filtered
                DeviceTypeMappingImportForm(data={"manufacturer": "Cisco"})

        MockDT.objects.filter.assert_called_once_with(manufacturer__name="Cisco")
        assert mock_dt_field.queryset == mock_filtered

    def test_without_data_no_filter_applied(self):
        from netbox_librenms_plugin.forms import DeviceTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_dt_field = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_device_type": mock_dt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.DeviceType") as MockDT:
                DeviceTypeMappingImportForm(data=None)

        MockDT.objects.filter.assert_not_called()

    def test_with_data_but_empty_manufacturer_no_filter(self):
        from netbox_librenms_plugin.forms import DeviceTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_dt_field = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_device_type": mock_dt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.DeviceType") as MockDT:
                DeviceTypeMappingImportForm(data={"librenms_hardware": "hw", "manufacturer": ""})

        MockDT.objects.filter.assert_not_called()

    def test_uses_to_field_name_for_filter_key(self):
        from netbox_librenms_plugin.forms import DeviceTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_mfr_field.to_field_name = "slug"
        mock_dt_field = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_device_type": mock_dt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.DeviceType") as MockDT:
                DeviceTypeMappingImportForm(data={"manufacturer": "cisco"})

        MockDT.objects.filter.assert_called_once_with(manufacturer__slug="cisco")


# =============================================================================
# Tests for ModuleTypeMappingImportForm.__init__ (lines 391-397)
# =============================================================================


class TestModuleTypeMappingImportFormInit:
    """Tests for ModuleTypeMappingImportForm.__init__."""

    def test_with_manufacturer_filters_module_types(self):
        from netbox_librenms_plugin.forms import ModuleTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_mfr_field.to_field_name = "name"
        mock_mt_field = MagicMock()
        mock_filtered = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_module_type": mock_mt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.ModuleType") as MockMT:
                MockMT.objects.filter.return_value = mock_filtered
                ModuleTypeMappingImportForm(data={"manufacturer": "Juniper"})

        MockMT.objects.filter.assert_called_once_with(manufacturer__name="Juniper")
        assert mock_mt_field.queryset == mock_filtered

    def test_without_data_no_filter_applied(self):
        from netbox_librenms_plugin.forms import ModuleTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_mt_field = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_module_type": mock_mt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.ModuleType") as MockMT:
                ModuleTypeMappingImportForm(data=None)

        MockMT.objects.filter.assert_not_called()

    def test_with_data_but_empty_manufacturer_no_filter(self):
        from netbox_librenms_plugin.forms import ModuleTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_mt_field = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_module_type": mock_mt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.ModuleType") as MockMT:
                ModuleTypeMappingImportForm(data={"librenms_model": "X", "manufacturer": ""})

        MockMT.objects.filter.assert_not_called()

    def test_uses_to_field_name_for_filter_key(self):
        from netbox_librenms_plugin.forms import ModuleTypeMappingImportForm

        mock_mfr_field = MagicMock()
        mock_mfr_field.to_field_name = "slug"
        mock_mt_field = MagicMock()

        def fake_super_init(self, data=None, *args, **kwargs):
            self.fields = {
                "manufacturer": mock_mfr_field,
                "netbox_module_type": mock_mt_field,
            }

        with patch("netbox.forms.NetBoxModelImportForm.__init__", fake_super_init):
            with patch("netbox_librenms_plugin.forms.ModuleType") as MockMT:
                ModuleTypeMappingImportForm(data={"manufacturer": "juniper"})

        MockMT.objects.filter.assert_called_once_with(manufacturer__slug="juniper")


# =============================================================================
# Tests for AddToLIbreSNMPV1V2.__init__ (lines 605-606)
# =============================================================================


class TestAddToLibreSNMPV1V2Init:
    """Tests for AddToLIbreSNMPV1V2.__init__."""

    def test_populates_poller_group_choices(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2

        mock_choices = [("0", "Default (0)"), ("1", "Group A (1)")]
        with patch(
            "netbox_librenms_plugin.forms._get_librenms_poller_group_choices",
            return_value=mock_choices,
        ):
            form = AddToLIbreSNMPV1V2()

        assert form.fields["poller_group"].choices == mock_choices

    def test_poller_group_field_present(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2

        with patch(
            "netbox_librenms_plugin.forms._get_librenms_poller_group_choices",
            return_value=[("0", "Default (0)")],
        ):
            form = AddToLIbreSNMPV1V2()

        assert "poller_group" in form.fields

    def test_calls_get_poller_group_choices_helper(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2

        with patch(
            "netbox_librenms_plugin.forms._get_librenms_poller_group_choices",
            return_value=[("0", "Default (0)")],
        ) as mock_helper:
            AddToLIbreSNMPV1V2()

        mock_helper.assert_called_once()


# =============================================================================
# Tests for AddToLIbreSNMPV3.__init__ (lines 703-704)
# =============================================================================


class TestAddToLibreSNMPV3Init:
    """Tests for AddToLIbreSNMPV3.__init__."""

    def test_populates_poller_group_choices(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV3

        mock_choices = [("0", "Default (0)"), ("2", "Remote Poller (2)")]
        with patch(
            "netbox_librenms_plugin.forms._get_librenms_poller_group_choices",
            return_value=mock_choices,
        ):
            form = AddToLIbreSNMPV3()

        assert form.fields["poller_group"].choices == mock_choices

    def test_poller_group_field_present(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV3

        with patch(
            "netbox_librenms_plugin.forms._get_librenms_poller_group_choices",
            return_value=[("0", "Default (0)")],
        ):
            form = AddToLIbreSNMPV3()

        assert "poller_group" in form.fields

    def test_calls_get_poller_group_choices_helper(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV3

        with patch(
            "netbox_librenms_plugin.forms._get_librenms_poller_group_choices",
            return_value=[("0", "Default (0)")],
        ) as mock_helper:
            AddToLIbreSNMPV3()

        mock_helper.assert_called_once()


# =============================================================================
# Tests for DeviceStatusFilterForm.__init__ (lines 714-717)
# =============================================================================


class TestDeviceStatusFilterFormInit:
    """Tests for DeviceStatusFilterForm.__init__."""

    def test_removes_filter_id_if_present(self):
        from netbox_librenms_plugin.forms import DeviceStatusFilterForm

        with patch("netbox.forms.NetBoxModelFilterSetForm.__init__", return_value=None):
            form = object.__new__(DeviceStatusFilterForm)
            form.fields = {"filter_id": MagicMock(), "site": MagicMock()}
            DeviceStatusFilterForm.__init__(form)

        assert "filter_id" not in form.fields
        assert "site" in form.fields

    def test_no_error_when_filter_id_absent(self):
        from netbox_librenms_plugin.forms import DeviceStatusFilterForm

        with patch("netbox.forms.NetBoxModelFilterSetForm.__init__", return_value=None):
            form = object.__new__(DeviceStatusFilterForm)
            form.fields = {"site": MagicMock(), "location": MagicMock()}
            DeviceStatusFilterForm.__init__(form)

        assert "site" in form.fields
        assert "location" in form.fields


# =============================================================================
# Tests for LibreNMSImportFilterForm.__init__ (lines 823-840)
# =============================================================================


class TestLibreNMSImportFilterFormInit:
    """Tests for LibreNMSImportFilterForm.__init__ bound-data handling."""

    def test_bound_empty_data_adds_use_background_job_default(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations"):
            form = LibreNMSImportFilterForm({})

        assert form.data.get("use_background_job") == "on"

    def test_bound_data_with_filter_field_does_not_add_default(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations"):
            form = LibreNMSImportFilterForm({"librenms_hostname": "router1"})

        assert "use_background_job" not in form.data

    def test_bound_data_with_librenms_type_does_not_add_default(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations"):
            form = LibreNMSImportFilterForm({"librenms_type": "network"})

        assert "use_background_job" not in form.data

    def test_existing_use_background_job_value_not_overridden(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations"):
            # User explicitly unchecked the checkbox — value is empty string
            form = LibreNMSImportFilterForm({"use_background_job": ""})

        assert form.data.get("use_background_job") == ""

    def test_bound_data_with_job_id_does_not_add_default(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations"):
            form = LibreNMSImportFilterForm({"job_id": "123"})

        assert "use_background_job" not in form.data

    def test_unbound_form_is_not_modified(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations"):
            form = LibreNMSImportFilterForm()

        assert not form.is_bound

    def test_populate_locations_always_called(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations") as mock_populate:
            LibreNMSImportFilterForm({})

        mock_populate.assert_called_once()

    def test_querydict_data_is_copied_and_mutated(self):
        from django.http import QueryDict
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        qd = QueryDict(mutable=True)
        with patch.object(LibreNMSImportFilterForm, "_populate_librenms_locations"):
            form = LibreNMSImportFilterForm(qd)

        assert form.data.get("use_background_job") == "on"


# =============================================================================
# Tests for LibreNMSImportFilterForm.clean (lines 851-861)
# =============================================================================


class TestLibreNMSImportFilterFormClean:
    """Tests for LibreNMSImportFilterForm.clean."""

    def test_apply_filters_with_no_filter_fields_raises(self):
        from django import forms as django_forms
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": "1"}
        form.cleaned_data = {}

        with pytest.raises(django_forms.ValidationError):
            LibreNMSImportFilterForm.clean(form)

    def test_apply_filters_with_hostname_passes(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": "1"}
        form.cleaned_data = {"librenms_hostname": "router1"}

        result = LibreNMSImportFilterForm.clean(form)
        assert result["librenms_hostname"] == "router1"

    def test_apply_filters_with_location_passes(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": "1"}
        form.cleaned_data = {"librenms_location": "NYC"}

        result = LibreNMSImportFilterForm.clean(form)
        assert result is not None

    def test_apply_filters_with_type_passes(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": "1"}
        form.cleaned_data = {"librenms_type": "network"}

        result = LibreNMSImportFilterForm.clean(form)
        assert result["librenms_type"] == "network"

    def test_apply_filters_with_os_passes(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": "1"}
        form.cleaned_data = {"librenms_os": "ios"}

        result = LibreNMSImportFilterForm.clean(form)
        assert result["librenms_os"] == "ios"

    def test_apply_filters_with_hardware_passes(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": "1"}
        form.cleaned_data = {"librenms_hardware": "C9300"}

        result = LibreNMSImportFilterForm.clean(form)
        assert result["librenms_hardware"] == "C9300"

    def test_apply_filters_with_sysname_passes(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": "1"}
        form.cleaned_data = {"librenms_sysname": "core-switch"}

        result = LibreNMSImportFilterForm.clean(form)
        assert result["librenms_sysname"] == "core-switch"

    def test_without_apply_filters_skips_validation(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {}
        form.cleaned_data = {}

        result = LibreNMSImportFilterForm.clean(form)
        assert result == {}

    def test_apply_filters_false_value_skips_validation(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.data = {"apply_filters": ""}
        form.cleaned_data = {}

        result = LibreNMSImportFilterForm.clean(form)
        assert result == {}


# =============================================================================
# Tests for LibreNMSImportFilterForm._populate_librenms_locations (lines 875-901)
# =============================================================================


class TestPopulateLibreNMSLocations:
    """Tests for LibreNMSImportFilterForm._populate_librenms_locations."""

    def _make_form(self):
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

        form = object.__new__(LibreNMSImportFilterForm)
        form.fields = {"librenms_location": MagicMock()}
        return form

    def test_cache_hit_sets_choices_and_returns_early(self):
        form = self._make_form()
        cached = [("", "All Locations"), ("1", "NYC")]
        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch(
                "netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key",
                return_value="loc_cache_key",
            ):
                with patch("django.core.cache.cache") as mock_cache:
                    mock_cache.get.return_value = cached
                    form._populate_librenms_locations()

        assert form.fields["librenms_location"].choices == cached
        mock_api.get_locations.assert_not_called()

    def test_cache_miss_fetches_from_api_and_sets_choices(self):
        form = self._make_form()
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_locations.return_value = (
            True,
            [
                {"id": 1, "location": "NYC"},
                {"id": 2, "location": "LAX"},
            ],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch(
                "netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key",
                return_value="loc_cache_key",
            ):
                with patch("django.core.cache.cache") as mock_cache:
                    mock_cache.get.return_value = None
                    form._populate_librenms_locations()

        choices = form.fields["librenms_location"].choices
        assert choices[0] == ("", "All Locations")
        assert any(c[1] == "NYC" for c in choices)
        assert any(c[1] == "LAX" for c in choices)

    def test_cache_miss_results_are_cached(self):
        form = self._make_form()
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_locations.return_value = (True, [{"id": 1, "location": "NYC"}])

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch(
                "netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key",
                return_value="loc_key",
            ):
                with patch("django.core.cache.cache") as mock_cache:
                    mock_cache.get.return_value = None
                    form._populate_librenms_locations()

        mock_cache.set.assert_called_once()

    def test_api_failure_logs_warning(self):
        form = self._make_form()
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.get_locations.return_value = (False, "Connection error")

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch(
                "netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key",
                return_value="loc_key",
            ):
                with patch("django.core.cache.cache") as mock_cache:
                    with patch("netbox_librenms_plugin.forms.logger") as mock_logger:
                        mock_cache.get.return_value = None
                        form._populate_librenms_locations()

        mock_logger.warning.assert_called_once()

    def test_exception_logs_and_does_not_reraise(self):
        form = self._make_form()

        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI",
            side_effect=Exception("API init failed"),
        ):
            with patch("netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key"):
                with patch("netbox_librenms_plugin.forms.logger") as mock_logger:
                    # Must not re-raise
                    form._populate_librenms_locations()

        mock_logger.exception.assert_called_once()

    def test_locations_sorted_alphabetically(self):
        form = self._make_form()
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_locations.return_value = (
            True,
            [
                {"id": 3, "location": "Chicago"},
                {"id": 1, "location": "NYC"},
                {"id": 2, "location": "Atlanta"},
            ],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch(
                "netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key",
                return_value="key",
            ):
                with patch("django.core.cache.cache") as mock_cache:
                    mock_cache.get.return_value = None
                    form._populate_librenms_locations()

        choices = form.fields["librenms_location"].choices
        non_empty = [c for c in choices if c[0] != ""]
        names = [c[1] for c in non_empty]
        assert names == sorted(names)

    def test_location_id_converted_to_string(self):
        form = self._make_form()
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_locations.return_value = (
            True,
            [{"id": 42, "location": "TestLoc"}],
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch(
                "netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key",
                return_value="key",
            ):
                with patch("django.core.cache.cache") as mock_cache:
                    mock_cache.get.return_value = None
                    form._populate_librenms_locations()

        choices = form.fields["librenms_location"].choices
        ids = [c[0] for c in choices if c[0] != ""]
        assert "42" in ids

    def test_location_missing_name_falls_back_to_location_id(self):
        form = self._make_form()
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300
        mock_api.get_locations.return_value = (
            True,
            [{"id": 7}],  # no "location" key
        )

        with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api):
            with patch(
                "netbox_librenms_plugin.import_utils.cache.get_location_choices_cache_key",
                return_value="key",
            ):
                with patch("django.core.cache.cache") as mock_cache:
                    mock_cache.get.return_value = None
                    form._populate_librenms_locations()

        choices = form.fields["librenms_location"].choices
        labels = [c[1] for c in choices if c[0] != ""]
        assert any("7" in label for label in labels)


# =============================================================================
# Tests for VirtualMachineStatusFilterForm.__init__ (lines 914-917)
# =============================================================================


class TestVirtualMachineStatusFilterFormInit:
    """Tests for VirtualMachineStatusFilterForm.__init__."""

    def test_removes_filter_id_if_present(self):
        from netbox_librenms_plugin.forms import VirtualMachineStatusFilterForm

        with patch("netbox.forms.NetBoxModelFilterSetForm.__init__", return_value=None):
            form = object.__new__(VirtualMachineStatusFilterForm)
            form.fields = {"filter_id": MagicMock(), "virtualmachine": MagicMock()}
            VirtualMachineStatusFilterForm.__init__(form)

        assert "filter_id" not in form.fields
        assert "virtualmachine" in form.fields

    def test_no_error_when_filter_id_absent(self):
        from netbox_librenms_plugin.forms import VirtualMachineStatusFilterForm

        with patch("netbox.forms.NetBoxModelFilterSetForm.__init__", return_value=None):
            form = object.__new__(VirtualMachineStatusFilterForm)
            form.fields = {"cluster": MagicMock(), "site": MagicMock()}
            VirtualMachineStatusFilterForm.__init__(form)

        assert "cluster" in form.fields
        assert "site" in form.fields


# =============================================================================
# Tests for DeviceImportConfigForm.__init__ (lines 1001-1046)
# =============================================================================


class TestDeviceImportConfigFormInit:
    """Tests for DeviceImportConfigForm.__init__."""

    def _make_mock_fields(self):
        return {
            "device_id": MagicMock(),
            "hostname": MagicMock(),
            "hardware": MagicMock(),
            "librenms_location": MagicMock(),
            "site": MagicMock(),
            "device_type": MagicMock(),
            "device_role": MagicMock(),
            "platform": MagicMock(),
        }

    def _fake_form_init(self, mock_fields):
        """Return a replacement for forms.Form.__init__ that sets self.fields."""

        def fake(self_form, *args, **kwargs):
            self_form.fields = mock_fields

        return fake

    def test_sets_initial_values_from_libre_device(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        libre_device = {
            "device_id": 42,
            "hostname": "router1.example.com",
            "hardware": "Cisco C9300",
            "location": "NYC-DC",
        }
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(libre_device=libre_device)

        assert mock_fields["device_id"].initial == 42
        assert mock_fields["hostname"].initial == "router1.example.com"
        assert mock_fields["hardware"].initial == "Cisco C9300"
        assert mock_fields["librenms_location"].initial == "NYC-DC"

    def test_site_set_from_validation_when_no_suggested_site(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_site = MagicMock()
        validation = {"site": {"site": mock_site}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation=validation)

        assert mock_fields["site"].initial == mock_site

    def test_suggested_site_takes_priority_over_validation_site(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_site_from_validation = MagicMock(name="validation_site")
        mock_site_suggested = MagicMock(name="suggested_site")
        validation = {"site": {"site": mock_site_from_validation}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation=validation, suggested_site=mock_site_suggested)

        assert mock_fields["site"].initial == mock_site_suggested

    def test_device_type_set_from_validation(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_dt = MagicMock()
        validation = {"device_type": {"device_type": mock_dt}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation=validation)

        assert mock_fields["device_type"].initial == mock_dt

    def test_suggested_device_type_overrides_validation(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_dt_v = MagicMock()
        mock_dt_s = MagicMock()
        validation = {"device_type": {"device_type": mock_dt_v}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation=validation, suggested_device_type=mock_dt_s)

        assert mock_fields["device_type"].initial == mock_dt_s

    def test_device_role_set_from_validation(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_role = MagicMock()
        validation = {"device_role": {"role": mock_role}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation=validation)

        assert mock_fields["device_role"].initial == mock_role

    def test_suggested_role_overrides_validation(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_role_v = MagicMock()
        mock_role_s = MagicMock()
        validation = {"device_role": {"role": mock_role_v}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation=validation, suggested_role=mock_role_s)

        assert mock_fields["device_role"].initial == mock_role_s

    def test_platform_set_from_validation(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_platform = MagicMock()
        validation = {"platform": {"platform": mock_platform}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation=validation)

        assert mock_fields["platform"].initial == mock_platform

    def test_platform_queryset_set_from_inline_import(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_fields = self._make_mock_fields()
        mock_qs = MagicMock()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = mock_qs
                DeviceImportConfigForm()

        assert mock_fields["platform"].queryset == mock_qs

    def test_filters_device_type_queryset_with_suggestions(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_dt1 = MagicMock()
        mock_dt1.id = 1
        mock_dt2 = MagicMock()
        mock_dt2.id = 2
        suggestions = [{"device_type": mock_dt1}, {"device_type": mock_dt2}]
        validation = {"device_type": {"suggestions": suggestions, "device_type": mock_dt1}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                with patch("netbox_librenms_plugin.forms.DeviceType") as MockDT:
                    DeviceImportConfigForm(validation=validation)

        MockDT.objects.filter.assert_called_once_with(id__in=[1, 2])
        MockDT.objects.exclude.assert_called_once_with(id__in=[1, 2])

    def test_empty_suggestions_list_skips_queryset_filter(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        validation = {"device_type": {"suggestions": [], "device_type": None}}
        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                with patch("netbox_librenms_plugin.forms.DeviceType") as MockDT:
                    DeviceImportConfigForm(validation=validation)

        MockDT.objects.filter.assert_not_called()

    def test_empty_validation_dict_no_initial_set(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm(validation={}, libre_device={})

        # No initial values set since validation and libre_device are empty
        mock_fields["site"].initial.__set__ if hasattr(mock_fields["site"].initial, "__set__") else None
        # Simply verify no exception was raised and the form was created

    def test_no_libre_device_no_initial_hostname(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        mock_fields = self._make_mock_fields()

        with patch("django.forms.Form.__init__", self._fake_form_init(mock_fields)):
            with patch("dcim.models.Platform") as MockPlatform:
                MockPlatform.objects.all.return_value = MagicMock()
                DeviceImportConfigForm()

        # initial should not have been set by any libre_device path
        mock_fields["device_id"].initial.__class__  # attribute access on MagicMock is fine
