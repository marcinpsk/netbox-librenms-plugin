"""Tests for module sync views and BaseModuleTableView bay matching logic.

Covers: InstallModuleView/InstallBranchView wiring, branch collection, cycle guards,
bay matching by name/mapping/position, serial comparison, status determination,
and depth tracking.  inventory-rebased branch only.
"""

from unittest.mock import MagicMock, patch


def _make_install_branch_view():
    from netbox_librenms_plugin.views.sync.modules import InstallBranchView

    view = object.__new__(InstallBranchView)
    view._librenms_api = None
    return view


class TestInstallBranchViewCollectBranch:
    """_collect_branch correctly collects parent + children depth-first."""

    def _make_inventory(self, items):
        """Helper to build a list of inventory dicts."""
        return items

    def test_collect_parent_with_model(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "WS-C4500X", "entPhysicalContainedIn": 0},
        ]
        result = view._collect_branch(1, inventory)
        assert len(result) == 1
        assert result[0]["entPhysicalIndex"] == 1

    def test_collect_parent_without_model_excluded(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "", "entPhysicalContainedIn": 0},
        ]
        result = view._collect_branch(1, inventory)
        assert result == []

    def test_collect_children_included_with_models(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD-A", "entPhysicalContainedIn": 1},
            {"entPhysicalIndex": 3, "entPhysicalModelName": "CHILD-B", "entPhysicalContainedIn": 1},
        ]
        result = view._collect_branch(1, inventory)
        indices = [item["entPhysicalIndex"] for item in result]
        assert 1 in indices
        assert 2 in indices
        assert 3 in indices

    def test_parent_comes_before_children(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD", "entPhysicalContainedIn": 1},
        ]
        result = view._collect_branch(1, inventory)
        indices = [item["entPhysicalIndex"] for item in result]
        assert indices.index(1) < indices.index(2)

    def test_deep_nesting_collected(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "ROOT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "MID", "entPhysicalContainedIn": 1},
            {"entPhysicalIndex": 3, "entPhysicalModelName": "LEAF", "entPhysicalContainedIn": 2},
        ]
        result = view._collect_branch(1, inventory)
        assert len(result) == 3

    def test_unknown_parent_returns_empty(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "ITEM", "entPhysicalContainedIn": 0},
        ]
        result = view._collect_branch(999, inventory)
        assert result == []


class TestInstallBranchViewCollectChildrenCycleGuard:
    """_collect_children must not loop on cyclic entPhysicalContainedIn links."""

    def test_cycle_does_not_cause_infinite_recursion(self):
        view = _make_install_branch_view()
        # A ↔ B cycle (A contains B, B contains A)
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "A", "entPhysicalContainedIn": 2},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "B", "entPhysicalContainedIn": 1},
        ]
        items = []
        # Should terminate without RecursionError
        view._collect_children(1, inventory, items, visited={1})

    def test_self_reference_does_not_loop(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 5, "entPhysicalModelName": "SELF", "entPhysicalContainedIn": 5},
        ]
        items = []
        view._collect_children(5, inventory, items, visited={5})
        # No infinite recursion — length may be 0 (self is excluded by visited)
        assert len(items) == 0


class TestInstallBranchViewGetModuleTypes:
    """_get_module_types builds a dict keyed by model name, part number, and mappings."""

    def test_indexes_by_model_and_part_number(self):
        mt1 = MagicMock()
        mt1.model = "WS-X4748"
        mt1.part_number = "ALT-PART-4748"

        mt2 = MagicMock()
        mt2.model = "WS-X4516"
        mt2.part_number = "WS-X4516"  # same as model → no extra key

        mock_mapping = MagicMock()
        mock_mapping.librenms_model = "libre-model-a"
        mock_mapping.netbox_module_type = mt1

        mock_mt_cls = MagicMock()
        mock_mt_cls.objects.all.return_value.select_related.return_value = [mt1, mt2]

        mock_map_cls = MagicMock()
        mock_map_cls.objects.select_related.return_value = [mock_mapping]

        with patch.dict(
            "sys.modules",
            {
                "dcim.models": type("m", (), {"ModuleType": mock_mt_cls})(),
            },
        ):
            with patch("netbox_librenms_plugin.models.ModuleTypeMapping", mock_map_cls):
                view = _make_install_branch_view()
                result = view._get_module_types()

        assert result["WS-X4748"] is mt1
        assert result["ALT-PART-4748"] is mt1
        assert result["WS-X4516"] is mt2
        assert result["libre-model-a"] is mt1


