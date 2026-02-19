from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


# entPhysicalClass values relevant for module sync
# Includes vendor-specific classes (Nokia TIMETRA-CHASSIS-MIB uses ioModule, cpmModule, etc.)
INVENTORY_CLASSES = {
    "module",
    "powerSupply",
    "fan",
    "ioModule",
    "cpmModule",
    "mdaModule",
    "fabricModule",
    "xioModule",
}


class BaseModuleTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing module/inventory data from LibreNMS.
    Fetches inventory, matches against NetBox module bays and module types,
    and renders a comparison table.
    """

    model = None
    partial_template_name = "netbox_librenms_plugin/_module_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device)."""
        return get_object_or_404(self.model, pk=pk)

    def get_table(self, data, obj):
        """Returns the table class. Subclasses should override."""
        raise NotImplementedError("Subclasses must implement get_table()")

    def post(self, request, pk):
        """Fetch inventory from LibreNMS, cache it, and render the module sync table."""
        obj = self.get_object(pk)

        self.librenms_id = self.librenms_api.get_librenms_id(obj)
        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS.")
            return render(
                request,
                self.partial_template_name,
                {"module_sync": {"object": obj, "table": None, "cache_expiry": None}},
            )

        success, inventory_data = self.librenms_api.get_device_inventory(self.librenms_id)

        if not success:
            messages.error(request, f"Failed to fetch inventory from LibreNMS: {inventory_data}")
            return render(
                request,
                self.partial_template_name,
                {"module_sync": {"object": obj, "table": None, "cache_expiry": None}},
            )

        # Fetch transceiver data and merge with inventory
        inventory_data = self._merge_transceiver_data(inventory_data)

        # Cache the merged inventory data
        cache.set(
            self.get_cache_key(obj, "inventory"),
            inventory_data,
            timeout=self.librenms_api.cache_timeout,
        )

        context = self._build_context(request, obj, inventory_data)
        messages.success(request, "Inventory data refreshed successfully.")
        return render(request, self.partial_template_name, {"module_sync": context})

    def get_context_data(self, request, obj):
        """Get context from cache (used by the main sync view on initial page load)."""
        cached_data = cache.get(self.get_cache_key(obj, "inventory"))
        if not cached_data:
            return {"table": None, "object": obj, "cache_expiry": None}
        return self._build_context(request, obj, cached_data)

    def _build_context(self, request, obj, inventory_data):
        """Build context with matched inventory items and table."""
        # Build a lookup of all inventory items by index for parent resolution
        index_map = {item["entPhysicalIndex"]: item for item in inventory_data}

        # Get NetBox module bays and modules for this device
        device_bays, module_scoped_bays = self._get_module_bays(obj)
        module_types = self._get_module_types()

        # Collect top-level items and their sub-components
        # Include synthetic transceiver items (from vendors without ENTITY-MIB SFP data)
        # Include top-level 'port' items with a model (transceivers in fixed-form switches)
        # Exclude items whose parent is also an INVENTORY_CLASSES item (they appear as sub-components)
        top_items = []
        for item in inventory_data:
            if item.get("_from_transceiver_api"):
                top_items.append(item)
                continue
            phys_class = item.get("entPhysicalClass", "")
            if phys_class not in INVENTORY_CLASSES:
                # Include top-level port items with a model name (e.g., Arcos SFPs)
                # but only if no ancestor is an INVENTORY_CLASSES item (would appear as sub-component)
                if phys_class == "port" and (item.get("entPhysicalModelName") or "").strip():
                    has_module_ancestor = False
                    cur_idx = item.get("entPhysicalContainedIn", 0)
                    seen = set()
                    while cur_idx and cur_idx in index_map and cur_idx not in seen:
                        seen.add(cur_idx)
                        ancestor = index_map[cur_idx]
                        if ancestor.get("entPhysicalClass", "") in INVENTORY_CLASSES:
                            has_module_ancestor = True
                            break
                        cur_idx = ancestor.get("entPhysicalContainedIn", 0)
                    if not has_module_ancestor:
                        top_items.append(item)
                continue
            # Check if parent is also an inventory-class item (skip if so)
            parent_idx = item.get("entPhysicalContainedIn", 0)
            if parent_idx and parent_idx in index_map:
                parent_class = index_map[parent_idx].get("entPhysicalClass", "")
                if parent_class in INVENTORY_CLASSES:
                    continue
            top_items.append(item)

        table_data = []
        for item in top_items:
            row = self._build_row(item, index_map, device_bays, module_types, depth=0)
            parent_idx = len(table_data)
            table_data.append(row)

            # Determine which bays sub-components should match against:
            # If parent matched a bay with an installed module, use that module's child bays
            parent_module_id = None
            if row.get("module_bay_id"):
                matched_bay = device_bays.get(row["module_bay"])
                if matched_bay and hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
                    parent_module_id = matched_bay.installed_module.pk

            child_bays = module_scoped_bays.get(parent_module_id, {}) if parent_module_id else device_bays

            # Find sub-components with a model name (transceivers, converters, etc.)
            # Track bay scope per depth level so nested modules use correct bays
            bays_by_depth = {0: child_bays}
            sub_items = self._get_sub_components(item["entPhysicalIndex"], inventory_data)
            for depth, sub_item in sub_items:
                scope_bays = bays_by_depth.get(depth, child_bays)
                sub_row = self._build_row(sub_item, index_map, scope_bays, module_types, depth=depth)
                table_data.append(sub_row)

                # If this sub-item matched an installed module, deeper items use its bays
                if sub_row.get("module_bay_id"):
                    matched_sub_bay = scope_bays.get(sub_row["module_bay"])
                    if (
                        matched_sub_bay
                        and hasattr(matched_sub_bay, "installed_module")
                        and matched_sub_bay.installed_module
                    ):
                        sub_module_id = matched_sub_bay.installed_module.pk
                        bays_by_depth[depth + 1] = module_scoped_bays.get(sub_module_id, {})

                # Mark parent if any child is installable
                if sub_row.get("can_install"):
                    table_data[parent_idx]["has_installable_children"] = True

        # Sort top-level groups by status, keeping children after their parent
        table_data = self._sort_with_hierarchy(table_data)

        table = self.get_table(table_data, obj)
        table.configure(request)

        cache_ttl = cache.ttl(self.get_cache_key(obj, "inventory"))
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl is not None else None

        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
        }

    def _merge_transceiver_data(self, inventory_data):
        """Merge transceiver API data with entity inventory.

        For vendors like Nokia that don't expose SFPs in ENTITY-MIB,
        the transceiver API provides SFP model, serial, and type info.

        Strategy:
        - For transceivers matching existing inventory items by entity_physical_index:
          supplement entPhysicalModelName if empty
        - For transceivers NOT in inventory: create synthetic inventory items
          so they appear in the modules table
        """
        success, transceivers = self.librenms_api.get_device_transceivers(self.librenms_id)
        if not success or not transceivers:
            return inventory_data

        # Build lookup of existing inventory items by index
        inv_by_index = {item["entPhysicalIndex"]: item for item in inventory_data}

        # Build set of existing serial numbers to detect duplicates
        # (transceiver API may use different entity indices than ENTITY-MIB)
        existing_serials = {(item.get("entPhysicalSerialNum") or "").strip() for item in inventory_data}
        existing_serials.discard("")

        # Types that are containers, not real transceiver modules
        SKIP_TYPES = {"Port Container", "Port", ""}

        for txr in transceivers:
            ent_idx = txr.get("entity_physical_index")
            if not ent_idx:
                continue

            model = (txr.get("model") or "").strip()
            serial = (txr.get("serial") or "").strip()
            txr_type = (txr.get("type") or "").strip()

            # Skip containers and entries with no useful data
            if txr_type in SKIP_TYPES and not model and not serial:
                continue

            # Use transceiver type as model fallback (e.g., "CFP2/QSFP28")
            display_model = model or (txr_type if txr_type not in SKIP_TYPES else "")

            if ent_idx in inv_by_index:
                # Supplement existing inventory item if model is missing
                existing = inv_by_index[ent_idx]
                if not (existing.get("entPhysicalModelName") or "").strip() and display_model:
                    existing["entPhysicalModelName"] = display_model
                if not (existing.get("entPhysicalSerialNum") or "").strip() and serial:
                    existing["entPhysicalSerialNum"] = serial
            else:
                # Skip if this serial already exists in inventory (duplicate from
                # transceiver API using different entity indices than ENTITY-MIB)
                if serial and serial in existing_serials:
                    continue

                # Create synthetic inventory item for SFPs not in entity inventory
                # Try to find port name for this transceiver
                port_id = txr.get("port_id", 0)
                name = f"Transceiver (port {port_id})" if port_id else f"Transceiver {ent_idx}"

                synthetic = {
                    "entPhysicalIndex": ent_idx,
                    "entPhysicalName": name,
                    "entPhysicalClass": "port",
                    "entPhysicalModelName": display_model,
                    "entPhysicalSerialNum": serial,
                    "entPhysicalDescr": txr_type,
                    "entPhysicalContainedIn": 0,
                    "_from_transceiver_api": True,
                }
                inventory_data.append(synthetic)

        return inventory_data

    def _get_sub_components(self, parent_idx, inventory_data):
        """Find descendant items with a model name (real hardware, not empty containers).

        Returns list of (depth, item) tuples.
        """
        results = []
        self._collect_descendants(parent_idx, inventory_data, depth=1, results=results)
        return results

    def _collect_descendants(self, parent_idx, inventory_data, depth, results):
        """Recursively collect descendant items that have a model name."""
        children = [i for i in inventory_data if i.get("entPhysicalContainedIn") == parent_idx]
        for child in children:
            model = (child.get("entPhysicalModelName") or "").strip()
            if model:
                results.append((depth, child))
                # Continue looking for deeper components (e.g., SFPs inside converters)
                self._collect_descendants(child["entPhysicalIndex"], inventory_data, depth + 1, results)
            else:
                # Skip containers without models, but check their children
                self._collect_descendants(child["entPhysicalIndex"], inventory_data, depth, results)

    def _sort_with_hierarchy(self, table_data):
        """Sort table keeping children grouped under their parent."""
        status_order = {"Installed": 0, "Serial Mismatch": 1, "Matched": 2, "No Type": 3, "No Bay": 4, "Unmatched": 5}

        # Group into top-level items with their children
        groups = []
        current_group = None
        for row in table_data:
            if row.get("depth", 0) == 0:
                current_group = {"parent": row, "children": []}
                groups.append(current_group)
            elif current_group is not None:
                current_group["children"].append(row)

        # Sort groups by parent status
        groups.sort(key=lambda g: status_order.get(g["parent"]["status"], 99))

        # Flatten back
        result = []
        for group in groups:
            result.append(group["parent"])
            result.extend(group["children"])
        return result

    def _get_module_bays(self, obj):
        """Get module bays for the device, organized by scope.

        Returns:
            tuple: (device_bays, module_bays) where:
                - device_bays: {name: bay} for device-level bays (module=None)
                - module_bays: {module_id: {name: bay}} for bays created by installed modules
        """
        from dcim.models import ModuleBay

        bays = ModuleBay.objects.filter(device=obj).select_related("installed_module__module_type")
        device_bays = {}
        module_scoped_bays = {}
        for bay in bays:
            if bay.module_id:
                module_scoped_bays.setdefault(bay.module_id, {})[bay.name] = bay
            else:
                device_bays[bay.name] = bay
        return device_bays, module_scoped_bays

    def _get_module_types(self):
        """Get all module types, indexed by model (part_number), with ModuleTypeMapping checked first."""
        from dcim.models import ModuleType

        from netbox_librenms_plugin.models import ModuleTypeMapping

        # Build base lookup from NetBox module types
        types = ModuleType.objects.all().select_related("manufacturer")
        result = {}
        for mt in types:
            result[mt.model] = mt
            if mt.part_number and mt.part_number != mt.model:
                result[mt.part_number] = mt

        # Overlay with explicit ModuleTypeMapping entries (take priority)
        for mapping in ModuleTypeMapping.objects.select_related("netbox_module_type__manufacturer"):
            result[mapping.librenms_model] = mapping.netbox_module_type

        return result

    def _find_parent_container_name(self, item, index_map):
        """Resolve the parent container name for an inventory item."""
        contained_in = item.get("entPhysicalContainedIn", 0)
        if contained_in == 0:
            return None
        parent = index_map.get(contained_in)
        if parent:
            return parent.get("entPhysicalName", "")
        return None

    def _lookup_bay_mapping(self, name, phys_class, module_bays):
        """Look up a ModuleBayMapping by librenms_name and return matched bay.

        Tries exact matches first, then regex patterns (is_regex=True).
        For regex matches, netbox_bay_name supports backreferences.
        """
        import re

        from netbox_librenms_plugin.models import ModuleBayMapping

        if not name:
            return None

        # Exact match first
        filters = {"librenms_name": name, "is_regex": False}
        if phys_class:
            mapping = ModuleBayMapping.objects.filter(**filters, librenms_class=phys_class).first()
            if not mapping:
                mapping = ModuleBayMapping.objects.filter(**filters, librenms_class="").first()
        else:
            mapping = ModuleBayMapping.objects.filter(**filters, librenms_class="").first()
        if mapping and mapping.netbox_bay_name in module_bays:
            return module_bays[mapping.netbox_bay_name]

        # Regex pattern matching
        regex_filters = {"is_regex": True}
        if phys_class:
            regex_mappings = list(ModuleBayMapping.objects.filter(**regex_filters, librenms_class=phys_class)) + list(
                ModuleBayMapping.objects.filter(**regex_filters, librenms_class="")
            )
        else:
            regex_mappings = list(ModuleBayMapping.objects.filter(**regex_filters, librenms_class=""))

        for mapping in regex_mappings:
            try:
                match = re.fullmatch(mapping.librenms_name, name)
            except re.error:
                continue
            if match:
                resolved_bay = match.expand(mapping.netbox_bay_name)
                if resolved_bay in module_bays:
                    return module_bays[resolved_bay]
        return None

    def _match_module_bay(self, item, index_map, module_bays):
        """
        Try to match an inventory item to a NetBox ModuleBay.
        Checks ModuleBayMapping table first, then falls back to exact parent name match,
        then description-based matching, then positional matching.
        """
        parent_name = self._find_parent_container_name(item, index_map)
        item_name = item.get("entPhysicalName", "")
        item_descr = item.get("entPhysicalDescr", "")
        phys_class = item.get("entPhysicalClass", "")

        # Check ModuleBayMapping table for parent container name, then item name
        bay = self._lookup_bay_mapping(parent_name, phys_class, module_bays)
        if bay:
            return bay
        bay = self._lookup_bay_mapping(item_name, phys_class, module_bays)
        if bay:
            return bay

        # Fallback: exact match on parent container name or item name
        if parent_name and parent_name in module_bays:
            return module_bays[parent_name]
        if item_name and item_name in module_bays:
            return module_bays[item_name]

        # Description-based matching: check mapping table and direct match
        bay = self._lookup_bay_mapping(item_descr, phys_class, module_bays)
        if bay:
            return bay
        if item_descr and item_descr in module_bays:
            return module_bays[item_descr]

        # Extract position from description "@ X/Y/Z" pattern (e.g., Juniper)
        bay = self._match_bay_from_description(item_descr, module_bays)
        if bay:
            return bay

        # Positional fallback: determine slot number from container sibling order
        # Handles SFPs inside converters where containers are unnamed
        bay = self._match_bay_by_position(item, index_map, module_bays)
        if bay:
            return bay

        return None

    @staticmethod
    def _match_bay_from_description(descr, module_bays):
        """Extract slot position from description and match to bay.

        Handles patterns like "MODEL @ 0/0/5" -> "Transceiver 0/0/5",
        and "PSM 0" / "Fan Tray 0" by direct lookup.
        """
        import re

        if not descr:
            return None

        # Try "@ X/Y/Z" pattern (e.g., "QSFP-100GBASE-LR4 @ 0/0/0")
        match = re.search(r"@\s*(\d+(?:/\d+)+)", descr)
        if match:
            position = match.group(1)
            for prefix in ("Transceiver", "SFP", "Port", "Slot"):
                bay_name = f"{prefix} {position}"
                if bay_name in module_bays:
                    return module_bays[bay_name]

        # Try description as-is for names like "PSM 0", "Fan Tray 0"
        if descr in module_bays:
            return module_bays[descr]

        return None

    @staticmethod
    def _match_bay_by_position(item, index_map, module_bays):
        """Match bay by item's positional order among container siblings.

        When an item is inside a container (no model), walk up to find the
        nearest ancestor with a model, count which container slot the item
        occupies, and match to the bay by number (e.g., SFP 1, SFP 2).
        """
        # Walk up through modelless containers to find the parent with a model
        current_idx = item.get("entPhysicalContainedIn", 0)
        container_idx = None
        for _ in range(5):
            if not current_idx or current_idx not in index_map:
                return None
            ancestor = index_map[current_idx]
            model = (ancestor.get("entPhysicalModelName") or "").strip()
            if model:
                # Found the parent with a model; container_idx is the intermediate container
                break
            container_idx = current_idx
            current_idx = ancestor.get("entPhysicalContainedIn", 0)
        else:
            return None

        if not container_idx:
            return None

        # Determine position: count siblings of the container under the parent
        parent_with_model_idx = current_idx
        siblings = sorted(
            [i for i in index_map.values() if i.get("entPhysicalContainedIn") == parent_with_model_idx],
            key=lambda x: x.get("entPhysicalParentRelPos", 0),
        )
        slot_num = None
        for i, sib in enumerate(siblings):
            if sib["entPhysicalIndex"] == container_idx:
                slot_num = i + 1
                break

        if slot_num is None:
            return None

        # Try common bay naming patterns
        for pattern in [f"SFP {slot_num}", f"Slot {slot_num}", f"Bay {slot_num}", f"Port {slot_num}"]:
            if pattern in module_bays:
                return module_bays[pattern]

        return None

    def _build_row(self, item, index_map, module_bays, module_types, depth=0):
        """Build a single table row from a LibreNMS inventory item."""
        from netbox_librenms_plugin.utils import (
            apply_normalization_rules,
            module_type_uses_module_path,
            supports_module_path,
        )

        model_name = item.get("entPhysicalModelName", "") or ""
        serial = item.get("entPhysicalSerialNum", "") or ""
        phys_class = item.get("entPhysicalClass", "")
        name = item.get("entPhysicalName", "") or "-"
        description = item.get("entPhysicalDescr", "") or ""

        # Match to NetBox module bay
        matched_bay = self._match_module_bay(item, index_map, module_bays)

        # Match to NetBox module type (exact first, then normalized)
        matched_type = module_types.get(model_name) if model_name else None
        if not matched_type and model_name:
            normalized = apply_normalization_rules(model_name, "module_type")
            if normalized != model_name:
                matched_type = module_types.get(normalized)

        # Check {module_path} compatibility
        needs_module_path = matched_type and module_type_uses_module_path(matched_type)
        module_path_blocked = needs_module_path and not supports_module_path()

        # Determine status
        status = self._determine_status(matched_bay, matched_type, serial, module_path_blocked)

        row = {
            "name": name,
            "model": model_name or "-",
            "serial": serial or "-",
            "description": description,
            "item_class": phys_class,
            "module_bay": matched_bay.name if matched_bay else "-",
            "module_type": matched_type.model if matched_type else "-",
            "status": status,
            "row_class": "",
            "can_install": False,
            "module_bay_id": matched_bay.pk if matched_bay else None,
            "module_type_id": matched_type.pk if matched_type else None,
            "depth": depth,
            "ent_physical_index": item.get("entPhysicalIndex"),
            "has_installable_children": False,
        }

        if module_path_blocked:
            from netbox_librenms_plugin.utils import MODULE_PATH_MIN_VERSION

            row["row_class"] = "table-warning"
            row["module_path_warning"] = f"Requires NetBox ≥ {MODULE_PATH_MIN_VERSION}"

        # Add URLs for matched objects
        if matched_bay:
            row["module_bay_url"] = matched_bay.get_absolute_url()
            # Check if a module is already installed in this bay
            if hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
                installed = matched_bay.installed_module
                row["installed_module"] = installed
                row["module_url"] = installed.get_absolute_url()
                # Check serial match
                if serial and installed.serial and installed.serial.strip() == serial.strip():
                    status = "Installed"
                    row["row_class"] = "table-success"
                elif serial and installed.serial and installed.serial.strip() != serial.strip():
                    status = "Serial Mismatch"
                    row["row_class"] = "table-danger"
                else:
                    status = "Installed"
                    row["row_class"] = "table-success"
                row["status"] = status
            elif matched_type and not module_path_blocked:
                # Bay exists, type matched, no module installed → can install
                row["can_install"] = True

        if matched_type:
            row["module_type_url"] = matched_type.get_absolute_url()

        return row

    def _determine_status(self, matched_bay, matched_type, serial, module_path_blocked=False):
        """Determine the sync status for an inventory item."""
        if module_path_blocked:
            return "Requires Upgrade"
        if matched_bay and matched_type:
            return "Matched"
        if not matched_bay:
            return "No Bay"
        if not matched_type:
            return "No Type"
        return "Unmatched"


