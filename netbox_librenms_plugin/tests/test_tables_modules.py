"""Tests for netbox_librenms_plugin.tables.modules module.

Covers LibreNMSModuleTable render_* methods by calling them directly,
bypassing __init__ with object.__new__.  No DB access required.
"""

from unittest.mock import MagicMock, patch


class TestLibreNMSModuleTable:
    """Direct unit tests for every render_* method on LibreNMSModuleTable."""

    def _make_table(self, device=None):
        """Create a bare table instance without calling __init__."""
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        table.device = device
        table.csrf_token = "test-csrf-token"
        return table

    # ------------------------------------------------------------------
    # render_name
    # ------------------------------------------------------------------

    def test_render_name_depth_zero_returns_value(self):
        """At depth 0 the raw value is returned unchanged."""
        table = self._make_table()
        result = table.render_name("Router", {"depth": 0})
        assert result == "Router"

    def test_render_name_depth_zero_none_value_returns_dash(self):
        """At depth 0, None value is replaced with '-'."""
        table = self._make_table()
        result = table.render_name(None, {"depth": 0})
        assert result == "-"

    def test_render_name_depth_zero_missing_depth_defaults_to_zero(self):
        """When 'depth' key is absent the record defaults to 0."""
        table = self._make_table()
        result = table.render_name("Switch", {})
        assert result == "Switch"

    def test_render_name_depth_nonzero_contains_indent_and_prefix(self):
        """Non-zero depth produces a padded span with tree prefix."""
        table = self._make_table()
        result = table.render_name("Card", {"depth": 2})
        result_str = str(result)
        assert "padding-left:40px" in result_str  # 2 * 20 = 40
        assert "└─" in result_str
        assert "Card" in result_str

    def test_render_name_depth_one_correct_padding(self):
        """Depth 1 produces 20 px of padding."""
        table = self._make_table()
        result = table.render_name("Sub", {"depth": 1})
        result_str = str(result)
        assert "padding-left:20px" in result_str

    def test_render_name_depth_nonzero_none_value_shows_dash(self):
        """None value at non-zero depth falls back to '-'."""
        table = self._make_table()
        result = table.render_name(None, {"depth": 3})
        result_str = str(result)
        assert "-" in result_str

    # ------------------------------------------------------------------
    # render_model
    # ------------------------------------------------------------------

    def test_render_model_empty_string_returns_dash(self):
        """Empty string value returns '-'."""
        table = self._make_table()
        assert table.render_model("", {}) == "-"

    def test_render_model_dash_value_returns_dash(self):
        """Literal '-' value returns '-'."""
        table = self._make_table()
        assert table.render_model("-", {}) == "-"

    def test_render_model_none_returns_dash(self):
        """None value returns '-'."""
        table = self._make_table()
        assert table.render_model(None, {}) == "-"

    def test_render_model_with_url_returns_link(self):
        """When module_type_url is present, a hyperlink is rendered."""
        table = self._make_table()
        result = str(table.render_model("C9300", {"module_type_url": "/dcim/module-types/1/"}))
        assert 'href="/dcim/module-types/1/"' in result
        assert "C9300" in result

    def test_render_model_without_url_returns_plain_value(self):
        """Without a URL the plain value string is returned."""
        table = self._make_table()
        result = table.render_model("C9300", {})
        assert result == "C9300"

    # ------------------------------------------------------------------
    # render_serial
    # ------------------------------------------------------------------

    def test_render_serial_empty_returns_dash(self):
        """Empty serial returns '-'."""
        table = self._make_table()
        assert table.render_serial("", {}) == "-"

    def test_render_serial_none_returns_dash(self):
        """None serial returns '-'."""
        table = self._make_table()
        assert table.render_serial(None, {}) == "-"

    def test_render_serial_present_returns_value(self):
        """Non-empty serial is returned as-is."""
        table = self._make_table()
        assert table.render_serial("SN12345", {}) == "SN12345"

    # ------------------------------------------------------------------
    # render_description
    # ------------------------------------------------------------------

    def test_render_description_none_returns_dash(self):
        """None description returns '-'."""
        table = self._make_table()
        assert table.render_description(None, {}) == "-"

    def test_render_description_empty_string_returns_dash(self):
        """Empty string description returns '-'."""
        table = self._make_table()
        assert table.render_description("", {}) == "-"

    def test_render_description_short_returns_value(self):
        """Short description (≤60 chars) is returned unchanged."""
        table = self._make_table()
        short = "Short description"
        assert table.render_description(short, {}) == short

    def test_render_description_exactly_60_chars_not_truncated(self):
        """Description of exactly 60 chars is returned unchanged."""
        table = self._make_table()
        exact = "A" * 60
        assert table.render_description(exact, {}) == exact

    def test_render_description_long_truncated_with_ellipsis(self):
        """Description longer than 60 chars is truncated and has hellip."""
        table = self._make_table()
        long_desc = "B" * 65
        result = str(table.render_description(long_desc, {}))
        assert "B" * 57 in result
        assert "&hellip;" in result

    def test_render_description_long_title_contains_full_value(self):
        """The title attribute of the truncated span contains the full value."""
        table = self._make_table()
        long_desc = "C" * 70
        result = str(table.render_description(long_desc, {}))
        assert long_desc in result  # full value in title attribute

    # ------------------------------------------------------------------
    # render_item_class
    # ------------------------------------------------------------------

    def test_render_item_class_module_uses_expansion_card_icon(self):
        """'module' class uses the mdi-expansion-card icon."""
        table = self._make_table()
        result = str(table.render_item_class("module", {}))
        assert "mdi-expansion-card" in result
        assert "module" in result

    def test_render_item_class_fan_uses_fan_icon(self):
        """'fan' class uses the mdi-fan icon."""
        table = self._make_table()
        result = str(table.render_item_class("fan", {}))
        assert "mdi-fan" in result

    def test_render_item_class_power_supply_uses_plug_icon(self):
        """'powerSupply' class uses the mdi-power-plug icon."""
        table = self._make_table()
        result = str(table.render_item_class("powerSupply", {}))
        assert "mdi-power-plug" in result

    def test_render_item_class_port_uses_ethernet_icon(self):
        """'port' class uses the mdi-ethernet icon."""
        table = self._make_table()
        result = str(table.render_item_class("port", {}))
        assert "mdi-ethernet" in result

    def test_render_item_class_unknown_uses_default_icon(self):
        """Unknown class falls back to mdi-card-outline icon."""
        table = self._make_table()
        result = str(table.render_item_class("unknown_class", {}))
        assert "mdi-card-outline" in result

    def test_render_item_class_io_module_variant(self):
        """'ioModule' is also mapped to expansion-card."""
        table = self._make_table()
        result = str(table.render_item_class("ioModule", {}))
        assert "mdi-expansion-card" in result

    # ------------------------------------------------------------------
    # render_module_bay
    # ------------------------------------------------------------------

    def test_render_module_bay_none_shows_no_matching_bay(self):
        """None value shows the 'No matching bay' danger span."""
        table = self._make_table()
        result = str(table.render_module_bay(None, {}))
        assert "text-danger" in result
        assert "No matching bay" in result

    def test_render_module_bay_dash_shows_no_matching_bay(self):
        """Literal '-' shows the 'No matching bay' danger span."""
        table = self._make_table()
        result = str(table.render_module_bay("-", {}))
        assert "text-danger" in result

    def test_render_module_bay_empty_shows_no_matching_bay(self):
        """Empty string shows the 'No matching bay' danger span."""
        table = self._make_table()
        result = str(table.render_module_bay("", {}))
        assert "text-danger" in result

    def test_render_module_bay_with_url_renders_link(self):
        """When module_bay_url is present, a hyperlink is rendered."""
        table = self._make_table()
        result = str(table.render_module_bay("Bay 1", {"module_bay_url": "/dcim/module-bays/5/"}))
        assert 'href="/dcim/module-bays/5/"' in result
        assert "Bay 1" in result

    def test_render_module_bay_without_url_returns_plain_value(self):
        """Without URL the plain bay name is returned."""
        table = self._make_table()
        result = table.render_module_bay("Bay 1", {})
        assert result == "Bay 1"

    # ------------------------------------------------------------------
    # render_module_type
    # ------------------------------------------------------------------

    def test_render_module_type_none_shows_no_matching_type(self):
        """None value shows the 'No matching type' warning span."""
        table = self._make_table()
        result = str(table.render_module_type(None, {}))
        assert "text-warning" in result
        assert "No matching type" in result

    def test_render_module_type_dash_shows_no_matching_type(self):
        """Literal '-' shows the 'No matching type' warning span."""
        table = self._make_table()
        result = str(table.render_module_type("-", {}))
        assert "text-warning" in result

    def test_render_module_type_with_url_renders_link(self):
        """When module_type_url is present, a hyperlink is rendered."""
        table = self._make_table()
        result = str(table.render_module_type("C9300-NM-8X", {"module_type_url": "/dcim/module-types/10/"}))
        assert 'href="/dcim/module-types/10/"' in result
        assert "C9300-NM-8X" in result

    def test_render_module_type_without_url_returns_plain_value(self):
        """Without URL the plain type name is returned."""
        table = self._make_table()
        result = table.render_module_type("C9300-NM-8X", {})
        assert result == "C9300-NM-8X"

    # ------------------------------------------------------------------
    # render_status
    # ------------------------------------------------------------------

    def test_render_status_installed_uses_success_badge(self):
        """'Installed' status renders a bg-success badge."""
        table = self._make_table()
        result = str(table.render_status("Installed", {}))
        assert "bg-success" in result
        assert "Installed" in result

    def test_render_status_matched_uses_info_badge(self):
        """'Matched' status renders a bg-info badge."""
        table = self._make_table()
        result = str(table.render_status("Matched", {}))
        assert "bg-info" in result

    def test_render_status_no_bay_uses_warning_badge(self):
        """'No Bay' status renders a bg-warning badge."""
        table = self._make_table()
        result = str(table.render_status("No Bay", {}))
        assert "bg-warning" in result

    def test_render_status_serial_mismatch_uses_danger_badge(self):
        """'Serial Mismatch' status renders a bg-danger badge."""
        table = self._make_table()
        result = str(table.render_status("Serial Mismatch", {}))
        assert "bg-danger" in result
        assert "Serial Mismatch" in result

    def test_render_status_unknown_value_uses_secondary_badge(self):
        """Unknown status value falls back to bg-secondary."""
        table = self._make_table()
        result = str(table.render_status("Weird Status", {}))
        assert "bg-secondary" in result

    def test_render_status_with_module_path_warning_adds_alert_icon(self):
        """module_path_warning adds an mdi-alert-outline icon."""
        table = self._make_table()
        result = str(table.render_status("Installed", {"module_path_warning": "Upgrade NetBox for module_path"}))
        assert "mdi-alert-outline" in result
        assert "Upgrade NetBox for module_path" in result
        assert "bg-success" in result

    def test_render_status_with_name_conflict_warning_adds_alert_icon(self):
        """name_conflict_warning adds an mdi-alert-outline icon with the warning text."""
        table = self._make_table()
        result = str(
            table.render_status("Name Conflict", {"name_conflict_warning": "Name already used by another module"})
        )
        assert "mdi-alert-outline" in result
        assert "Name already used by another module" in result

    def test_render_status_with_module_type_upgrade_hint_adds_info_icon(self):
        """module_type_upgrade_hint adds an mdi-information-outline icon."""
        table = self._make_table()
        result = str(table.render_status("Matched", {"module_type_upgrade_hint": "Newer module type available"}))
        assert "mdi-information-outline" in result
        assert "Newer module type available" in result

    def test_render_status_module_path_warning_takes_priority_over_hint(self):
        """module_path_warning short-circuits before module_type_upgrade_hint."""
        table = self._make_table()
        # Both present — path_warning fires first
        result = str(
            table.render_status(
                "Installed",
                {
                    "module_path_warning": "path warning",
                    "module_type_upgrade_hint": "hint text",
                },
            )
        )
        assert "mdi-alert-outline" in result
        # hint icon should NOT be present
        assert "mdi-information-outline" not in result

    # ------------------------------------------------------------------
    # render_actions
    # ------------------------------------------------------------------

    def test_render_actions_no_device_returns_empty_string(self):
        """Returns empty string when no device is set on the table."""
        table = self._make_table(device=None)
        result = table.render_actions(None, {"can_install": True})
        assert result == ""

    def test_render_actions_no_buttons_returns_empty_string(self):
        """Returns empty string when record has no actionable flags."""
        device = MagicMock()
        device.pk = 1
        table = self._make_table(device=device)
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/fake/"):
            result = table.render_actions(None, {})
        assert result == ""

    def test_render_actions_can_install_renders_install_button(self):
        """can_install=True renders an Install form button."""
        device = MagicMock()
        device.pk = 1
        table = self._make_table(device=device)
        record = {
            "can_install": True,
            "module_bay_id": 5,
            "module_type_id": 10,
            "serial": "SN123",
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/install-url/"):
            result = str(table.render_actions(None, record))

        assert "Install" in result
        assert "/install-url/" in result
        assert "SN123" in result
        assert "mdi-download" in result

    def test_render_actions_has_installable_children_renders_branch_button(self):
        """has_installable_children + ent_physical_index renders Install Branch button."""
        device = MagicMock()
        device.pk = 2
        table = self._make_table(device=device)
        record = {
            "has_installable_children": True,
            "ent_physical_index": 42,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/branch-url/"):
            result = str(table.render_actions(None, record))

        assert "Install Branch" in result
        assert "/branch-url/" in result
        assert "mdi-file-tree" in result

    def test_render_actions_both_buttons_rendered(self):
        """Both Install and Install Branch buttons render when both flags are set."""
        device = MagicMock()
        device.pk = 3
        table = self._make_table(device=device)
        record = {
            "can_install": True,
            "module_bay_id": 1,
            "module_type_id": 2,
            "serial": "SN-BOTH",
            "has_installable_children": True,
            "ent_physical_index": 99,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Install" in result
        assert "Install Branch" in result

    def test_render_actions_installable_children_without_index_skips_branch(self):
        """has_installable_children without ent_physical_index skips branch button."""
        device = MagicMock()
        device.pk = 4
        table = self._make_table(device=device)
        record = {
            "has_installable_children": True,
            # ent_physical_index intentionally absent
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = table.render_actions(None, record)

        assert result == ""

    def test_render_actions_csrf_token_included_in_form(self):
        """The CSRF token stored on the table is embedded in install form."""
        device = MagicMock()
        device.pk = 5
        table = self._make_table(device=device)
        table.csrf_token = "my-csrf-value"
        record = {"can_install": True, "module_bay_id": 1, "module_type_id": 1, "serial": ""}
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "my-csrf-value" in result


class TestLibreNMSModuleTableInit:
    """Tests for __init__ and configure methods, bypassing django-tables2 super().__init__."""

    def test_init_sets_device_and_defaults(self):
        """__init__ sets device, tab, prefix, htmx_url, csrf_token attributes."""
        import django_tables2 as dt2
        from unittest.mock import MagicMock, patch

        device = MagicMock()
        with patch.object(dt2.Table, "__init__", return_value=None):
            from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

            table = LibreNMSModuleTable(device=device)

        assert table.device is device
        assert table.tab == "modules"
        assert table.prefix == "modules_"
        assert table.htmx_url is None
        assert table.csrf_token == ""

    def test_init_without_device_defaults_to_none(self):
        """__init__ with no device argument sets device=None."""
        import django_tables2 as dt2
        from unittest.mock import patch

        with patch.object(dt2.Table, "__init__", return_value=None):
            from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

            table = LibreNMSModuleTable()

        assert table.device is None

    def test_configure_sets_csrf_token(self):
        """configure() sets csrf_token from get_token and calls RequestConfig."""
        import django_tables2 as dt2
        from unittest.mock import MagicMock, patch

        with patch.object(dt2.Table, "__init__", return_value=None):
            from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

            table = LibreNMSModuleTable()

        request = MagicMock()
        mock_rc_instance = MagicMock()

        with (
            patch("netbox_librenms_plugin.tables.modules.get_table_paginate_count", return_value=25),
            patch("django.middleware.csrf.get_token", return_value="csrf-abc"),
            patch("django_tables2.RequestConfig", return_value=mock_rc_instance) as mock_rc_cls,
        ):
            table.configure(request)

        assert table.csrf_token == "csrf-abc"
        mock_rc_cls.assert_called_once()
        mock_rc_instance.configure.assert_called_once_with(table)