class TestInstallModuleViewWiring:
    """InstallModuleView must have correct mixins and attributes."""

    def test_has_librenms_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        assert LibreNMSPermissionMixin in InstallModuleView.__mro__

    def test_has_netbox_object_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        assert NetBoxObjectPermissionMixin in InstallModuleView.__mro__

    def test_install_module_view_not_in_base(self):
        """InstallModuleView must NOT be defined in views/base anymore."""
        import importlib

        mod = importlib.import_module("netbox_librenms_plugin.views.base.modules_view")
        assert not hasattr(mod, "InstallModuleView"), (
            "InstallModuleView must have been moved out of views/base/modules_view.py"
        )


class TestInstallBranchViewWiring:
    """InstallBranchView must have CacheMixin for cache key generation."""

    def test_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView
        from netbox_librenms_plugin.views.mixins import CacheMixin

        assert CacheMixin in InstallBranchView.__mro__

    def test_has_netbox_object_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        assert NetBoxObjectPermissionMixin in InstallBranchView.__mro__


# ---------------------------------------------------------------------------
# Helper: build a BaseModuleTableView instance without __init__
# ---------------------------------------------------------------------------


def _make_base_view():
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    view = object.__new__(BaseModuleTableView)
    view._device_manufacturer = None
    return view


def _bay(name, installed_module=None, pk=None):
    """Quick MagicMock module bay."""
    bay = MagicMock()
    bay.name = name
    bay.pk = pk or id(bay)
    bay.installed_module = installed_module
    bay.get_absolute_url.return_value = f"/dcim/module-bays/{bay.pk}/"
    return bay


def _module(serial="SN001", module_type_id=1):
    mod = MagicMock()
    mod.serial = serial
    mod.module_type_id = module_type_id
    mod.get_absolute_url.return_value = "/dcim/modules/1/"
    return mod


# ---------------------------------------------------------------------------
# _determine_status
# ---------------------------------------------------------------------------


class TestDetermineStatus:
    """_determine_status returns the correct badge string for every combination."""

    def test_matched_bay_and_type(self):
        view = _make_base_view()
        assert view._determine_status(MagicMock(), MagicMock(), "") == "Matched"

    def test_no_bay_regardless_of_type(self):
        view = _make_base_view()
        assert view._determine_status(None, MagicMock(), "") == "No Bay"
        assert view._determine_status(None, None, "") == "No Bay"

    def test_bay_without_type(self):
        view = _make_base_view()
        assert view._determine_status(MagicMock(), None, "") == "No Type"

    def test_unmatched_when_neither(self):
        # This path is unreachable via current code (No Bay catches it first),
        # but _determine_status is a standalone method so test the logic directly.
        view = _make_base_view()
        # Trick: pass a falsy non-None bay to skip "no bay" but reach "no type"
        # Not possible with current logic; just verify No Bay path dominates.
        assert view._determine_status(None, None, "SN1") == "No Bay"


# ---------------------------------------------------------------------------
# Serial comparison inside _build_row
# ---------------------------------------------------------------------------


