"""Real ORM, cache, and view tests for module synchronization."""

import json

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    install_module,
    make_device,
    make_interface,
    make_module_bay,
    make_module_type,
    make_superuser,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    make_request,
    message_texts,
    post as view_post,
    trusted_module_inventory_payload,
)

pytestmark = pytest.mark.django_db


def _view(view_class, request, live_librenms):
    """Bind a real view to a real request and real LibreNMS client."""
    view = view_class()
    view._librenms_api = live_librenms.api
    view.setup(request)
    return view


def _post_request(data):
    return make_request("post", data, user=make_superuser(), path="/modules/")


def _inventory_item(index, model, name, *, parent=0, serial="", phys_class="module", **extra):
    row = {
        "entPhysicalIndex": index,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": phys_class,
        "entPhysicalContainedIn": parent,
        "entPhysicalSerialNum": serial,
    }
    row.update(extra)
    return row


def _run_install_single(device, item, index_map, module_types):
    """Run the shared installer against unrestricted real model querysets."""
    from dcim.models import Interface, ModuleBay

    from netbox_librenms_plugin.views.sync.modules import InstallBranchView, _module_component_specs

    return InstallBranchView._install_single(
        device,
        item,
        index_map,
        module_types,
        module_bays=ModuleBay.objects.all(),
        allowed_module_type_ids={module_type.pk for module_type in module_types.values()},
        changeable_components={model: model.objects.all() for _, _, model in _module_component_specs()},
        changeable_interfaces=Interface.objects.all(),
        deletable_interfaces=Interface.objects.all(),
        exact_mappings=[],
        regex_mappings=[],
        manufacturer_id=device.device_type.manufacturer_id,
        norm_rules_bay={},
    )


