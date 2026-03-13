"""Coverage tests for netbox_librenms_plugin.forms — has_option_only empty-data guard."""

from unittest.mock import patch


class TestLibreNMSFilterFormBackgroundJobDefault:
    """Tests for use_background_job default injection in LibreNMSImportFilterForm.__init__.

    The form checks args[0] (positional) for the data dict, matching how Django
    binds forms from request.GET/POST. Tests pass data positionally to match.
    """

    def _make_form(self, data):
        """Instantiate LibreNMSImportFilterForm with mocked server settings.
        Pass data as positional arg to match how Django provides request.GET.
        """
        with (
            patch("netbox_librenms_plugin.forms.LibreNMSImportFilterForm._populate_librenms_locations"),
        ):
            from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

            # Pass data positionally — the form's __init__ checks args[0], not kwargs
            return LibreNMSImportFilterForm(data)

    def test_empty_data_sets_use_background_job_on(self):
        """LibreNMSImportFilterForm({}) should set use_background_job='on' (initial GET)."""
        form = self._make_form({})
        assert form.data.get("use_background_job") == "on"

    def test_option_only_data_does_not_auto_set_background_job(self):
        """Submitting only option-only fields should NOT auto-set use_background_job."""
        # show_disabled is an option-only field — not a real filter field
        # has_option_only = bool({show_disabled}) and not non_option_fields and not has_filters
        # = True and True and True = True → condition fails → use_background_job NOT injected
        form = self._make_form({"show_disabled": "on"})
        assert form.data.get("use_background_job") is None

    def test_filter_data_does_not_auto_set_background_job(self):
        """When real filter fields are submitted, use_background_job is not auto-injected."""
        form = self._make_form({"librenms_hostname": "switch01"})
        assert form.data.get("use_background_job") is None

    def test_use_background_job_preserved_when_already_set(self):
        """If use_background_job is already in data, it should not be overridden."""
        form = self._make_form({"use_background_job": "off"})
        assert form.data.get("use_background_job") == "off"

    def test_no_positional_args_no_injection(self):
        """Unbound form (no positional args) should not inject use_background_job."""
        with (
            patch("netbox_librenms_plugin.forms.LibreNMSImportFilterForm._populate_librenms_locations"),
        ):
            from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

            form = LibreNMSImportFilterForm()
        assert form.data.get("use_background_job") is None

    def test_pagination_param_does_not_inject_background_job(self):
        """Auxiliary params like 'page' must not trigger background-job default injection."""
        form = self._make_form({"page": "2"})
        assert form.data.get("use_background_job") is None


class TestPollerGroupCacheKeyServerScoped:
    """Finding 6: verify the poller-group cache key is scoped by server_key."""

    def test_cache_hit_returns_cached_choices(self):
        """Cache lookup uses the server-scoped key; hit returns cached value without API call."""
        from unittest.mock import MagicMock, patch

        mock_api = MagicMock()
        mock_api.server_key = "prod"
        cached = [("0", "Default (0)"), ("1", "Group1 (1)")]

        with (
            patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api),
            patch("django.core.cache.cache") as mock_cache,
        ):
            mock_cache.get.return_value = cached
            from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

            result = _get_librenms_poller_group_choices()

        expected_key = f"librenms_poller_group_choices_{mock_api.server_key}"
        mock_cache.get.assert_called_once_with(expected_key)
        assert result is cached

    def test_cache_miss_calls_api_and_stores_server_scoped_key(self):
        """Cache miss fetches from API and stores under the server-scoped key."""
        from unittest.mock import MagicMock, patch

        mock_api = MagicMock()
        mock_api.server_key = "staging"
        mock_api.cache_timeout = 60
        mock_api.get_poller_groups.return_value = (True, [{"id": 1, "group_name": "G1", "descr": ""}])

        with (
            patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", return_value=mock_api),
            patch("django.core.cache.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None  # cache miss
            from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

            _get_librenms_poller_group_choices()

        expected_key = f"librenms_poller_group_choices_{mock_api.server_key}"
        set_call_key = mock_cache.set.call_args[0][0]
        assert set_call_key == expected_key


class TestQueryDictNotMutated:
    """Finding 7: LibreNMSImportFilterForm must not mutate the original QueryDict."""

    def test_querydict_original_not_modified(self):
        """When a QueryDict without use_background_job is passed, the original must be unchanged."""
        from django.http import QueryDict
        from unittest.mock import patch

        # An empty QueryDict has no fields — should trigger auto-inject of use_background_job,
        # but only on the copy, not on the original.
        qd = QueryDict("")
        assert qd.get("use_background_job") is None

        with (
            patch("netbox_librenms_plugin.forms.LibreNMSImportFilterForm._populate_librenms_locations"),
        ):
            from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

            LibreNMSImportFilterForm(qd)

        # Original QueryDict must remain unmodified
        assert qd.get("use_background_job") is None


class TestDeviceImportConfigFormInitialValues:
    """Finding 8: DeviceImportConfigForm sets/skips initial values correctly."""

    def _make_form(self, libre_device=None, validation=None):
        from unittest.mock import patch

        with (
            patch("dcim.models.Platform"),
            patch("dcim.models.Site"),
            patch("dcim.models.DeviceType"),
            patch("dcim.models.DeviceRole"),
        ):
            from netbox_librenms_plugin.forms import DeviceImportConfigForm

            return DeviceImportConfigForm(
                libre_device=libre_device or {},
                validation=validation or {},
            )

    def test_empty_validation_dict_no_initial_set(self):
        """With empty libre_device and validation, no field initials should be set by the form."""
        from unittest.mock import patch

        with (
            patch("dcim.models.Platform"),
            patch("dcim.models.Site"),
            patch("dcim.models.DeviceType"),
            patch("dcim.models.DeviceRole"),
        ):
            from netbox_librenms_plugin.forms import DeviceImportConfigForm

            form = DeviceImportConfigForm(libre_device={}, validation={})

        # Fields populated from libre_device should have blank/None initials when no data given
        assert form.fields["hostname"].initial in (None, "")
        assert form.fields["hardware"].initial in (None, "")

    def test_libre_device_sets_initial_hostname(self):
        """libre_device dict populates hostname and hardware initial values."""
        from unittest.mock import patch

        libre_device = {
            "device_id": 5,
            "hostname": "sw01",
            "hardware": "Cisco 3850",
            "location": "NYC",
        }

        with (
            patch("dcim.models.Platform"),
            patch("dcim.models.Site"),
            patch("dcim.models.DeviceType"),
            patch("dcim.models.DeviceRole"),
        ):
            from netbox_librenms_plugin.forms import DeviceImportConfigForm

            form = DeviceImportConfigForm(libre_device=libre_device, validation={})

        assert form.fields["hostname"].initial == "sw01"
        assert form.fields["hardware"].initial == "Cisco 3850"
        assert form.fields["device_id"].initial == 5