class InstallModuleView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Install a NetBox Module into a ModuleBay from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions_json("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        module_bay_id = request.POST.get("module_bay_id")
        module_type_id = request.POST.get("module_type_id")
        serial = request.POST.get("serial", "").strip()

        if not module_bay_id or not module_type_id:
            messages.error(request, "Missing module bay or module type.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        module_bay = get_object_or_404(ModuleBay, pk=module_bay_id, device=device)
        module_type = get_object_or_404(ModuleType, pk=module_type_id)

        # Block install if module type uses {module_path} and NetBox doesn't support it
        from netbox_librenms_plugin.utils import (
            MODULE_PATH_MIN_VERSION,
            module_type_uses_module_path,
            supports_module_path,
        )

        if module_type_uses_module_path(module_type) and not supports_module_path():
            messages.error(
                request,
                f"Cannot install {module_type.model}: its interface templates use "
                f"{{module_path}} which requires NetBox ≥ {MODULE_PATH_MIN_VERSION}.",
            )
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

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

                # Post-install: apply InterfaceNameRule if one exists
                renamed = self._apply_interface_name_rules(module, module_bay)

            rename_msg = f" ({renamed} interface(s) renamed)" if renamed else ""
            messages.success(
                request, f"Installed {module_type.model} in {module_bay.name} (serial: {serial or 'N/A'}).{rename_msg}"
            )
        except Exception as e:
            messages.error(request, f"Failed to install module: {e}")

        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
        return redirect(f"{sync_url}?tab=modules#librenms-module-table")

    @staticmethod
    def _apply_interface_name_rules(module, module_bay):
        """Apply InterfaceNameRule rename after module installation.

        Looks up a matching rule for (module_type, parent_module_type) and
        renames interfaces created by NetBox's template instantiation.

        Returns:
            Number of interfaces renamed, or 0 if no rule matched.
        """
        from dcim.models import Interface

        from netbox_librenms_plugin.models import InterfaceNameRule
        from netbox_librenms_plugin.utils import evaluate_name_template

        module_type = module.module_type

        # Determine parent module type (if installed inside another module)
        parent_module_type = None
        if module_bay.parent:
            parent_bay = module_bay.parent
            if hasattr(parent_bay, "installed_module") and parent_bay.installed_module:
                parent_module_type = parent_bay.installed_module.module_type

        # Look up rule: most specific first (device_type + parent), then broader matches
        device_type = module.device.device_type if module.device else None
        rule = None
        if parent_module_type and device_type:
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type=parent_module_type,
                device_type=device_type,
            ).first()
        if not rule and parent_module_type:
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type=parent_module_type,
                device_type__isnull=True,
            ).first()
        if not rule and device_type:
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type__isnull=True,
                device_type=device_type,
            ).first()
        if not rule:
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type__isnull=True,
                device_type__isnull=True,
            ).first()

        if not rule:
            return 0

        # Build context variables for template evaluation
        bay_position = module_bay.position or "0"
        # If position is a template expression (e.g., {module}), extract from bay name
        if bay_position.startswith("{"):
            import re

            match = re.search(r"(\d+)$", module_bay.name)
            bay_position = match.group(1) if match else "0"
        parent_bay_position = "0"
        sfp_slot = bay_position
        slot = bay_position

        if module_bay.parent:
            parent_bay = module_bay.parent
            parent_bay_position = parent_bay.position or "0"
            # slot is typically the top-level module position
            if parent_bay.parent and hasattr(parent_bay.parent, "installed_module"):
                grandparent = parent_bay.parent
                slot = grandparent.position or parent_bay_position
            else:
                slot = parent_bay_position
        elif hasattr(module_bay, "module") and module_bay.module:
            # Bay belongs to an installed module (not nested, but module-scoped)
            # Resolve slot from the module's own bay position
            owner_module = module_bay.module
            if hasattr(owner_module, "module_bay") and owner_module.module_bay:
                slot = owner_module.module_bay.position or bay_position

        interfaces = Interface.objects.filter(module=module)
        renamed = 0

        for iface in interfaces:
            variables = {
                "slot": slot,
                "bay_position": bay_position,
                "parent_bay_position": parent_bay_position,
                "sfp_slot": sfp_slot,
                "base": iface.name,
            }

            if rule.channel_count > 0:
                # Breakout: rename base and create additional channel interfaces
                for ch in range(rule.channel_count):
                    variables["channel"] = str(rule.channel_start + ch)
                    new_name = evaluate_name_template(rule.name_template, variables)
                    if ch == 0:
                        iface.name = new_name
                        iface.full_clean()
                        iface.save()
                        renamed += 1
                    else:
                        # Create additional breakout interfaces
                        breakout_iface = Interface(
                            device=module.device,
                            module=module,
                            name=new_name,
                            type=iface.type,
                            enabled=iface.enabled,
                        )
                        breakout_iface.full_clean()
                        breakout_iface.save()
                        renamed += 1
            else:
                # Simple rename (converter offset, etc.)
                new_name = evaluate_name_template(rule.name_template, variables)
                if new_name != iface.name:
                    iface.name = new_name
                    iface.full_clean()
                    iface.save()
                    renamed += 1

        return renamed