class TestInventoryCacheContract:
    """Module actions accept only current, per-device inventory snapshots."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"inventory": []}, []),
            ({"inventory": [{"entPhysicalIndex": 1}]}, [{"entPhysicalIndex": 1}]),
            (None, None),
            ([], None),
            ({"inventory": "bad"}, None),
            ({"inventory": ["bad"]}, None),
        ],
    )
    def test_inventory_container_validation(self, payload, expected):
        from netbox_librenms_plugin.views.sync.modules import _extract_inventory_list

        assert _extract_inventory_list(payload) == expected

    def test_current_fingerprint_returns_the_real_cached_rows(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import _get_cached_inventory_for_device

        device = make_device("module-current-cache", librenms_cf={"default": 41})
        view = _view(_cache_view_class(), _post_request({}), live_librenms)
        rows = [_inventory_item(1, "CARD", "Slot 1")]
        key = seed_inventory(view, device, rows, librenms_id=41)

        assert _get_cached_inventory_for_device(device, "default", view.get_cache_key) == rows
        assert cache.get(key)["librenms_id"] == 41

    def test_stale_fingerprint_is_rejected(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import _get_cached_inventory_for_device

        device = make_device("module-stale-cache", librenms_cf={"default": 42})
        view = _view(_cache_view_class(), _post_request({}), live_librenms)
        seed_inventory(view, device, [_inventory_item(1, "CARD", "Slot 1")], librenms_id=99)

        assert _get_cached_inventory_for_device(device, "default", view.get_cache_key) is None


def _cache_view_class():
    from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

    return InstallSelectedView


class TestTargetDeviceResolution:
    """Row-level target selection stays within the page device's real chassis."""

    def test_valid_chassis_member_is_selected(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device_with_validation

        first = make_device("target-vc-first")
        second = make_device("target-vc-second")
        make_virtual_chassis("target-vc", first, second)

        target, invalid = _resolve_target_device_with_validation(first, str(second.pk), Device.objects.all())

        assert target == second
        assert invalid is False

    @pytest.mark.parametrize("selection", ["bad", "", None, 9_999_999])
    def test_invalid_or_missing_selection_falls_back_to_page_device(self, selection):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device_with_validation

        device = make_device(f"target-fallback-{selection}")

        target, invalid = _resolve_target_device_with_validation(device, selection, Device.objects.all())

        assert target == device
        assert invalid is bool(selection)

    def test_member_of_another_chassis_is_rejected(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device_with_validation

        page = make_device("target-page")
        sibling = make_device("target-sibling")
        outsider = make_device("target-outsider")
        make_virtual_chassis("target-page-vc", page, sibling)
        make_virtual_chassis("target-other-vc", outsider)

        target, invalid = _resolve_target_device_with_validation(page, outsider.pk, Device.objects.all())

        assert target == page
        assert invalid is True


class TestInventoryIdentityHelpers:
    @pytest.mark.parametrize(
        ("row", "port_id", "names"),
        [
            ({"_librenms_port_id": "17", "_librenms_ifname": "Ethernet1"}, 17, ["Ethernet1"]),
            (
                {
                    "port_id": 18,
                    "_librenms_ifname": "Ethernet2",
                    "_librenms_ifdescr": "uplink",
                    "entPhysicalName": "Ethernet2",
                },
                18,
                ["Ethernet2", "uplink"],
            ),
            ({"port_id": True, "entPhysicalName": "  "}, None, []),
        ],
    )
    def test_port_identity_is_normalized_and_deduplicated(self, row, port_id, names):
        from netbox_librenms_plugin.views.sync.modules import _get_item_port_identity

        assert _get_item_port_identity(row) == (port_id, names)

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Ethernet2/1/17", [2, 1, 17]),
            ("xe-0/0/7", [0, 0, 7]),
            ("management", []),
            (None, []),
        ],
    )
    def test_interface_coordinates_follow_real_labels(self, label, expected):
        from netbox_librenms_plugin.views.sync.modules import _extract_interface_coordinates

        assert _extract_interface_coordinates(label) == expected

    def test_unique_coordinate_match_selects_the_real_module_interface(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.sync.modules import _select_module_interface_by_coordinates

        device = make_device("coordinate-device")
        bay = make_module_bay(device, "Coordinate Bay")
        module_type = make_module_type("COORDINATE-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")
        expected = Interface.objects.create(device=device, module=module, name="Ethernet2/1/17", type="other")
        Interface.objects.create(device=device, module=module, name="Ethernet2/1/18", type="other")

        selected = _select_module_interface_by_coordinates(
            device,
            list(module.interfaces.all()),
            {"entPhysicalName": "port 1/17"},
        )

        assert selected == expected

    def test_tied_coordinate_match_fails_closed(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.sync.modules import _select_module_interface_by_coordinates

        device = make_device("coordinate-tie")
        bay = make_module_bay(device, "Coordinate Tie Bay")
        module_type = make_module_type("COORDINATE-TIE-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")
        # Both names score identically against 1/17: same final coordinate, same module coordinate.
        Interface.objects.create(device=device, module=module, name="Ethernet1/17", type="other")
        Interface.objects.create(device=device, module=module, name="Ethernet9/1/17", type="other")

        assert (
            _select_module_interface_by_coordinates(
                device,
                list(module.interfaces.all()),
                {"entPhysicalName": "port 1/17"},
            )
            is None
        )


class TestInterfaceBinding:
    """Bind inventory port identities to real NetBox interfaces without reassignment."""

    def test_name_match_binds_port_id_and_module(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = make_device("bind-module-interface")
        bay = make_module_bay(device, "Bind Bay")
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=make_module_type("BIND-CARD"),
            status="active",
        )
        interface = make_interface(device, "Ethernet20")

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 220, "_librenms_ifname": interface.name},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        interface.refresh_from_db()
        assert result == {"status": "bound", "interface": interface.name, "port_id": 220, "changed": True}
        assert interface.module == module
        assert get_librenms_device_id(interface, "default", auto_save=False) == 220

    def test_existing_port_owner_on_another_device_is_a_conflict(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        target = make_device("bind-target")
        owner_device = make_device("bind-existing-owner")
        owner = make_interface(owner_device, "Ethernet21")
        owner.custom_field_data["librenms_id"] = {"default": 221}
        owner.save(update_fields=["custom_field_data"])

        result = _bind_interface_librenms_id(
            target,
            {"_librenms_port_id": 221, "_librenms_ifname": "Ethernet21"},
            None,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "conflict"
        assert owner_device.name in result["reason"]

    def test_ambiguous_module_interfaces_require_manual_mapping(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = make_device("bind-ambiguous")
        bay = make_module_bay(device, "Ambiguous Bay")
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=make_module_type("AMBIGUOUS-CARD"),
            status="active",
        )
        Interface.objects.create(device=device, module=module, name="alpha", type="other")
        Interface.objects.create(device=device, module=module, name="beta", type="other")

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 222},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "skipped"
        assert "multiple module interfaces" in result["reason"]


class TestBranchCollection:
    """Collect installable inventory branches in deterministic parent-first order."""

    def _view(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        return InstallBranchView()

    def test_parent_and_nested_children_are_collected_depth_first(self):
        rows = [
            _inventory_item(1, "PARENT", "Slot 1"),
            _inventory_item(2, "CHILD", "Daughter 1", parent=1),
            _inventory_item(3, "GRANDCHILD", "Optic 1", parent=2),
            _inventory_item(4, "SIBLING", "Daughter 2", parent=1),
        ]

        branch = self._view()._collect_branch(1, rows)

        assert [row["entPhysicalIndex"] for row in branch] == [1, 2, 3, 4]

    def test_cycle_is_stopped_without_duplicate_rows(self):
        rows = [
            _inventory_item(1, "ONE", "One", parent=2),
            _inventory_item(2, "TWO", "Two", parent=1),
        ]

        branch = self._view()._collect_branch(1, rows)

        assert [row["entPhysicalIndex"] for row in branch] == [1, 2]

    def test_blank_model_container_is_skipped_but_its_child_is_kept(self):
        rows = [
            _inventory_item(1, "", "Container"),
            _inventory_item(2, "CHILD", "Child", parent=1),
        ]

        branch = self._view()._collect_branch(1, rows)

        assert [row["entPhysicalIndex"] for row in branch] == [2]


class TestSharedInstaller:
    """Install rows against real types, bays, templates, and constraints."""

    def test_matching_type_and_bay_create_a_real_module(self):
        from dcim.models import Module

        device = make_device("installer-success")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("INSTALL-CARD")
        item = _inventory_item(1, module_type.model, bay.name, serial="INSTALL-SERIAL")

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        module = Module.objects.get(pk=result["module_pk"])
        assert result["status"] == "installed"
        assert module.module_bay == bay
        assert module.module_type == module_type
        assert module.serial == "INSTALL-SERIAL"

    def test_missing_type_skips_without_creating_a_module(self):
        from dcim.models import Module

        device = make_device("installer-no-type")
        make_module_bay(device, "Slot 1")
        item = _inventory_item(1, "UNKNOWN-CARD", "Slot 1")

        result = _run_install_single(device, item, {1: item}, {})

        assert result["status"] == "skipped"
        assert "no matching type" in result["reason"]
        assert not Module.objects.filter(device=device).exists()

    def test_oob_row_is_read_only_even_when_type_and_bay_match(self):
        from dcim.models import Module

        device = make_device("installer-oob")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("OOB-CARD")
        item = _inventory_item(1, module_type.model, bay.name, _source="oob")

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        assert result["status"] == "skipped"
        assert "read-only" in result["reason"]
        assert not Module.objects.filter(device=device).exists()

    def test_occupied_bay_returns_the_existing_module(self):
        device = make_device("installer-occupied")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("OCCUPIED-CARD")
        occupant = install_module(device, bay.name, module_type.model)
        item = _inventory_item(1, module_type.model, bay.name)

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        assert result["status"] == "skipped"
        assert result["reason"] == "bay already occupied"
        assert result["module_pk"] == occupant.pk

    def test_placeholder_serial_is_stored_as_blank(self):
        from dcim.models import Module

        device = make_device("installer-placeholder")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("PLACEHOLDER-CARD")
        item = _inventory_item(1, module_type.model, bay.name, serial="-")

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        assert Module.objects.get(pk=result["module_pk"]).serial == ""


class TestInstallAndUpdateViews:
    """Exercise real module mutation views from request through ORM state."""

    def test_single_install_creates_the_selected_module(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install", librenms_cf={"default": 51})
        bay = make_module_bay(device, "View Install Bay")
        module_type = make_module_type("VIEW-INSTALL-CARD")
        request = _post_request(
            {
                "module_bay_id": bay.pk,
                "module_type_id": module_type.pk,
                "serial": "VIEW-SERIAL",
                "server_key": "default",
            }
        )

        response = view_post(_view(InstallModuleView, request, live_librenms), request, pk=device.pk)

        module = Module.objects.get(device=device, module_bay=bay)
        assert response.status_code == 302
        assert module.module_type == module_type
        assert module.serial == "VIEW-SERIAL"
        assert any("Installed VIEW-INSTALL-CARD" in text for text in message_texts(request))

    def test_single_install_does_not_replace_an_occupied_bay(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install-occupied", librenms_cf={"default": 52})
        bay = make_module_bay(device, "Occupied View Bay")
        module_type = make_module_type("VIEW-OCCUPIED-CARD")
        existing = install_module(device, bay.name, module_type.model, serial="ORIGINAL")
        request = _post_request(
            {
                "module_bay_id": bay.pk,
                "module_type_id": module_type.pk,
                "serial": "REPLACEMENT",
                "server_key": "default",
            }
        )

        response = view_post(_view(InstallModuleView, request, live_librenms), request, pk=device.pk)

        assert response.status_code == 302
        assert Module.objects.get(device=device, module_bay=bay) == existing
        assert Module.objects.get(pk=existing.pk).serial == "ORIGINAL"
        assert any("already has a module" in text for text in message_texts(request))

    def test_update_serial_changes_the_real_module(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("view-update-serial", librenms_cf={"default": 53})
        bay = make_module_bay(device, "Serial Bay")
        module = install_module(device, bay.name, "SERIAL-CARD", serial="OLD")
        request = _post_request({"module_id": module.pk, "serial": "NEW", "server_key": "default"})

        response = view_post(_view(UpdateModuleSerialView, request, live_librenms), request, pk=device.pk)

        module.refresh_from_db()
        assert response.status_code == 302
        assert module.serial == "NEW"
        assert any("Updated serial" in text for text in message_texts(request))

    def test_update_interface_binds_cached_inventory_identity(self, live_librenms):
        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("view-update-interface", librenms_cf={"default": 54})
        bay = make_module_bay(device, "Interface Bay")
        module = install_module(device, bay.name, "INTERFACE-CARD")
        interface = make_interface(device, "Ethernet54")
        item = _inventory_item(
            540,
            module.module_type.model,
            bay.name,
            _librenms_port_id=5540,
            _librenms_ifname=interface.name,
        )
        request = _post_request({"module_id": module.pk, "ent_index": 540, "server_key": "default"})
        view = _view(UpdateModuleInterfaceView, request, live_librenms)
        seed_inventory(view, device, [item], librenms_id=54)

        response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.module == module
        assert get_librenms_device_id(interface, "default", auto_save=False) == 5540
        assert any("Updated interface" in text for text in message_texts(request))

    def test_install_selected_uses_real_cache_mapping_and_models(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.models import ModuleBayMapping
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("view-install-selected", librenms_cf={"default": 55})
        bay = make_module_bay(device, "Selected Bay")
        module_type = make_module_type("SELECTED-CARD")
        ModuleBayMapping.objects.create(
            librenms_name="LibreNMS Selected Slot",
            librenms_class="module",
            netbox_bay_name=bay.name,
        )
        item = _inventory_item(550, module_type.model, "LibreNMS Selected Slot", serial="SELECTED-SERIAL")
        request = _post_request({"select": ["550"], "server_key": "default"})
        view = _view(InstallSelectedView, request, live_librenms)
        seed_inventory(view, device, [item], librenms_id=55)

        response = view_post(view, request, pk=device.pk)

        module = Module.objects.get(device=device, module_bay=bay)
        assert response.status_code == 302
        assert module.module_type == module_type
        assert module.serial == "SELECTED-SERIAL"
        assert any("Installed 1 module" in text for text in message_texts(request))

    def test_branch_install_does_not_use_hidden_bay_or_module_type(self, live_librenms):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("module-catalog-scope", librenms_cf={"default": 1})
        hidden_bay = make_module_bay(device, "Hidden Scope Bay")
        allowed_bay = make_module_bay(device, "Allowed Scope Bay")
        hidden_type = make_module_type("Hidden Scope Module Type")
        allowed_type = make_module_type("Allowed Scope Module Type")
        user = make_user_with_perms(
            "module-catalog-scope",
            [("view", Device), ("add", Module), ("add", Interface), ("change", Interface), ("delete", Interface)],
        )
        user = grant(user, "view", ModuleBay, constraints={"pk": allowed_bay.pk})
        user = grant(user, "view", ModuleType, constraints={"pk": allowed_type.pk})
        request = make_request(
            "post",
            {"parent_index": "100", "server_key": "default"},
            user=user,
            path="/modules/install-branch/",
        )
        view = _view(InstallBranchView, request, live_librenms)
        seed_inventory(
            view,
            device,
            [_inventory_item(100, hidden_type.model, hidden_bay.name)],
            librenms_id=1,
        )

        view_post(view, request, pk=device.pk)

        assert not Module.objects.filter(module_bay=hidden_bay).exists()

    def test_interface_outside_change_grant_is_not_bound_to_module(self, live_librenms):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("module-interface-scope", librenms_cf={"default": 2})
        bay = make_module_bay(device, "Interface Scope Bay")
        module = install_module(device, bay.name, "INTERFACE-SCOPE-CARD")
        hidden = make_interface(device, "Te1/1/1")
        allowed = make_interface(device, "Te1/1/2")
        set_librenms_device_id(hidden, 42, "default")
        hidden.save(update_fields=["custom_field_data"])
        user = make_user_with_perms("module-interface-scope", [("view", Device), ("view", Module)])
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        request = make_request(
            "post",
            {"module_id": str(module.pk), "server_key": "default", "ent_index": "77"},
            user=user,
            path="/modules/update-interface/",
        )
        view = _view(UpdateModuleInterfaceView, request, live_librenms)
        seed_inventory(
            view,
            device,
            [
                _inventory_item(
                    77, module.module_type.model, bay.name, _librenms_port_id=42, _librenms_ifname=hidden.name
                )
            ],
            librenms_id=2,
        )

        view_post(view, request, pk=device.pk)

        hidden.refresh_from_db()
        assert hidden.module_id is None


class TestModulesRedirectResponse:
    """_modules_redirect_response: the classic (non-HTMX) redirect back to the modules tab."""

    def test_classic_request_uses_redirect(self):
        """A plain form post is sent back to the modules tab anchor."""
        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        response = _modules_redirect_response(_post_request({}), "/sync/")

        assert response.status_code == 302
        assert response.url == "/sync/?tab=modules#librenms-module-table"

    def test_explicit_server_key_is_appended(self):
        """A server-scoped action returns the user to the cache namespace it just read or mutated."""
        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        response = _modules_redirect_response(_post_request({}), "/sync/", server_key="prod server")

        # quote_plus encodes the value and the fragment stays last.
        assert response.url == "/sync/?tab=modules&server_key=prod+server#librenms-module-table"

    def test_server_key_read_from_post_when_not_passed(self):
        """A call site that resolves no key still propagates the posted server context."""
        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        response = _modules_redirect_response(_post_request({"server_key": "production"}), "/sync/")

        assert response.url == "/sync/?tab=modules&server_key=production#librenms-module-table"


class TestModulesActionResponse:
    """Module actions swap the module tab in place over HTMX and keep the classic redirect."""

    SERVER_KEY = "prod"

    def _configure_server(self, settings):
        """Configure one LibreNMS server the module actions can resolve."""
        configure_librenms_servers(
            settings,
            {self.SERVER_KEY: {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}},
        )

    def _cache_key(self, device):
        """Return the module tab's inventory cache key for this class's server."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        return DeviceModuleTableView().get_cache_key(device, "inventory", server_key=self.SERVER_KEY)

    def _seed_inventory(self, device, bay, module_type, *, serial="ACTION-1", librenms_id=9201):
        """Seed one inventory row matching the bay and module type under the module tab's cache key."""
        payload = trusted_module_inventory_payload(
            device,
            [
                {
                    "entPhysicalIndex": 8201,
                    "entPhysicalClass": "module",
                    "entPhysicalModelName": module_type.model,
                    "entPhysicalContainedIn": 0,
                    "entPhysicalName": bay.name,
                    "entPhysicalSerialNum": serial,
                }
            ],
            server_key=self.SERVER_KEY,
            librenms_id=librenms_id,
        )
        cache.set(self._cache_key(device), payload, 300)

    def test_htmx_install_swaps_the_module_tab_in_place(self, client, settings, django_capture_on_commit_callbacks):
        """An HTMX install answers with the module tab fragment instead of navigating the browser."""
        from dcim.models import Module
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-install")
        bay = make_module_bay(device, "Action Bay")
        module_type = make_module_type("ACTION-CARD")
        self._seed_inventory(device, bay, module_type)
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                url,
                {
                    "server_key": self.SERVER_KEY,
                    "module_bay_id": str(bay.pk),
                    "module_type_id": str(module_type.pk),
                    "serial": "ACTION-1",
                },
                HTTP_HX_REQUEST="true",
            )

        assert response.status_code == 200
        assert response["HX-Retarget"] == "#module-sync-content"
        assert response["HX-Reswap"] == "innerHTML"
        assert "HX-Redirect" not in response
        assert "HX-Refresh" not in response
        trigger = json.loads(response["HX-Trigger"])
        assert "closeModal" in trigger
        assert "librenmsCacheChanged" in trigger
        body = response.content.decode()
        assert 'id="librenms-module-table"' in body
        assert f'name="server_key" value="{self.SERVER_KEY}"' in body
        assert f"Installed {module_type.model} in {bay.name}" in body
        assert '<span class="badge bg-success text-white">Installed</span>' in body
        assert Module.objects.filter(device=device, module_bay=bay, module_type=module_type).exists()

    def test_htmx_action_keeps_the_page_and_sort_of_the_current_url(self, client, settings):
        """The re-rendered table honours the page's own query (page, per_page), not the action URL's empty one."""
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-paged")
        module_type = make_module_type("PAGED-CARD")
        # NetBox's EnhancedPaginator folds up to 5 orphans into the last page, so 7 rows make a real page 2.
        bays = [make_module_bay(device, f"Bay {number:02d}") for number in range(1, 8)]
        payload = trusted_module_inventory_payload(
            device,
            [
                {
                    "entPhysicalIndex": 8200 + number,
                    "entPhysicalClass": "module",
                    "entPhysicalModelName": module_type.model,
                    "entPhysicalContainedIn": 0,
                    "entPhysicalName": bay.name,
                    "entPhysicalSerialNum": f"PAGED-{number}",
                }
                for number, bay in enumerate(bays, start=1)
            ],
            server_key=self.SERVER_KEY,
            librenms_id=9204,
        )
        cache.set(self._cache_key(device), payload, 300)
        client.force_login(make_superuser())
        sync_page = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk})
        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})

        response = client.post(
            url,
            {
                "server_key": self.SERVER_KEY,
                "module_bay_id": str(bays[0].pk),
                "module_type_id": str(module_type.pk),
                "serial": "PAGED-1",
            },
            HTTP_HX_REQUEST="true",
            HTTP_HX_CURRENT_URL=(
                f"http://testserver{sync_page}?tab=modules&server_key={self.SERVER_KEY}"
                "&modules_per_page=1&modules_page=2#librenms-module-table"
            ),
        )

        assert response.status_code == 200
        body = response.content.decode()
        table = body.split('id="librenms-module-table"', 1)[1]
        assert "Bay 02" in table
        assert "Bay 01" not in table
        assert "modules_page=1" in body

    def test_classic_install_still_redirects_to_the_modules_tab(self, client, settings):
        """Without the HTMX header the same install keeps the server-scoped redirect contract."""
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-classic")
        bay = make_module_bay(device, "Classic Bay")
        module_type = make_module_type("CLASSIC-CARD")
        self._seed_inventory(device, bay, module_type, librenms_id=9202)
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})

        response = client.post(
            url,
            {
                "server_key": self.SERVER_KEY,
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "serial": "ACTION-1",
            },
        )

        assert response.status_code == 302
        assert response.url.endswith(f"?tab=modules&server_key={self.SERVER_KEY}#librenms-module-table")

    def test_htmx_serial_update_from_the_modal_is_retargeted(self, client, settings):
        """A mismatch-modal action, which swaps nothing itself, is retargeted at the module tab."""
        from dcim.models import Module
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-serial")
        bay = make_module_bay(device, "Serial Bay")
        module_type = make_module_type("SERIAL-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD-SERIAL")
        self._seed_inventory(device, bay, module_type, librenms_id=9203)
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})

        response = client.post(
            url,
            {"server_key": self.SERVER_KEY, "module_id": str(module.pk), "serial": "ACTION-1"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert response["HX-Retarget"] == "#module-sync-content"
        assert response["HX-Reswap"] == "innerHTML"
        body = response.content.decode()
        assert 'id="librenms-module-table"' in body
        assert f"Updated serial for {module_type.model} in {bay.name}" in body
        module.refresh_from_db()
        assert module.serial == "ACTION-1"

    def test_htmx_action_without_a_snapshot_renders_the_empty_tab(self, client, settings):
        """A missing inventory snapshot reports the error inside the re-rendered tab."""
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-no-snapshot")
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:install_selected", kwargs={"pk": device.pk})

        response = client.post(
            url,
            {"server_key": self.SERVER_KEY, "select": "8201"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert response["HX-Retarget"] == "#module-sync-content"
        assert "HX-Redirect" not in response
        body = response.content.decode()
        assert "No cached inventory data" in body
        assert "Refresh Modules" in body

    def _seed_serial_mismatch(self, suffix, *, conflict, librenms_id):
        """Seed a device whose installed module's serial differs from the cached LibreNMS serial."""
        from dcim.models import Module

        device = make_device(f"modules-preview-{suffix}")
        bay = make_module_bay(device, "Preview Bay")
        module_type = make_module_type(f"PREVIEW-CARD-{suffix}")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD-SERIAL")
        self._seed_inventory(device, bay, module_type, librenms_id=librenms_id)
        if conflict:
            other = make_device(f"modules-preview-{suffix}-holder")
            other_bay = make_module_bay(other, "Holder Bay")
            Module.objects.create(device=other, module_bay=other_bay, module_type=module_type, serial="ACTION-1")
        return device, module

    @pytest.mark.parametrize(
        ("conflict", "expected_action"),
        [(False, "update-module-serial"), (True, "move-module")],
        ids=["update_serial_only", "move"],
    )
    def test_the_mismatch_preview_answers_a_whole_modal_of_bound_forms(
        self, client, settings, conflict, expected_action
    ):
        """The preview fills #htmx-modal-content, so it must carry the header the JS used to build."""
        from django.urls import reverse

        from netbox_librenms_plugin.tests._html_helpers import open_tags

        self._configure_server(settings)
        suffix = "move" if conflict else "serial"
        device, module = self._seed_serial_mismatch(suffix, conflict=conflict, librenms_id=9210 + int(conflict))
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:module_mismatch_preview", kwargs={"pk": device.pk})

        response = client.get(
            url,
            {
                "module_id": str(module.pk),
                "ent_index": "8201",
                "server_key": self.SERVER_KEY,
                "selected_device_id": str(device.pk),
            },
        )

        assert response.status_code == 200
        body = response.content.decode()
        assert 'id="htmx-modal-label"' in body
        assert "closeHtmxModal()" in body
        assert 'class="modal-body"' in body
        forms = open_tags(body, "form")
        # Both branches render the replace form plus the one the conflict state selects.
        assert len(forms) == 2
        assert any(expected_action in form["action"] for form in forms)
        assert any("replace-module" in form["action"] for form in forms)
        for form in forms:
            # The forms are swapped into the modal, so their own target must be the module tab.
            assert form["hx-target"] == "#module-sync-content"
            assert form["hx-swap"] == "innerHTML"
            assert form["hx-sync"] == "#module-sync-content:drop"


class TestAddBayTemplatePostValidation:
    """AddBayTemplateView refuses a tampered target_kind before it touches any object."""

    def test_invalid_target_kind_returns_400(self, client):
        """The modal echoes target_kind from its GET render, so a bad value is a tampered request."""
        from dcim.models import ModuleBayTemplate
        from django.urls import reverse

        device = make_device("add-bay-template-tampered")
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})

        response = client.post(url, {"target_kind": "bogus", "target_pk": "1", "name": "Slot 1"})

        assert response.status_code == 400
        assert b"Invalid target_kind" in response.content
        assert not ModuleBayTemplate.objects.filter(name="Slot 1").exists()


class TestVirtualChassisInterfaceNormalization:
    def _module(self, device, name):
        from dcim.models import Module

        bay = make_module_bay(device, f"{name} Bay")
        return Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=make_module_type(name),
            status="active",
        )

    def test_interface_name_is_rewritten_to_the_member_position(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        first = make_device("normalize-first")
        device = make_device("normalize-second")
        make_virtual_chassis("normalize-vc", first, device)
        module = self._module(device, "NORMALIZE-CARD")
        interface = Interface.objects.create(device=device, module=module, name="Te1/1/1", type="other")

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        interface.refresh_from_db()
        assert result == {"renamed": 1, "adopted": 0, "removed": 0, "skipped": 0}
        assert interface.name == "Te2/1/1"

    def test_existing_desired_name_is_adopted_and_generated_duplicate_removed(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        first = make_device("normalize-adopt-first")
        device = make_device("normalize-adopt-second")
        make_virtual_chassis("normalize-adopt-vc", first, device)
        module = self._module(device, "NORMALIZE-ADOPT-CARD")
        generated = Interface.objects.create(device=device, module=module, name="Te1/1/2", type="other")
        standalone = make_interface(device, "Te2/1/2")

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        standalone.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 1, "removed": 1, "skipped": 0}
        assert standalone.module == module
        assert not Interface.objects.filter(pk=generated.pk).exists()

    @pytest.mark.parametrize(
        ("counts", "expected"),
        [
            ({}, ""),
            ({"renamed": 2, "adopted": 1, "removed": 1, "skipped": 0}, "renamed 2, adopted 1, removed 1"),
            ({"skipped": 3}, "skipped 3"),
        ],
    )
    def test_adjustment_summary_reports_nonzero_actions(self, counts, expected):
        from netbox_librenms_plugin.views.sync.modules import _format_vc_adjustment_summary

        assert _format_vc_adjustment_summary(counts) == expected


class TestModuleComponentAdoption:
    """Authorize standalone adoption for each component type NetBox replicates."""

    @staticmethod
    def _couples_rear_port(model):
        return any(field.name == "rear_port" for field in model._meta.get_fields())

    @staticmethod
    def _type_kwargs(model_name):
        if "Interface" in model_name:
            return {"type": "1000base-t"}
        if "FrontPort" in model_name or "RearPort" in model_name:
            return {"type": "8p8c"}
        return {}

    @pytest.mark.parametrize("spec_index", range(8))
    def test_matching_standalone_component_is_authorized(self, spec_index):
        from dcim.constants import MODULE_TOKEN
        from dcim.models import Module, RearPort, RearPortTemplate

        from netbox_librenms_plugin.views.sync.modules import (
            _authorize_adoptable_module_components,
            _module_component_specs,
            _module_template_adoption_name,
        )

        specs = _module_component_specs()
        template_attribute, _component_attribute, component_model = specs[spec_index]
        device = make_device(f"adopt-{component_model.__name__.lower()}")
        module_type = make_module_type(f"ADOPT-{component_model.__name__}")
        template_model = getattr(type(module_type), template_attribute).rel.related_model
        template_kwargs = {
            "module_type": module_type,
            "name": f"{MODULE_TOKEN}-adopt-{component_model.__name__.lower()}",
            **self._type_kwargs(template_model.__name__),
        }
        if template_model.__name__ == "FrontPortTemplate" and self._couples_rear_port(template_model):
            template_kwargs["rear_port"] = RearPortTemplate.objects.create(
                module_type=module_type,
                name=f"rear-template-{spec_index}",
                type="8p8c",
            )
            template_kwargs["rear_port_position"] = 1
        template = template_model.objects.create(**template_kwargs)
        bay = make_module_bay(device, f"Adopt Bay {spec_index}")
        bay.position = "A1"
        bay.save(update_fields=["position"])
        module = Module(device=device, module_bay=bay, module_type=module_type)
        expected_name = _module_template_adoption_name(template_attribute, template, module)
        component_kwargs = {
            "device": device,
            "name": expected_name,
            **self._type_kwargs(component_model.__name__),
        }
        if component_model.__name__ == "FrontPort" and self._couples_rear_port(component_model):
            component_kwargs["rear_port"] = RearPort.objects.create(
                device=device,
                name=f"rear-component-{spec_index}",
                type="8p8c",
            )
            component_kwargs["rear_port_position"] = 1
        standalone = component_model.objects.create(**component_kwargs)

        allowed = {model: model.objects.all() for _, _, model in specs}
        authorized = _authorize_adoptable_module_components(module, allowed)

        assert authorized[component_model] == {standalone.pk}


class TestModuleInterfaceMessages:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (
                {"status": "bound", "interface": "Ethernet1", "port_id": 1},
                "Updated interface Ethernet1 for CARD in Slot 1.",
            ),
            (
                {"status": "bound", "adopted_count": 2},
                "Updated interfaces for CARD in Slot 1: adopted 2 existing standalone interface(s).",
            ),
            (
                {"status": "bound", "interface": "Ethernet1", "adopted_count": 2},
                "Updated interface Ethernet1 for CARD in Slot 1 and adopted 2 existing standalone interface(s).",
            ),
            (
                {"status": "bound", "changed": False, "adopted_count": 0},
                "No interface changes were needed for CARD in Slot 1.",
            ),
            (
                {"status": "bound"},
                "No interface changes were needed for CARD in Slot 1.",
            ),
        ],
    )
    def test_message_describes_the_real_mutation(self, result, expected):
        from netbox_librenms_plugin.views.sync.modules import _module_interface_update_message

        assert _module_interface_update_message(result, "CARD in Slot 1") == expected

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ({"status": "installed", "module_pk": 1}, True),
            ({"status": "installed", "module_pk": None}, False),
            ({"status": "skipped", "module_pk": 1, "reason": "bay already occupied"}, True),
            ({"status": "skipped", "module_pk": 1, "reason": "no matching type"}, False),
        ],
    )
    def test_bind_attempt_requires_a_stable_module_context(self, result, expected):
        from netbox_librenms_plugin.views.sync.modules import _should_attempt_bind_for_result

        assert _should_attempt_bind_for_result(result) is expected


def test_module_interface_prediction_signal_uses_real_templates():
    from dcim.models import InterfaceTemplate, Module
    from django.dispatch import receiver

    from netbox_librenms_plugin.signals import predict_module_interface_names
    from netbox_librenms_plugin.utils import get_module_template_interface_names

    device = make_device("prediction-device")
    module_type = make_module_type("PREDICTION-CARD")
    InterfaceTemplate.objects.create(module_type=module_type, name="Ethernet1", type="other")
    bay = make_module_bay(device, "Prediction Bay")
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")

    @receiver(predict_module_interface_names)
    def rewrite(sender, device, module, names, **kwargs):
        return [f"{name}/child" for name in names]

    try:
        assert get_module_template_interface_names(device, module) == ["Ethernet1/child"]
    finally:
        predict_module_interface_names.disconnect(rewrite)


def test_inventory_rows_are_json_serializable_for_cache_boundaries():
    row = _inventory_item(1, "CARD", "Slot 1", serial="SERIAL")

    assert json.loads(json.dumps(row)) == row
