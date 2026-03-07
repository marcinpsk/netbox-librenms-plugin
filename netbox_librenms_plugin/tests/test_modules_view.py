"""Tests for BaseModuleTableView sync logic (modules_view.py).

Focuses on the bay-scope tracking in _build_context and the serial
comparison logic in _build_row.
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_view():
    """Instantiate BaseModuleTableView bypassing __init__."""
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    view = object.__new__(BaseModuleTableView)
    view._device_manufacturer = None
    view._librenms_api = MagicMock(server_key="test-server")
    view.get_cache_key = MagicMock(return_value="test_cache_key")
    return view


def _captured_table_view(view):
    """Replace get_table with a version that captures the raw table_data list."""
    rows_store = {}

    def fake_get_table(table_data, obj):
        rows_store["rows"] = table_data
        m = MagicMock()
        m.configure = MagicMock()
        return m

    view.get_table = fake_get_table
    return rows_store


def _run_build_context(view, inventory_data, device_bays, module_scoped_bays, module_types):
    """Call _build_context with all DB-accessing calls mocked out."""
    rows_store = _captured_table_view(view)
    view._get_module_bays = MagicMock(return_value=(device_bays, module_scoped_bays))
    view._get_module_types = MagicMock(return_value=module_types)

    with (
        patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
        patch("netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda v, *a, **kw: v),
        patch("netbox_librenms_plugin.utils.supports_module_path", return_value=False),
        patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False),
        patch("netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False),
        patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=True),
        patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping,
    ):
        mock_cache.ttl = MagicMock(return_value=None)
        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda s: iter([])
        mock_qs.first.return_value = None
        mock_mapping.objects.filter.return_value = mock_qs

        # Inline import: patch ModuleBayMapping inside models module
        view._build_context(MagicMock(), MagicMock(), inventory_data)

    return rows_store.get("rows", [])


# ---------------------------------------------------------------------------
# Inventory data factories
# ---------------------------------------------------------------------------


def _linecard_inventory():
    """
    Minimal inventory modelling the prod-lab03-sw4 scenario:

    Linecard(slot 3)  [WS-X4908, module, top-level]
      X2 Port 2       [container, no model]
        Converter 3/2 [CVR-X2-SFP, other] — INSTALLED in NetBox
          SFP slot     [container, no model]
            GE3/11    [GLC-TE, port, serial=MTC213403BB]
      X2 Port 4       [container, no model]
        Converter 3/4 [CVR-X2-SFP, other] — NOT installed in NetBox
          SFP slot 4  [container, no model]
            GE3/15    [GLC-T, port, serial=MTC19330SQC]
    """
    return [
        {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Slot 3",
            "entPhysicalModelName": "WS-X4908",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
            "entPhysicalSerialNum": "S_LINECARD",
            "entPhysicalParentRelPos": 3,
        },
        # --- X2 Port 2 branch (installed CVR) ---
        {
            "entPhysicalIndex": 10,
            "entPhysicalName": "X2 Port 2",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 2,
        },
        {
            "entPhysicalIndex": 11,
            "entPhysicalName": "Converter 3/2",
            "entPhysicalModelName": "CVR-X2-SFP",
            "entPhysicalClass": "other",
            "entPhysicalContainedIn": 10,
            "entPhysicalSerialNum": "FDO_CVR2",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 12,
            "entPhysicalName": "SFP slot",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 11,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 13,
            "entPhysicalName": "GigabitEthernet3/11",
            "entPhysicalModelName": "GLC-TE",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 12,
            "entPhysicalSerialNum": "MTC213403BB",
            "entPhysicalParentRelPos": 1,
        },
        # --- X2 Port 4 branch (NOT installed CVR) ---
        {
            "entPhysicalIndex": 20,
            "entPhysicalName": "X2 Port 4",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 4,
        },
        {
            "entPhysicalIndex": 21,
            "entPhysicalName": "Converter 3/4",
            "entPhysicalModelName": "CVR-X2-SFP",
            "entPhysicalClass": "other",
            "entPhysicalContainedIn": 20,
            "entPhysicalSerialNum": "FDO_CVR4",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 22,
            "entPhysicalName": "SFP slot 4",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 21,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 23,
            "entPhysicalName": "GigabitEthernet3/15",
            "entPhysicalModelName": "GLC-T",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 22,
            "entPhysicalSerialNum": "MTC19330SQC",
            "entPhysicalParentRelPos": 1,
        },
    ]


def _bay_setup():
    """Build mock device_bays and module_scoped_bays matching _linecard_inventory."""
    # --- module instances (NetBox Module objects) ---
    linecard_module = MagicMock()
    linecard_module.pk = 100
    linecard_module.serial = "S_LINECARD"

    cvr2_module = MagicMock()
    cvr2_module.pk = 200
    cvr2_module.serial = "FDO_CVR2"

    glc_te_installed = MagicMock()
    glc_te_installed.serial = "MTC213403BB"
    glc_te_installed.get_absolute_url.return_value = "/modules/99/"

    # --- device-level bays ---
    slot3_bay = MagicMock()
    slot3_bay.name = "Slot 3"
    slot3_bay.installed_module = linecard_module
    device_bays = {"Slot 3": slot3_bay}

    # --- module-scoped bays created by the linecard ---
    x2p2_bay = MagicMock()
    x2p2_bay.name = "X2 Port 2"
    x2p2_bay.installed_module = cvr2_module  # INSTALLED

    x2p4_bay = MagicMock()
    x2p4_bay.name = "X2 Port 4"
    x2p4_bay.installed_module = None  # NOT installed

    # --- module-scoped bays created by the installed CVR at X2 Port 2 ---
    sfp1_bay = MagicMock()
    sfp1_bay.name = "SFP 1"
    sfp1_bay.installed_module = glc_te_installed

    sfp2_bay = MagicMock()
    sfp2_bay.name = "SFP 2"
    sfp2_bay.installed_module = None

    module_scoped_bays = {
        100: {"X2 Port 2": x2p2_bay, "X2 Port 4": x2p4_bay},
        200: {"SFP 1": sfp1_bay, "SFP 2": sfp2_bay},
    }

    return device_bays, module_scoped_bays


def _module_types():
    """Minimal module-type dict for the test scenario."""
    mt_linecard = MagicMock()
    mt_linecard.model = "WS-X4908"
    mt_cvr = MagicMock()
    mt_cvr.model = "CVR-X2-SFP"
    mt_glc_te = MagicMock()
    mt_glc_te.model = "GLC-TE"
    mt_glc_t = MagicMock()
    mt_glc_t.model = "GLC-T"
    return {
        "WS-X4908": mt_linecard,
        "CVR-X2-SFP": mt_cvr,
        "GLC-TE": mt_glc_te,
        "GLC-T": mt_glc_t,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBayDepthScopeWithUninstalledParent:
    """
    Regression tests for the stale bays_by_depth bug.

    Scenario: two converters at depth-1 share the same parent linecard.
    Converter 3/2 IS installed (it has SFP child bays).
    Converter 3/4 is NOT installed (no SFP child bays exist yet in NetBox).

    Bug: bays_by_depth[2] is set when processing Converter 3/2, and NOT
    cleared when processing Converter 3/4.  GigabitEthernet3/15 (depth-2
    child of Converter 3/4) then inherits the stale SFP scope and gets
    "Serial Mismatch" instead of "No Bay".

    Fix: when a matched bay has no installed module, set bays_by_depth[depth+1]
    to {} to prevent leakage to subsequent siblings at the same depth.
    """

    def _build_rows(self):
        view = _make_view()
        device_bays, module_scoped_bays = _bay_setup()
        module_types = _module_types()
        return _run_build_context(view, _linecard_inventory(), device_bays, module_scoped_bays, module_types)

    def _row(self, rows, name):
        for r in rows:
            if r.get("name") == name:
                return r
        return None

    def test_glc_t_under_installed_converter_is_installed(self):
        """GLC-TE under the installed Converter 3/2 must show 'Installed'."""
        rows = self._build_rows()
        row = self._row(rows, "GigabitEthernet3/11")
        assert row is not None, "GigabitEthernet3/11 row not found"
        assert row["status"] == "Installed", (
            f"Expected 'Installed' but got {row['status']!r} — GLC-TE under an installed CVR should be Installed"
        )

    def test_glc_t_under_uninstalled_converter_is_no_bay_not_serial_mismatch(self):
        """GLC-T under the uninstalled Converter 3/4 must show 'No Bay'.

        Before the fix, bays_by_depth[2] retains the SFP scope from
        Converter 3/2 and GigabitEthernet3/15 incorrectly gets 'Serial Mismatch'.
        """
        rows = self._build_rows()
        row = self._row(rows, "GigabitEthernet3/15")
        assert row is not None, "GigabitEthernet3/15 row not found"
        assert row["status"] != "Serial Mismatch", (
            "GigabitEthernet3/15 shows 'Serial Mismatch' — stale bays_by_depth scope "
            "leaking from Converter 3/2 into Converter 3/4's child items (regression)"
        )
        assert row["status"] == "No Bay", (
            f"Expected 'No Bay' but got {row['status']!r}; "
            "the parent converter is not installed so child SFPs cannot be matched"
        )

    def test_uninstalled_converter_itself_shows_matched(self):
        """Converter 3/4 is matched to X2 Port 4 but not yet installed → 'Matched'."""
        rows = self._build_rows()
        row = self._row(rows, "Converter 3/4")
        assert row is not None, "Converter 3/4 row not found"
        assert row["status"] == "Matched", f"Expected 'Matched' but got {row['status']!r} for uninstalled converter"

    def test_installed_converter_itself_shows_installed(self):
        """Converter 3/2 is installed in X2 Port 2 with matching serial → 'Installed'."""
        rows = self._build_rows()
        row = self._row(rows, "Converter 3/2")
        assert row is not None, "Converter 3/2 row not found"
        assert row["status"] == "Installed", f"Expected 'Installed' but got {row['status']!r} for installed converter"

    def test_no_stale_scope_across_multiple_siblings(self):
        """bays_by_depth is reset for EACH sibling, so the second uninstalled
        converter does not leak into a third converter's children."""
        # Add a second installed converter at X2 Port 6 and verify its SFP
        # also shows correct status, unaffected by the reset for X2 Port 4.
        inventory = _linecard_inventory() + [
            {
                "entPhysicalIndex": 30,
                "entPhysicalName": "X2 Port 6",
                "entPhysicalModelName": "",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 1,
                "entPhysicalSerialNum": "",
                "entPhysicalParentRelPos": 6,
            },
            {
                "entPhysicalIndex": 31,
                "entPhysicalName": "Converter 3/6",
                "entPhysicalModelName": "CVR-X2-SFP",
                "entPhysicalClass": "other",
                "entPhysicalContainedIn": 30,
                "entPhysicalSerialNum": "FDO_CVR6",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 32,
                "entPhysicalName": "SFP slot 6",
                "entPhysicalModelName": "",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 31,
                "entPhysicalSerialNum": "",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 33,
                "entPhysicalName": "GigabitEthernet3/22",
                "entPhysicalModelName": "GLC-TE",
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 32,
                "entPhysicalSerialNum": "SFP6_SERIAL",
                "entPhysicalParentRelPos": 1,
            },
        ]

        view = _make_view()
        device_bays, module_scoped_bays = _bay_setup()
        module_types = _module_types()

        # Add a third installed CVR at X2 Port 6 with its own SFP 1 bay
        cvr6_module = MagicMock()
        cvr6_module.pk = 300
        cvr6_module.serial = "FDO_CVR6"

        sfp1_bay_6 = MagicMock()
        sfp1_bay_6.name = "SFP 1"
        sfp6_installed = MagicMock()
        sfp6_installed.serial = "SFP6_SERIAL"
        sfp6_installed.get_absolute_url.return_value = "/modules/199/"
        sfp1_bay_6.installed_module = sfp6_installed

        x2p6_bay = MagicMock()
        x2p6_bay.name = "X2 Port 6"
        x2p6_bay.installed_module = cvr6_module

        module_scoped_bays[100]["X2 Port 6"] = x2p6_bay
        module_scoped_bays[300] = {"SFP 1": sfp1_bay_6}

        rows = _run_build_context(view, inventory, device_bays, module_scoped_bays, module_types)

        def _row(name):
            return next((r for r in rows if r.get("name") == name), None)

        # The GE3/22 under the 3rd converter (installed) should be Installed
        row6 = _row("GigabitEthernet3/22")
        assert row6 is not None, "GigabitEthernet3/22 not found"
        assert row6["status"] == "Installed", (
            f"Expected 'Installed' but got {row6['status']!r} — "
            "GLC-TE under installed Converter 3/6 should be Installed"
        )
        # And GE3/15 under the uninstalled converter is still No Bay
        row15 = _row("GigabitEthernet3/15")
        assert row15["status"] == "No Bay", f"GigabitEthernet3/15 status {row15['status']!r} — should still be No Bay"