class InstallBranchView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Install a module and all its installable descendants from LibreNMS inventory."""

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions_json("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        parent_index = request.POST.get("parent_index")

        if not parent_index:
            messages.error(request, "Missing parent inventory index.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        parent_index = int(parent_index)

        # Get cached inventory data
        cached_data = cache.get(self.get_cache_key(device, "inventory"))
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Build index map and collect the branch to install
        index_map = {item["entPhysicalIndex"]: item for item in cached_data}
        branch_items = self._collect_branch(parent_index, cached_data)

        if not branch_items:
            messages.warning(request, "No installable items found in this branch.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Load module types (with mappings)
        module_types = self._get_module_types()

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
        parent = next((i for i in inventory_data if i["entPhysicalIndex"] == parent_index), None)
        if parent:
            model = (parent.get("entPhysicalModelName") or "").strip()
            if model:
                items.append(parent)
            self._collect_children(parent_index, inventory_data, items)
        return items

    def _collect_children(self, parent_idx, inventory_data, items):
        """Recursively collect children with models, depth-first."""
        children = [i for i in inventory_data if i.get("entPhysicalContainedIn") == parent_idx]
        for child in children:
            model = (child.get("entPhysicalModelName") or "").strip()
            if model:
                items.append(child)
            # Always recurse to find deeper items (containers may lack models)
            self._collect_children(child["entPhysicalIndex"], inventory_data, items)

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

    def _install_single(self, device, item, index_map, module_types, ModuleBay, ModuleType, Module):
        """Try to install a single inventory item.

        Re-fetches module bays each time since parent installs create new ones.
        Scopes bay lookup to the correct parent module to handle duplicate bay names.
        """
        from netbox_librenms_plugin.models import ModuleBayMapping
        from netbox_librenms_plugin.utils import module_type_uses_module_path, supports_module_path

        model_name = (item.get("entPhysicalModelName") or "").strip()
        serial = (item.get("entPhysicalSerialNum") or "").strip()
        name = item.get("entPhysicalName", "") or model_name

        # Match module type
        matched_type = module_types.get(model_name)
        if not matched_type:
            return {"status": "skipped", "name": name, "reason": "no matching type"}

        # Check {module_path} compatibility
        if module_type_uses_module_path(matched_type) and not supports_module_path():
            return {"status": "skipped", "name": name, "reason": "requires {module_path}"}

        # Re-fetch module bays (parent install creates new child bays)
        bays = ModuleBay.objects.filter(device=device).select_related("installed_module__module_type")

        # Determine if this item belongs under an installed module
        # by tracing its LibreNMS parent hierarchy to an installed item
        parent_module_id = self._find_parent_module_id(item, index_map, device, ModuleBay)

        if parent_module_id:
            bay_dict = {bay.name: bay for bay in bays if bay.module_id == parent_module_id}
        else:
            bay_dict = {bay.name: bay for bay in bays if not bay.module_id}

        # Match module bay using mapping table
        matched_bay = self._match_bay(item, index_map, bay_dict, ModuleBayMapping)
        if not matched_bay:
            return {"status": "skipped", "name": name, "reason": "no matching bay"}

        # Check if already installed
        if hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
            return {"status": "skipped", "name": name, "reason": "bay already occupied"}

        # Install
        module = Module(
            device=device,
            module_bay=matched_bay,
            module_type=matched_type,
            serial=serial,
            status="active",
        )
        module.full_clean()
        module.save()

        # Apply interface name rules
        InstallModuleView._apply_interface_name_rules(module, matched_bay)

        return {"status": "installed", "name": f"{matched_type.model} → {matched_bay.name}"}

    @staticmethod
    def _find_parent_module_id(item, index_map, device, ModuleBay):
        """Find the NetBox module ID for the installed parent of this inventory item.

        Walks up the LibreNMS hierarchy to find an ancestor whose name matches
        an installed module bay on the device.
        """
        from netbox_librenms_plugin.models import ModuleBayMapping

        current = item
        for _ in range(10):  # max depth guard
            parent_idx = current.get("entPhysicalContainedIn", 0)
            if not parent_idx or parent_idx not in index_map:
                return None
            parent = index_map[parent_idx]
            parent_name = parent.get("entPhysicalName", "")

            # Check if this parent matches an installed module bay on the device
            device_bays = ModuleBay.objects.filter(device=device, module_id__isnull=True).select_related(
                "installed_module"
            )

            for bay in device_bays:
                if hasattr(bay, "installed_module") and bay.installed_module:
                    if bay.name == parent_name:
                        return bay.installed_module.pk

            # Also check ModuleBayMapping for indirect matches
            mapping = ModuleBayMapping.objects.filter(librenms_name=parent_name).first()
            if mapping:
                bay = (
                    ModuleBay.objects.filter(device=device, name=mapping.netbox_bay_name, module_id__isnull=True)
                    .select_related("installed_module")
                    .first()
                )
                if bay and hasattr(bay, "installed_module") and bay.installed_module:
                    return bay.installed_module.pk

            current = parent
        return None

    @staticmethod
    def _match_bay(item, index_map, module_bays, ModuleBayMapping):
        """Match an inventory item to a module bay (same logic as BaseModuleTableView)."""
        # Resolve parent name
        contained_in = item.get("entPhysicalContainedIn", 0)
        parent_name = None
        if contained_in:
            parent = index_map.get(contained_in)
            if parent:
                parent_name = parent.get("entPhysicalName", "")

        item_name = item.get("entPhysicalName", "")
        phys_class = item.get("entPhysicalClass", "")

        # Check mapping for parent container name
        if parent_name:
            filters = {"librenms_name": parent_name}
            if phys_class:
                mapping = ModuleBayMapping.objects.filter(**filters, librenms_class=phys_class).first()
                if not mapping:
                    mapping = ModuleBayMapping.objects.filter(**filters, librenms_class="").first()
            else:
                mapping = ModuleBayMapping.objects.filter(**filters, librenms_class="").first()
            if mapping and mapping.netbox_bay_name in module_bays:
                return module_bays[mapping.netbox_bay_name]

        # Check mapping for item name
        if item_name:
            filters = {"librenms_name": item_name}
            if phys_class:
                mapping = ModuleBayMapping.objects.filter(**filters, librenms_class=phys_class).first()
                if not mapping:
                    mapping = ModuleBayMapping.objects.filter(**filters, librenms_class="").first()
            else:
                mapping = ModuleBayMapping.objects.filter(**filters, librenms_class="").first()
            if mapping and mapping.netbox_bay_name in module_bays:
                return module_bays[mapping.netbox_bay_name]

        # Fallback: exact match on parent container name
        if parent_name and parent_name in module_bays:
            return module_bays[parent_name]
        if item_name and item_name in module_bays:
            return module_bays[item_name]

        # Positional fallback for items inside converters
        return BaseModuleTableView._match_bay_by_position(item, index_map, module_bays)