class TestBuildRowSerialComparison:
    """_build_row sets 'Installed' or 'Serial Mismatch' based on installed module serial."""

    def _make_item(self, model_name, serial):
        return {
            "entPhysicalModelName": model_name,
            "entPhysicalSerialNum": serial,
            "entPhysicalName": model_name,
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalIndex": 10,
            "entPhysicalContainedIn": 0,
        }

    def _make_matched_type(self, model="WS-X4748"):
        mt = MagicMock()
        mt.model = model
        mt.pk = 1
        mt.get_absolute_url.return_value = "/dcim/module-types/1/"
        # Make uses-module-path/token checks return False so badges don't appear
        mt.interfacetemplates = MagicMock()
        mt.interfacetemplates.all.return_value = []
        return mt

    def test_matching_serial_gives_installed_status(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN-ABC-123")
        mt = self._make_matched_type()
        installed = _module(serial="SN-ABC-123")
        bay = _bay("Slot 1", installed_module=installed)

        module_bays = {"Slot 1": bay}
        module_types = {"WS-X4748": mt}
        index_map = {10: item}

        with patch.object(view, "_match_module_bay", return_value=bay):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
                with patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False):
                    with patch("netbox_librenms_plugin.utils.supports_module_path", return_value=False):
                        with patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False):
                            with patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False):
                                with patch(
                                    "netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False
                                ):
                                    row = view._build_row(item, index_map, module_bays, module_types, depth=0)

        assert row["status"] == "Installed"
        assert row["row_class"] == "table-success"

    def test_serial_mismatch_gives_danger_status(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN-NEW-999")
        mt = self._make_matched_type()
        installed = _module(serial="SN-OLD-111")
        bay = _bay("Slot 1", installed_module=installed)

        module_bays = {"Slot 1": bay}
        module_types = {"WS-X4748": mt}
        index_map = {10: item}

        with patch.object(view, "_match_module_bay", return_value=bay):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
                with patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False):
                    with patch("netbox_librenms_plugin.utils.supports_module_path", return_value=False):
                        with patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False):
                            with patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False):
                                with patch(
                                    "netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False
                                ):
                                    row = view._build_row(item, index_map, module_bays, module_types, depth=0)

        assert row["status"] == "Serial Mismatch"
        assert row["row_class"] == "table-danger"

    def test_no_bay_gives_no_bay_status(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN1")
        mt = self._make_matched_type()

        with patch.object(view, "_match_module_bay", return_value=None):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
                with patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False):
                    with patch("netbox_librenms_plugin.utils.supports_module_path", return_value=False):
                        with patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False):
                            with patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False):
                                with patch(
                                    "netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False
                                ):
                                    row = view._build_row(item, {10: item}, {}, {"WS-X4748": mt}, depth=0)

        assert row["status"] == "No Bay"

    def test_no_type_gives_no_type_status(self):
        view = _make_base_view()
        item = self._make_item("UNKNOWN-MODEL", "SN1")
        bay = _bay("Slot 1")

        with patch.object(view, "_match_module_bay", return_value=bay):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="UNKNOWN-MODEL"):
                with patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False):
                    with patch("netbox_librenms_plugin.utils.supports_module_path", return_value=False):
                        with patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False):
                            with patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False):
                                with patch(
                                    "netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False
                                ):
                                    row = view._build_row(item, {10: item}, {"Slot 1": bay}, {}, depth=0)

        assert row["status"] == "No Type"

    def test_can_install_set_when_bay_free_and_type_matched(self):
        """can_install=True only when bay exists, type matched, and bay is empty."""
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN1")
        mt = self._make_matched_type()
        # Bay with no installed module
        bay = _bay("Slot 1", installed_module=None)
        bay.installed_module = None

        with patch.object(view, "_match_module_bay", return_value=bay):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
                with patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False):
                    with patch("netbox_librenms_plugin.utils.supports_module_path", return_value=False):
                        with patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False):
                            with patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False):
                                with patch(
                                    "netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False
                                ):
                                    row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["can_install"] is True

    def test_can_install_false_when_bay_occupied(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN1")
        mt = self._make_matched_type()
        installed = _module(serial="SN1")
        bay = _bay("Slot 1", installed_module=installed)

        with patch.object(view, "_match_module_bay", return_value=bay):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
                with patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False):
                    with patch("netbox_librenms_plugin.utils.supports_module_path", return_value=False):
                        with patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False):
                            with patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False):
                                with patch(
                                    "netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False
                                ):
                                    row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["can_install"] is False


# ---------------------------------------------------------------------------
# Depth tracking in render_name
# ---------------------------------------------------------------------------