class TestCollectDescendants:
    """Tests for _collect_descendants depth tracking."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return object.__new__(BaseModuleTableView)

    def test_empty_container_children_at_same_depth(self):
        """Children of a no-model container are returned at the same depth as the container."""
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "REAL-MODULE", "entPhysicalContainedIn": 1},
        ]
        children_by_parent = {}
        for item in inventory:
            p = item.get("entPhysicalContainedIn")
            if p is not None:
                children_by_parent.setdefault(p, []).append(item)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, depth=1, results=results)
        assert len(results) == 1
        depth, item = results[0]
        assert depth == 1, "Child of modelless container must be at the same depth"
        assert item["entPhysicalModelName"] == "REAL-MODULE"

    def test_model_children_at_incremented_depth(self):
        """Children of a model-bearing item are at depth+1."""
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD", "entPhysicalContainedIn": 1},
        ]
        children_by_parent = {}
        for item in inventory:
            p = item.get("entPhysicalContainedIn")
            if p is not None:
                children_by_parent.setdefault(p, []).append(item)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, depth=1, results=results)
        depths = [d for d, _ in results]
        assert depths == [1, 2], f"Expected [1, 2] but got {depths}"


class TestDetermineStatus:
    """Tests for _determine_status logic."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return object.__new__(BaseModuleTableView)

    def test_matched_bay_and_type(self):
        view = self._view()
        assert view._determine_status(MagicMock(), MagicMock(), "S1") == "Matched"

    def test_no_bay(self):
        view = self._view()
        assert view._determine_status(None, MagicMock(), "S1") == "No Bay"

    def test_no_type(self):
        view = self._view()
        assert view._determine_status(MagicMock(), None, "S1") == "No Type"

    def test_unmatched_fallback(self):
        view = self._view()
        # matched_bay but no matched_type handled by No Type branch first
        assert view._determine_status(None, None, "S1") == "No Bay"


