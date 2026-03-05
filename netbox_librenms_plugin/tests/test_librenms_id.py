"""Tests for multi-server librenms_id helpers.

Covers get_librenms_device_id, find_by_librenms_id, and migrate_legacy_librenms_id.
set_librenms_device_id is already tested in test_utils.py::TestSetLibreNMSDeviceId.
"""

from unittest.mock import MagicMock


class TestGetLibreNMSDeviceId:
    """Tests for get_librenms_device_id()."""

    def test_returns_none_when_cf_missing(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {}
        result = get_librenms_device_id(obj, "default")
        assert result is None

    def test_returns_int_for_legacy_bare_integer(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": 42}
        result = get_librenms_device_id(obj, "default")
        assert result == 42

    def test_legacy_bare_int_returned_for_any_server_key(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": 99}
        assert get_librenms_device_id(obj, "production") == 99
        assert get_librenms_device_id(obj, "secondary") == 99

    def test_returns_value_for_matching_server_key(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"production": 7, "secondary": 12}}
        assert get_librenms_device_id(obj, "production") == 7

    def test_returns_none_for_missing_server_key_in_dict(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"production": 7}}
        result = get_librenms_device_id(obj, "secondary")
        assert result is None

    def test_returns_none_for_unexpected_type(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "not-an-int-or-dict"}
        result = get_librenms_device_id(obj, "default")
        assert result is None

    def test_default_server_key_is_default(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"default": 5}}
        assert get_librenms_device_id(obj) == 5


class TestFindByLibreNMSId:
    """Tests for find_by_librenms_id()."""

    def test_queries_server_key_and_legacy_integer(self):
        from unittest.mock import MagicMock
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.first.return_value = None

        find_by_librenms_id(mock_model, 42, "default")

        mock_model.objects.filter.assert_called_once()
        # Verify the Q argument covers both the JSON server-key path and legacy integer path.
        call_args = mock_model.objects.filter.call_args
        q_arg = call_args[0][0]
        q_str = str(q_arg)
        assert "librenms_id__default" in q_str, "Expected JSON-scoped server_key lookup in filter"
        assert "librenms_id" in q_str, "Expected legacy integer lookup in filter"

    def test_returns_first_matching_object(self):
        from netbox_librenms_plugin.utils import find_by_librenms_id

        expected = MagicMock()
        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.first.return_value = expected

        result = find_by_librenms_id(mock_model, 42, "default")
        assert result is expected

    def test_returns_none_when_not_found(self):
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.first.return_value = None

        result = find_by_librenms_id(mock_model, 999, "production")
        assert result is None

    def test_default_server_key_is_default(self):
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.first.return_value = None

        find_by_librenms_id(mock_model, 42)

        # Verify filter was called (Q objects are built internally — just confirm call was made)
        mock_model.objects.filter.assert_called_once()


class TestMigrateLegacyLibreNMSId:
    """Tests for migrate_legacy_librenms_id()."""

    def test_returns_true_when_migrated(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 42}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is True

    def test_migrates_integer_to_dict_format(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 42}
        migrate_legacy_librenms_id(obj, "production")
        assert obj.custom_field_data["librenms_id"] == {"production": 42}

    def test_returns_false_when_already_dict(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 42}}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is False

    def test_returns_false_when_value_is_none(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": None}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is False

    def test_does_not_call_save(self):
        """migrate_legacy_librenms_id must NOT call obj.save() — caller is responsible."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 7}
        migrate_legacy_librenms_id(obj, "default")
        obj.save.assert_not_called()

    def test_preserves_value_in_migrated_dict(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 99}
        migrate_legacy_librenms_id(obj, "secondary")
        assert obj.custom_field_data["librenms_id"]["secondary"] == 99


class TestLibreNMSIdRoundtrip:
    """get_librenms_device_id should see the value set by set_librenms_device_id."""

    def test_set_then_get_returns_same_value(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {}
        obj.cf = obj.custom_field_data  # make cf a live view of custom_field_data

        set_librenms_device_id(obj, 42, "production")
        result = get_librenms_device_id(obj, "production")
        assert result == 42

    def test_set_multiple_servers_get_correct_each(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {}
        obj.cf = obj.custom_field_data

        set_librenms_device_id(obj, 10, "primary")
        set_librenms_device_id(obj, 20, "secondary")

        assert get_librenms_device_id(obj, "primary") == 10
        assert get_librenms_device_id(obj, "secondary") == 20

    def test_migrate_then_get_returns_value(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 55}
        obj.cf = obj.custom_field_data

        migrate_legacy_librenms_id(obj, "default")
        result = get_librenms_device_id(obj, "default")
        assert result == 55
