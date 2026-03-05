"""Sync action views for module/inventory installation from LibreNMS."""

from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


class InstallModuleView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Install a NetBox Module into a ModuleBay from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        module_bay_id = request.POST.get("module_bay_id")
        module_type_id = request.POST.get("module_type_id")
        serial = request.POST.get("serial", "").strip()

        if not module_bay_id or not module_type_id:
            messages.error(request, "Missing module bay or module type.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        server_key = request.POST.get("server_key") or None

        module_bay = get_object_or_404(ModuleBay, pk=module_bay_id, device=device)
        module_type = get_object_or_404(ModuleType, pk=module_type_id)

        # Check if bay already has a module installed
        if hasattr(module_bay, "installed_module") and module_bay.installed_module:
            messages.warning(request, f"Module bay '{module_bay.name}' already has a module installed.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        try:
            with transaction.atomic():
                module = Module(
                    device=device,
                    module_bay=module_bay,
                    module_type=module_type,
                    serial=serial,
                    status="active",
                )
                module.full_clean()
                module.save()

            cache.delete(self.get_cache_key(device, "inventory", server_key=server_key))
            messages.success(
                request, f"Installed {module_type.model} in {module_bay.name} (serial: {serial or 'N/A'})."
            )
        except Exception as e:
            messages.error(request, f"Failed to install module: {e}")

        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
        return redirect(f"{sync_url}?tab=modules#librenms-module-table")


class InstallBranchView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Install a module and all its installable descendants from LibreNMS inventory."""

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        parent_index = request.POST.get("parent_index")
        server_key = request.POST.get("server_key") or None

        if not parent_index:
            messages.error(request, "Missing parent inventory index.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        try:
            parent_index = int(parent_index)
        except ValueError:
            messages.error(request, "Invalid parent inventory index.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Get cached inventory data
        cached_data = cache.get(self.get_cache_key(device, "inventory", server_key=server_key))
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Build index map and collect the branch to install
        index_map = {idx: item for item in cached_data if (idx := item.get("entPhysicalIndex")) is not None}
        branch_items = self._collect_branch(parent_index, cached_data)

        if not branch_items:
            messages.warning(request, "No installable items found in this branch.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Load module types (with mappings)
        module_types = self._get_module_types()

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries
        from netbox_librenms_plugin.models import ModuleBayMapping

        all_mappings = list(ModuleBayMapping.objects.all())
        exact_mappings = [m for m in all_mappings if not m.is_regex]
        regex_mappings = [m for m in all_mappings if m.is_regex]

        # Install top-down: each install may create new child bays
        installed = []
        skipped = []
        failed = []

        try:
            with transaction.atomic():
                for item in branch_items:
                    result = self._install_single(
                        device,
                        item,
                        index_map,
                        module_types,
                        ModuleBay,
                        ModuleType,
                        Module,
                        exact_mappings=exact_mappings,
                        regex_mappings=regex_mappings,
                    )
                    if result["status"] == "installed":
                        installed.append(result["name"])
                    elif result["status"] == "skipped":
                        skipped.append(f"{result['name']}: {result['reason']}")
                    else:
                        failed.append(f"{result['name']}: {result['reason']}")
        except Exception as e:
            messages.error(request, f"Branch install failed: {e}")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Report results
        if installed:
            cache.delete(self.get_cache_key(device, "inventory", server_key=server_key))
            messages.success(request, f"Installed {len(installed)} module(s): {', '.join(installed)}")
        if skipped:
            messages.info(request, f"Skipped {len(skipped)}: {'; '.join(skipped)}")
        if failed:
            messages.warning(request, f"Failed {len(failed)}: {'; '.join(failed)}")

        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
        return redirect(f"{sync_url}?tab=modules#librenms-module-table")

    def _collect_branch(self, parent_index, inventory_data):
        """Collect all items in a branch depth-first, parent first.

        Returns items in install order (parent before children).
        """
        items = []
        parent = next((i for i in inventory_data if i.get("entPhysicalIndex") == parent_index), None)
        if parent:
            model = (parent.get("entPhysicalModelName") or "").strip()
            if model:
                items.append(parent)
            self._collect_children(parent_index, inventory_data, items, visited={parent_index})
        return items

    def _collect_children(self, parent_idx, inventory_data, items, visited=None):
        """Recursively collect children with models, depth-first."""
        if visited is None:
            visited = set()
        children = [i for i in inventory_data if i.get("entPhysicalContainedIn") == parent_idx]
        for child in children:
            child_idx = child.get("entPhysicalIndex")
            if child_idx is None:
                continue
            if child_idx in visited:
                continue
            visited.add(child_idx)
            model = (child.get("entPhysicalModelName") or "").strip()
            if model:
                items.append(child)
            # Always recurse to find deeper items (containers may lack models)
            self._collect_children(child_idx, inventory_data, items, visited)

    def _get_module_types(self):
        """Get all module types indexed by model, with mappings applied."""
        from dcim.models import ModuleType

        from netbox_librenms_plugin.models import ModuleTypeMapping

        types = ModuleType.objects.all().select_related("manufacturer")
        result = {}
        for mt in types:
            result[mt.model] = mt
            if mt.part_number and mt.part_number != mt.model:
                result[mt.part_number] = mt
        for mapping in ModuleTypeMapping.objects.select_related("netbox_module_type__manufacturer"):
            result[mapping.librenms_model] = mapping.netbox_module_type
        return result

    def _install_single(
        self,
        device,
        item,
        index_map,
        module_types,
        ModuleBay,
        ModuleType,
        Module,
        exact_mappings=None,
        regex_mappings=None,
    ):
        """Try to install a single inventory item.

        Re-fetches module bays each time since parent installs create new ones.
        Scopes bay lookup to the correct parent module to handle duplicate bay names.
        """
        from netbox_librenms_plugin.utils import apply_normalization_rules

        model_name = (item.get("entPhysicalModelName") or "").strip()
        serial = (item.get("entPhysicalSerialNum") or "").strip()
        name = item.get("entPhysicalName", "") or model_name

        # Match module type (direct, then normalization fallback)
        matched_type = module_types.get(model_name)
        if not matched_type and model_name:
            manufacturer = getattr(getattr(device, "device_type", None), "manufacturer", None)
            normalized = apply_normalization_rules(model_name, "module_type", manufacturer=manufacturer)
            if normalized != model_name:
                matched_type = module_types.get(normalized)
        if not matched_type:
            return {"status": "skipped", "name": name, "reason": "no matching type"}

        # Re-fetch module bays (parent install creates new child bays)
        bays = ModuleBay.objects.filter(device=device).select_related("installed_module__module_type")

        # Use preloaded mappings if provided, otherwise load from DB
        if exact_mappings is None or regex_mappings is None:
            from netbox_librenms_plugin.models import ModuleBayMapping

            all_mappings = list(ModuleBayMapping.objects.all())
            exact_mappings = [m for m in all_mappings if not m.is_regex]
            regex_mappings = [m for m in all_mappings if m.is_regex]

        bay_mappings = exact_mappings + regex_mappings

        # Determine if this item belongs under an installed module
        # by tracing its LibreNMS parent hierarchy to an installed item
        parent_module_id = self._find_parent_module_id(item, index_map, bays, bay_mappings)

        if parent_module_id:
            bay_dict = {bay.name: bay for bay in bays if bay.module_id == parent_module_id}
        else:
            bay_dict = {bay.name: bay for bay in bays if not bay.module_id}

        # Match module bay using preloaded mapping data
        matched_bay = self._match_bay(item, index_map, bay_dict, exact_mappings, regex_mappings)
        if not matched_bay:
            return {"status": "skipped", "name": name, "reason": "no matching bay"}

        # Check if already installed
        if hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
            return {"status": "skipped", "name": name, "reason": "bay already occupied"}

        # Install
        try:
            with transaction.atomic():  # savepoint: failure here won't abort parent tx
                module = Module(
                    device=device,
                    module_bay=matched_bay,
                    module_type=matched_type,
                    serial=serial,
                    status="active",
                )
                module.full_clean()
                module.save()
        except Exception as e:
            error_msg = str(e)
            if "dcim_interface_unique_device_name" in error_msg:
                error_msg = (
                    "duplicate interface name — this module type's interface template "
                    "uses {module} which resolves to the same name for all siblings. "
                    "An interface naming plugin with a rewrite rule for this module type can fix this."
                )
            return {"status": "failed", "name": name, "reason": error_msg}

        return {"status": "installed", "name": f"{matched_type.model} → {matched_bay.name}"}

    @staticmethod
    def _find_parent_module_id(item, index_map, device_bays, bay_mappings):
        """Find the NetBox module ID for the installed parent of this inventory item.

        Walks up the LibreNMS hierarchy to find an ancestor whose name matches
        an installed module bay on the device.

        Args:
            item: The inventory item dict.
            index_map: Dict mapping entPhysicalIndex to inventory item.
            device_bays: Pre-fetched queryset/list of ModuleBay objects for the device.
            bay_mappings: Pre-fetched list of all ModuleBayMapping objects.
        """

        current = item
        # Build bay name->bay dict from pre-fetched bays for fast lookup
        bay_by_name = {}
        for bay in device_bays:
            if bay.name not in bay_by_name:
                bay_by_name[bay.name] = bay
        # Build mapping dict keyed by librenms_name for fast lookup
        mapping_by_name = {}
        for m in bay_mappings:
            if m.librenms_name not in mapping_by_name:
                mapping_by_name[m.librenms_name] = m

        visited = set()
        while True:
            parent_idx = current.get("entPhysicalContainedIn", 0)
            if not parent_idx or parent_idx not in index_map:
                return None
            if parent_idx in visited:
                return None
            visited.add(parent_idx)
            parent = index_map[parent_idx]
            parent_name = parent.get("entPhysicalName", "")
            parent_descr = parent.get("entPhysicalDescr", "")

            # Check if this parent matches an installed module bay on the device
            for bay in device_bays:
                if hasattr(bay, "installed_module") and bay.installed_module:
                    if bay.name == parent_name or (parent_descr and bay.name == parent_descr):
                        return bay.installed_module.pk

            # Also check ModuleBayMapping for indirect matches using pre-fetched data
            for name in [parent_name, parent_descr]:
                if not name:
                    continue
                mapping = mapping_by_name.get(name)
                if mapping:
                    bay = bay_by_name.get(mapping.netbox_bay_name)
                    if bay and hasattr(bay, "installed_module") and bay.installed_module:
                        return bay.installed_module.pk

            current = parent

    @staticmethod
    def _match_bay(item, index_map, module_bays, exact_mappings, regex_mappings):
        """Match an inventory item to a module bay (same logic as BaseModuleTableView)."""
        import re

        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Resolve parent name by walking up the containment hierarchy
        contained_in = item.get("entPhysicalContainedIn", 0)
        parent_name = None
        if contained_in:
            visited_anc = set()
            current_idx = contained_in
            while current_idx and current_idx not in visited_anc:
                visited_anc.add(current_idx)
                ancestor = index_map.get(current_idx)
                if not ancestor:
                    break
                ancestor_name = ancestor.get("entPhysicalName", "")
                if ancestor_name:
                    parent_name = ancestor_name
                    break
                current_idx = ancestor.get("entPhysicalContainedIn", 0)

        item_name = item.get("entPhysicalName", "")
        item_descr = item.get("entPhysicalDescr", "")
        phys_class = item.get("entPhysicalClass", "")

        # Build candidate names: parent, item name, item description
        candidate_names = [n for n in [parent_name, item_name, item_descr] if n]

        # Check mapping for each candidate (exact match, in-memory lookup)
        # Group exact_mappings by (librenms_name, librenms_class) for O(1) lookup
        exact_by_name: dict = {}
        for m in exact_mappings:
            exact_by_name.setdefault(m.librenms_name, []).append(m)

        for name in candidate_names:
            candidates_for_name = exact_by_name.get(name, [])
            mapping = None
            if phys_class:
                mapping = next((m for m in candidates_for_name if m.librenms_class == phys_class), None)
            if not mapping:
                mapping = next((m for m in candidates_for_name if m.librenms_class == ""), None)
            if mapping and mapping.netbox_bay_name in module_bays:
                return module_bays[mapping.netbox_bay_name]

        # Regex pattern matching using preloaded list
        for name in candidate_names:
            bay = BaseModuleTableView._lookup_regex_bay_mapping(re, name, phys_class, module_bays, regex_mappings)
            if bay:
                return bay

        # Fallback: exact match on candidate names against bay dict
        for name in candidate_names:
            if name in module_bays:
                return module_bays[name]

        # Positional fallback for items inside converters
        return BaseModuleTableView._match_bay_by_position(item, index_map, module_bays)


class InstallSelectedView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Install a user-selected set of inventory items by their entPhysicalIndex values.

    Reuses InstallBranchView._install_single for each selected item so every item
    goes through the same type/bay/serial resolution pipeline as a branch install.
    Only items where a matching bay *and* module type are found will be installed;
    items with no bay or no type are silently skipped (same behaviour as branch).
    """

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        server_key = request.POST.get("server_key") or None

        selected_indices = request.POST.getlist("select")
        if not selected_indices:
            messages.warning(request, "No modules selected.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        cached_data = cache.get(self.get_cache_key(device, "inventory", server_key=server_key))
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        try:
            # Use dict.fromkeys to preserve order while deduplicating
            selected_list = list(dict.fromkeys(int(i) for i in selected_indices))
        except ValueError:
            messages.error(request, "Invalid selection.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        index_map = {idx: item for item in cached_data if (idx := item.get("entPhysicalIndex")) is not None}
        items = [index_map[idx] for idx in selected_list if idx in index_map]

        if not items:
            messages.warning(request, "None of the selected indices matched cached inventory.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        helper = InstallBranchView()
        module_types = helper._get_module_types()

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries
        from netbox_librenms_plugin.models import ModuleBayMapping

        all_mappings = list(ModuleBayMapping.objects.all())
        exact_mappings = [m for m in all_mappings if not m.is_regex]
        regex_mappings = [m for m in all_mappings if m.is_regex]

        installed, skipped, failed = [], [], []

        try:
            with transaction.atomic():
                for item in items:
                    result = helper._install_single(
                        device,
                        item,
                        index_map,
                        module_types,
                        ModuleBay,
                        ModuleType,
                        Module,
                        exact_mappings=exact_mappings,
                        regex_mappings=regex_mappings,
                    )
                    if result["status"] == "installed":
                        installed.append(result["name"])
                    elif result["status"] == "skipped":
                        skipped.append(f"{result['name']}: {result['reason']}")
                    else:
                        failed.append(f"{result['name']}: {result['reason']}")
        except Exception as e:
            messages.error(request, f"Install failed: {e}")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        if installed:
            cache.delete(self.get_cache_key(device, "inventory", server_key=server_key))
            messages.success(request, f"Installed {len(installed)} module(s): {', '.join(installed)}")
        if skipped:
            messages.info(request, f"Skipped {len(skipped)}: {'; '.join(skipped)}")
        if failed:
            messages.warning(request, f"Failed {len(failed)}: {'; '.join(failed)}")

        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
        return redirect(f"{sync_url}?tab=modules#librenms-module-table")