class TestBuildRowSerialMismatch:
    """Tests for serial mismatch detection and can_update_serial flag in _build_row."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        return view

    def _make_bay(self, installed_serial=None):
        """Create a mock bay with an optionally installed module."""
        bay = MagicMock()
        bay.pk = 10
        bay.name = "Slot 1"
        bay.get_absolute_url.return_value = "/dcim/module-bays/10/"
        if installed_serial is not None:
            module = MagicMock()
            module.pk = 42
            module.serial = installed_serial
            module.get_absolute_url.return_value = "/dcim/modules/42/"
            bay.installed_module = module
        else:
            bay.installed_module = None
        return bay

    def _make_item(self, model_name="XCM-7s-b", serial="NS225161205"):
        return {
            "entPhysicalModelName": model_name,
            "entPhysicalSerialNum": serial,
            "entPhysicalName": "Slot 1",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalIndex": 100,
        }

    def test_serial_match_sets_installed_status(self):
        """When ENTITY-MIB serial matches NetBox serial, status is Installed."""
        view = self._view()
        bay = self._make_bay(installed_serial="NS225161205")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        with (
            patch.object(view, "_match_module_bay", return_value=bay),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False),
            patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False),
            patch("netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False),
            patch("netbox_librenms_plugin.utils.supports_module_path", return_value=True),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Installed"
        assert row["row_class"] == "table-success"
        assert not row.get("can_update_serial")

    def test_serial_mismatch_sets_can_update_serial(self):
        """When serials differ, can_update_serial=True and installed_module_id set."""
        view = self._view()
        bay = self._make_bay(installed_serial="TESTSRL")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        with (
            patch.object(view, "_match_module_bay", return_value=bay),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False),
            patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False),
            patch("netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False),
            patch("netbox_librenms_plugin.utils.supports_module_path", return_value=True),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Serial Mismatch"
        assert row["row_class"] == "table-danger"
        assert row.get("can_update_serial") is True
        assert row.get("installed_module_id") == 42

    def test_empty_netbox_serial_does_not_set_mismatch(self):
        """When NetBox serial is empty, status is Installed regardless of LibreNMS serial."""
        view = self._view()
        bay = self._make_bay(installed_serial="")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        with (
            patch.object(view, "_match_module_bay", return_value=bay),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.module_type_uses_module_path", return_value=False),
            patch("netbox_librenms_plugin.utils.module_type_is_end_module", return_value=False),
            patch("netbox_librenms_plugin.utils.module_type_uses_module_token", return_value=False),
            patch("netbox_librenms_plugin.utils.supports_module_path", return_value=True),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Installed"
        assert not row.get("can_update_serial")