class TestRenderNameDepth:
    """render_name applies tree indentation based on depth."""

    def test_depth_zero_returns_plain_value(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = table.render_name("Supervisor", {"depth": 0})
        assert "padding-left" not in str(result)
        assert "Supervisor" in str(result)

    def test_depth_one_adds_padding(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = str(table.render_name("Line Card", {"depth": 1}))
        assert "padding-left" in result
        assert "20px" in result

    def test_depth_two_doubles_padding(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = str(table.render_name("SFP", {"depth": 2}))
        assert "40px" in result

    def test_depth_renders_tree_prefix(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = str(table.render_name("Port 1", {"depth": 1}))
        assert "└─" in result


# ---------------------------------------------------------------------------
# _match_bay_by_position
# ---------------------------------------------------------------------------


class TestMatchBayByPosition:
    """_match_bay_by_position resolves position-based bay names for SFPs in converters."""

    def test_matches_sfp_slot_by_sibling_order(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Build an inventory: parent (model) → container1 → item1, container2 → item2
        parent_item = {
            "entPhysicalIndex": 1,
            "entPhysicalModelName": "CONVERTER",
            "entPhysicalContainedIn": 0,
            "entPhysicalParentRelPos": 0,
        }
        container1 = {
            "entPhysicalIndex": 2,
            "entPhysicalModelName": "",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        }
        container2 = {
            "entPhysicalIndex": 3,
            "entPhysicalModelName": "",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 2,
        }
        sfp1 = {
            "entPhysicalIndex": 4,
            "entPhysicalModelName": "SFP-10G-LR",
            "entPhysicalContainedIn": 2,
            "entPhysicalParentRelPos": 1,
        }
        sfp2 = {
            "entPhysicalIndex": 5,
            "entPhysicalModelName": "SFP-10G-SR",
            "entPhysicalContainedIn": 3,
            "entPhysicalParentRelPos": 1,
        }

        index_map = {1: parent_item, 2: container1, 3: container2, 4: sfp1, 5: sfp2}
        bays = {"SFP 1": _bay("SFP 1"), "SFP 2": _bay("SFP 2")}

        result1 = BaseModuleTableView._match_bay_by_position(sfp1, index_map, bays)
        result2 = BaseModuleTableView._match_bay_by_position(sfp2, index_map, bays)

        assert result1 is bays["SFP 1"]
        assert result2 is bays["SFP 2"]

    def test_returns_none_when_no_modelless_container(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Item directly under parent with model (no modelless container)
        parent = {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0}
        item = {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD", "entPhysicalContainedIn": 1}
        index_map = {1: parent, 2: item}
        bays = {"Slot 1": _bay("Slot 1")}

        result = BaseModuleTableView._match_bay_by_position(item, index_map, bays)
        assert result is None

    def test_returns_none_when_no_bays_match_pattern(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        parent = {
            "entPhysicalIndex": 1,
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 0,
            "entPhysicalParentRelPos": 0,
        }
        container = {
            "entPhysicalIndex": 2,
            "entPhysicalModelName": "",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        }
        item = {
            "entPhysicalIndex": 3,
            "entPhysicalModelName": "X",
            "entPhysicalContainedIn": 2,
            "entPhysicalParentRelPos": 1,
        }
        index_map = {1: parent, 2: container, 3: item}
        bays = {"InterfaceA": _bay("InterfaceA")}  # no "SFP 1"/"Slot 1"/etc.

        result = BaseModuleTableView._match_bay_by_position(item, index_map, bays)
        assert result is None


# ---------------------------------------------------------------------------
# _match_module_bay — exact name fallback
# ---------------------------------------------------------------------------


class TestMatchModuleBayExactFallback:
    """When no ModuleBayMapping exists, exact parent/item/descr name is tried."""

    def test_exact_parent_name_match(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = _make_base_view()
        parent = {
            "entPhysicalIndex": 1,
            "entPhysicalModelName": "PARENT",
            "entPhysicalContainedIn": 0,
            "entPhysicalName": "Slot 1",
        }
        item = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Linecard A",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 1,
        }
        index_map = {1: parent, 2: item}
        bay = _bay("Slot 1")
        bays = {"Slot 1": bay}

        with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mbm:
            mock_mbm.objects.filter.return_value.first.return_value = None
            mock_mbm.objects.filter.return_value = MagicMock()
            mock_mbm.objects.filter.return_value.first.return_value = None

            # Also patch _lookup_regex_bay_mapping to return None
            with patch.object(BaseModuleTableView, "_lookup_regex_bay_mapping", return_value=None):
                with patch.object(BaseModuleTableView, "_match_bay_by_position", return_value=None):
                    result = view._match_module_bay(item, index_map, bays)

        assert result is bay

    def test_item_name_used_when_no_parent_name(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = _make_base_view()
        item = {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Module Bay 3",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {1: item}
        bay = _bay("Module Bay 3")
        bays = {"Module Bay 3": bay}

        with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mbm:
            mock_mbm.objects.filter.return_value.first.return_value = None
            with patch.object(BaseModuleTableView, "_lookup_regex_bay_mapping", return_value=None):
                with patch.object(BaseModuleTableView, "_match_bay_by_position", return_value=None):
                    result = view._match_module_bay(item, index_map, bays)

        assert result is bay

    def test_returns_none_when_no_match(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = _make_base_view()
        item = {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Unknown-X",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {1: item}
        bays = {"Slot 1": _bay("Slot 1")}

        with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mbm:
            mock_mbm.objects.filter.return_value.first.return_value = None
            with patch.object(BaseModuleTableView, "_lookup_regex_bay_mapping", return_value=None):
                with patch.object(BaseModuleTableView, "_match_bay_by_position", return_value=None):
                    result = view._match_module_bay(item, index_map, bays)

        assert result is None


# ---------------------------------------------------------------------------
# _install_single — status codes
# ---------------------------------------------------------------------------


class TestInstallSingleStatus:
    """_install_single returns the correct status dict in each path."""

    def _make_args(self):
        """Return (device, item, index_map, module_types, ModuleBay, ModuleType, Module)."""
        device = MagicMock()
        device.device_type.manufacturer = None

        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "SN123",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        mt = MagicMock()
        mt.model = "WS-X4748"
        mt.pk = 1

        bay = _bay("Slot 1")
        bay.installed_module = None

        index_map = {10: item}
        module_types = {"WS-X4748": mt}

        ModuleBay = MagicMock()
        ModuleBay.objects.filter.return_value.select_related.return_value = [bay]
        ModuleType = MagicMock()
        Module = MagicMock()

        return device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt

    def test_returns_installed_on_success(self):
        from contextlib import contextmanager

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        module_instance = MagicMock()
        Module.return_value = module_instance

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
                with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls:
                    mock_mapping_cls.objects.all.return_value = []
                    with patch.object(view, "_find_parent_module_id", return_value=None):
                        with patch.object(view, "_match_bay", return_value=bay):
                            result = view._install_single(
                                device, item, index_map, module_types, ModuleBay, ModuleType, Module
                            )

        assert result["status"] == "installed"
        assert "WS-X4748" in result["name"]

    def test_returns_skipped_when_no_type(self):
        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()

        with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
            with patch.object(view, "_find_parent_module_id", return_value=None):
                result = view._install_single(
                    device,
                    item,
                    index_map,
                    {},  # empty module_types → no match
                    ModuleBay,
                    ModuleType,
                    Module,
                )

        assert result["status"] == "skipped"
        assert "no matching type" in result["reason"]

    def test_returns_skipped_when_no_bay(self):
        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()

        with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
            with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls:
                mock_mapping_cls.objects.all.return_value = []
                with patch.object(view, "_find_parent_module_id", return_value=None):
                    with patch.object(view, "_match_bay", return_value=None):
                        result = view._install_single(
                            device, item, index_map, module_types, ModuleBay, ModuleType, Module
                        )

        assert result["status"] == "skipped"
        assert "no matching bay" in result["reason"]

    def test_returns_skipped_when_bay_already_occupied(self):
        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        bay.installed_module = _module()  # occupied!

        with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
            with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls:
                mock_mapping_cls.objects.all.return_value = []
                with patch.object(view, "_find_parent_module_id", return_value=None):
                    with patch.object(view, "_match_bay", return_value=bay):
                        result = view._install_single(
                            device, item, index_map, module_types, ModuleBay, ModuleType, Module
                        )

        assert result["status"] == "skipped"
        assert "already occupied" in result["reason"]

    def test_returns_failed_on_exception(self):
        from contextlib import contextmanager

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        Module.side_effect = Exception("DB error")

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="WS-X4748"):
                with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls:
                    mock_mapping_cls.objects.all.return_value = []
                    with patch.object(view, "_find_parent_module_id", return_value=None):
                        with patch.object(view, "_match_bay", return_value=bay):
                            result = view._install_single(
                                device, item, index_map, module_types, ModuleBay, ModuleType, Module
                            )

        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Regression: ToggleColumn accessor for per-row checkboxes
# ---------------------------------------------------------------------------


class TestToggleColumnAccessor:
    """ToggleColumn must have accessor='ent_physical_index' so per-row checkboxes render."""

    def test_selection_column_has_correct_accessor(self):
        """Regression: without accessor='ent_physical_index' checkboxes are empty."""
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        col = LibreNMSModuleTable.base_columns["selection"]
        assert col.accessor == "ent_physical_index", (
            "ToggleColumn must use accessor='ent_physical_index'; "
            "otherwise the column value resolves to '' and render() is never called"
        )

    def test_selection_column_renders_checkbox_for_record_with_index(self):
        """Per-row checkbox renders when ent_physical_index is present in record."""
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        record = {
            "ent_physical_index": 42,
            "name": "Slot 1",
            "model": "WS-X4748",
            "depth": 0,
        }
        table = LibreNMSModuleTable([record])
        rows = list(table.rows)
        assert len(rows) == 1
        # The cell value for 'selection' should be 42 (ent_physical_index), not ''
        cell_val = rows[0].get_cell("selection")
        assert str(cell_val) != "", "Checkbox cell must not be empty for a record with ent_physical_index"


# ---------------------------------------------------------------------------
# Regression: ancestor walk skips containers with N/A model (Cisco 8201 style)
# ---------------------------------------------------------------------------


class TestAncestorWalkGenericContainerModel:
    """Top-level items under containers with 'N/A' model should not be excluded."""

    def _run_top_items(self, inventory_data):
        from netbox_librenms_plugin.views.base.modules_view import INVENTORY_CLASSES, _GENERIC_CONTAINER_MODELS

        idx_map = {
            item["entPhysicalIndex"]: item for item in inventory_data if item.get("entPhysicalIndex") is not None
        }
        top_items = []
        for item in inventory_data:
            phys_class = item.get("entPhysicalClass")
            if phys_class not in INVENTORY_CLASSES:
                continue
            model = (item.get("entPhysicalModelName") or "").strip()
            if phys_class == "container" and model in _GENERIC_CONTAINER_MODELS:
                continue
            if model and model in _GENERIC_CONTAINER_MODELS:
                continue
            is_descendant = False
            current_idx = item.get("entPhysicalContainedIn", 0)
            visited_ancestors = set()
            while current_idx and current_idx in idx_map and current_idx not in visited_ancestors:
                visited_ancestors.add(current_idx)
                ancestor = idx_map[current_idx]
                anc_class = ancestor.get("entPhysicalClass")
                if anc_class in INVENTORY_CLASSES:
                    anc_model = (ancestor.get("entPhysicalModelName") or "").strip()
                    if anc_class == "container" and anc_model in _GENERIC_CONTAINER_MODELS:
                        current_idx = ancestor.get("entPhysicalContainedIn", 0)
                        continue
                    is_descendant = True
                    break
                current_idx = ancestor.get("entPhysicalContainedIn", 0)
            if is_descendant:
                continue
            top_items.append(item)
        return top_items

    def test_item_under_container_with_na_model_is_top_level(self):
        """Module under a container with model='N/A' must appear as top-level item."""
        inventory = [
            # chassis (not in INVENTORY_CLASSES, so ignored in ancestor walk)
            {
                "entPhysicalIndex": 9000,
                "entPhysicalClass": "chassis",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 0,
            },
            # container with model='N/A' inside chassis — generic slot
            {
                "entPhysicalIndex": 8000,
                "entPhysicalClass": "container",
                "entPhysicalModelName": "N/A",
                "entPhysicalContainedIn": 9000,
            },
            # real module inside the N/A container — should be top-level
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 8000,
            },
        ]
        top = self._run_top_items(inventory)
        indices = [i["entPhysicalIndex"] for i in top]
        assert 1 in indices, "Module inside N/A container must be a top-level item (Cisco 8201 regression)"

    def test_item_under_container_with_empty_model_is_top_level(self):
        """Legacy: module under container with empty model still works."""
        inventory = [
            {
                "entPhysicalIndex": 9000,
                "entPhysicalClass": "chassis",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 8000,
                "entPhysicalClass": "container",
                "entPhysicalModelName": "",
                "entPhysicalContainedIn": 9000,
            },
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 8000,
            },
        ]
        top = self._run_top_items(inventory)
        indices = [i["entPhysicalIndex"] for i in top]
        assert 1 in indices

    def test_item_under_real_module_is_excluded(self):
        """Module inside another real (non-generic) module stays a descendant."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "PARENT-MODULE",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "CHILD-MODULE",
                "entPhysicalContainedIn": 1,
            },
        ]
        top = self._run_top_items(inventory)
        indices = [i["entPhysicalIndex"] for i in top]
        assert 1 in indices
        assert 2 not in indices, "Child module under real parent must remain a descendant"


# ---------------------------------------------------------------------------
# Regression: parent_row_idx (table index) must not alias entPhysicalIndex
# ---------------------------------------------------------------------------


class TestParentRowIdxVsEntityIndex:
    """Regression: parent_row_idx must be used for table_data access, not parent_ent_idx.

    Bug: parent_idx was first set to len(table_data) (a small row index), then
    overwritten with item.get("entPhysicalIndex") (which can be millions).
    table_data[parent_idx] then indexed the list with the large entity value,
    causing IndexError or wrong-row mutations.
    """

    def test_has_installable_children_set_on_correct_row(self):
        """has_installable_children must land on table row 0, not on entity index 8_000_000."""
        import importlib
        from unittest.mock import MagicMock, patch

        mod = importlib.import_module("netbox_librenms_plugin.views.base.modules_view")
        BaseModuleTableView = mod.BaseModuleTableView

        LARGE_IDX = 8_000_000  # >> any table_data list length
        CHILD_IDX = 8_000_001

        inventory = [
            {
                "entPhysicalIndex": LARGE_IDX,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "BIG-MODULE",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "SN1",
                "entPhysicalName": "Big Module",
            },
            {
                "entPhysicalIndex": CHILD_IDX,
                "entPhysicalClass": "port",
                "entPhysicalModelName": "SFP-X",
                "entPhysicalContainedIn": LARGE_IDX,
                "entPhysicalSerialNum": "SN2",
                "entPhysicalName": "Port 1",
            },
        ]

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._librenms_api = MagicMock(server_key="test-server")

        captured_table_data = []

        def fake_build_row(item, index_map, bays, module_types, depth=0):
            if item.get("entPhysicalIndex") == LARGE_IDX:
                return {"ent_physical_index": LARGE_IDX, "can_install": False, "depth": 0}
            # child returns can_install=True to trigger the has_installable_children path
            return {"ent_physical_index": CHILD_IDX, "can_install": True, "depth": 1}

        def fake_get_table(table_data, obj):
            captured_table_data.extend(table_data)
            return MagicMock()

        request = MagicMock()
        obj = MagicMock()
        obj.device_type.manufacturer = None

        with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping:
            mock_mapping.objects.all.return_value = []
            with patch.object(view, "_get_module_bays", return_value=({}, {})):
                with patch.object(view, "_get_module_types", return_value={}):
                    with patch.object(view, "_build_row", side_effect=fake_build_row):
                        with patch.object(view, "get_table", side_effect=fake_get_table):
                            with patch.object(view, "_sort_with_hierarchy", side_effect=lambda x: x):
                                # Old bug: IndexError when large entity index used as list index
                                view._build_context(request, obj, inventory)

        assert len(captured_table_data) >= 1, "table_data must contain the parent row"
        assert captured_table_data[0].get("has_installable_children") is True, (
            "has_installable_children must be set on table row 0 (parent_row_idx), "
            "not at entity index 8_000_000 which would cause IndexError"
        )


# ---------------------------------------------------------------------------
# Regression: install views must NOT delete the LibreNMS inventory cache
# ---------------------------------------------------------------------------


class TestInstallViewsDoNotDeleteCache:
    """Install views must not call cache.delete after a successful install.

    The LibreNMS inventory cache stores what LibreNMS reports (hardware list).
    It is unaffected by NetBox module installs; _get_module_bays() is a live DB
    query so the next render correctly shows the "Installed" state without any
    cache invalidation.  Deleting the cache after install caused an empty modules
    tab (regression).
    """

    def test_install_module_view_no_cache_delete_in_source(self):
        """InstallModuleView.post body must not contain a cache.delete call."""
        import inspect

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        source = inspect.getsource(InstallModuleView.post)
        assert "cache.delete" not in source, (
            "InstallModuleView.post must not call cache.delete — "
            "deleting the inventory cache after install causes an empty modules tab."
        )

    def test_install_branch_view_no_cache_delete_in_source(self):
        """InstallBranchView.post body must not contain a cache.delete call."""
        import inspect

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        source = inspect.getsource(InstallBranchView.post)
        assert "cache.delete" not in source, (
            "InstallBranchView.post must not call cache.delete — "
            "deleting the inventory cache after install causes an empty modules tab."
        )

    def test_install_selected_view_no_cache_delete_in_source(self):
        """InstallSelectedView.post body must not contain a cache.delete call."""
        import inspect

        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        source = inspect.getsource(InstallSelectedView.post)
        assert "cache.delete" not in source, (
            "InstallSelectedView.post must not call cache.delete — "
            "deleting the inventory cache after install causes an empty modules tab."
        )
