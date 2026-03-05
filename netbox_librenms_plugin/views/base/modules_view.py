from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
)


# entPhysicalClass values relevant for module sync
# Includes vendor-specific classes (Nokia TIMETRA-CHASSIS-MIB uses ioModule, cpmModule, etc.)
INVENTORY_CLASSES = {
    "module",
    "powerSupply",
    "fan",
    "port",
    "container",
    "ioModule",
    "cpmModule",
    "mdaModule",
    "fabricModule",
    "xioModule",
}

# Model name values that indicate a generic/empty container (not real hardware)
_GENERIC_CONTAINER_MODELS = {"", "BUILTIN", "Default", "N/A"}


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

        # Cache the merged inventory data, namespaced by server to avoid cross-server collisions
        cache.set(
            self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key),
            inventory_data,
            timeout=self.librenms_api.cache_timeout,
        )

        context = self._build_context(request, obj, inventory_data)
        messages.success(request, "Inventory data refreshed successfully.")
        return render(request, self.partial_template_name, {"module_sync": context})

    def get_context_data(self, request, obj):
        """Get context from cache (used by the main sync view on initial page load)."""
        cached_data = cache.get(self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key))
        if not cached_data:
            return {"table": None, "object": obj, "cache_expiry": None}
        return self._build_context(request, obj, cached_data)

    def _build_context(self, request, obj, inventory_data):
        """Build context with matched inventory items and table."""
        # Build a lookup of all inventory items by index for parent resolution
        # Skip items with missing entPhysicalIndex to avoid KeyError on malformed data.
        index_map = {idx: item for item in inventory_data if (idx := item.get("entPhysicalIndex")) is not None}

        # Store manufacturer for normalization rules in _build_row
        self._device_manufacturer = getattr(getattr(obj, "device_type", None), "manufacturer", None)

        # Preload all ModuleBayMapping rows once to avoid N+1 queries in _match_module_bay.
        from netbox_librenms_plugin.models import ModuleBayMapping

        all_bay_mappings = list(ModuleBayMapping.objects.all())
        self._exact_bay_mappings = [m for m in all_bay_mappings if not m.is_regex]
        self._regex_bay_mappings = [m for m in all_bay_mappings if m.is_regex]

        # Get NetBox module bays and modules for this device
        device_bays, module_scoped_bays = self._get_module_bays(obj)
        module_types = self._get_module_types()

        # Collect top-level items and their sub-components
        # Include synthetic transceiver items (from vendors without ENTITY-MIB SFP data)
        # Exclude items that have any ancestor with an INVENTORY_CLASSES class
        # (they appear as sub-components under that ancestor)
        top_items = []
        for item in inventory_data:
            if item.get("_from_transceiver_api"):
                top_items.append(item)
                continue
            phys_class = item.get("entPhysicalClass")
            if phys_class not in INVENTORY_CLASSES:
                continue
            # Skip items with generic model names (not real hardware).
            # Containers with empty model are physical slot representations.
            model = (item.get("entPhysicalModelName") or "").strip()
            if phys_class == "container" and model in _GENERIC_CONTAINER_MODELS:
                continue
            if model and model in _GENERIC_CONTAINER_MODELS:
                continue
            # Walk up ancestor chain; skip if any ancestor is an inventory-class item.
            # Containers with empty model are physical slot/bay representations, not
            # real modules — skip them so children can be top-level items.
            is_descendant = False
            current_idx = item.get("entPhysicalContainedIn", 0)
            visited_ancestors = set()
            while current_idx and current_idx in index_map and current_idx not in visited_ancestors:
                visited_ancestors.add(current_idx)
                ancestor = index_map[current_idx]
                anc_class = ancestor.get("entPhysicalClass")
                if anc_class in INVENTORY_CLASSES:
                    anc_model = (ancestor.get("entPhysicalModelName") or "").strip()
                    # Containers with generic/empty models are physical slot representations
                    if anc_class == "container" and anc_model in _GENERIC_CONTAINER_MODELS:
                        current_idx = ancestor.get("entPhysicalContainedIn", 0)
                        continue
                    is_descendant = True
                    break
                current_idx = ancestor.get("entPhysicalContainedIn", 0)
            if is_descendant:
                continue
            top_items.append(item)

        table_data = []
        from netbox_librenms_plugin.utils import apply_normalization_rules

        # Build combined bay lookup so synthetic transceiver entries (which may
        # live inside installed modules) can find their module-scoped bays.
        all_bays = dict(device_bays)
        for scope_bays in module_scoped_bays.values():
            all_bays.update(scope_bays)

        for item in top_items:
            # Transceiver API entries may live inside installed modules, so they
            # need the full bay map.  ENTITY-MIB top-level items must only match
            # device-level bays to avoid name collisions with module-scoped bays
            # that share the same name as a device bay.
            item_bays = all_bays if item.get("_from_transceiver_api") else device_bays
            row = self._build_row(item, index_map, item_bays, module_types, depth=0)
            parent_row_idx = len(table_data)
            table_data.append(row)

            # Determine which bays sub-components should match against:
            # If parent matched a bay with an installed module, use that module's child bays.
            # If parent matched a bay but it's NOT installed, children can't be installed
            # individually (parent must be installed first to create child bays).
            parent_module_id = None
            parent_bay_matched_but_uninstalled = False
            if row.get("module_bay_id"):
                matched_bay = item_bays.get(row["module_bay"])
                if matched_bay and hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
                    parent_module_id = matched_bay.installed_module.pk
                else:
                    # Parent matched a bay but it's not installed yet
                    parent_bay_matched_but_uninstalled = True

            if parent_bay_matched_but_uninstalled:
                # Empty dict: children can't match any bay individually
                child_bays = {}
            elif parent_module_id:
                child_bays = module_scoped_bays.get(parent_module_id, {})
            else:
                child_bays = device_bays

            # Find sub-components with a model name (transceivers, converters, etc.)
            # Track bay scope per depth level so nested modules use correct bays
            bays_by_depth = {0: child_bays}
            parent_ent_idx = item.get("entPhysicalIndex")
            if parent_ent_idx is None:
                continue
            sub_items = self._get_sub_components(parent_ent_idx, inventory_data)
            for depth, sub_item in sub_items:
                scope_bays = bays_by_depth.get(depth, child_bays)
                sub_row = self._build_row(sub_item, index_map, scope_bays, module_types, depth=depth)
                table_data.append(sub_row)

                # Update bay scope for children of this sub-item.
                # Must always set bays_by_depth[depth+1] when a bay was matched to
                # prevent stale scope from a previously-processed sibling at the
                # same depth leaking into this item's children.
                if sub_row.get("module_bay_id"):
                    matched_sub_bay = scope_bays.get(sub_row["module_bay"])
                    if (
                        matched_sub_bay
                        and hasattr(matched_sub_bay, "installed_module")
                        and matched_sub_bay.installed_module
                    ):
                        sub_module_id = matched_sub_bay.installed_module.pk
                        bays_by_depth[depth + 1] = module_scoped_bays.get(sub_module_id, {})
                    else:
                        # Bay matched but not yet installed: reset child scope so
                        # items under this uninstalled module don't accidentally
                        # inherit bays from a previously-processed installed sibling.
                        bays_by_depth[depth + 1] = {}

                # Mark parent if any child is installable
                if sub_row.get("can_install"):
                    table_data[parent_row_idx]["has_installable_children"] = True

            # When parent is installable but children can't match bays yet
            # (parent module not installed), enable "Install Branch" if any child
            # has a matching module type (branch install handles bay creation).
            if (
                parent_bay_matched_but_uninstalled
                and row.get("can_install")
                and not table_data[parent_row_idx].get("has_installable_children")
            ):
                for _depth, sub_item in sub_items:
                    sub_model = (sub_item.get("entPhysicalModelName") or "").strip()
                    if not sub_model:
                        continue
                    matched = module_types.get(sub_model)
                    if not matched:
                        normalized = apply_normalization_rules(
                            sub_model,
                            "module_type",
                            manufacturer=getattr(self, "_device_manufacturer", None),
                        )
                        matched = module_types.get(normalized)
                    if matched:
                        table_data[parent_row_idx]["has_installable_children"] = True
                        break

        # Sort top-level groups by status, keeping children after their parent
        table_data = self._sort_with_hierarchy(table_data)

        table = self.get_table(table_data, obj)
        table.configure(request)

        cache_ttl = getattr(cache, "ttl", lambda k: None)(
            self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key)
        )
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl is not None else None

        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": self.librenms_api.server_key,
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

        # Build lookup of existing inventory items by index and serial
        inv_by_index = {idx: item for item in inventory_data if (idx := item.get("entPhysicalIndex")) is not None}
        inv_serials = {
            (item.get("entPhysicalSerialNum") or "").strip()
            for item in inventory_data
            if (item.get("entPhysicalSerialNum") or "").strip()
        }

        # Build port_id → ifName lookup for better synthetic item naming
        port_name_map = self._build_port_name_map(transceivers)

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
                # Skip if serial already exists in ENTITY-MIB data (avoid duplicates)
                if serial and serial in inv_serials:
                    continue
                # Create synthetic inventory item for SFPs not in entity inventory
                port_id = txr.get("port_id", 0)
                ifname = port_name_map.get(port_id)
                if ifname:
                    name = ifname
                elif port_id:
                    name = f"Transceiver (port {port_id})"
                else:
                    name = f"Transceiver {ent_idx}"

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
                # Update dedupe maps so subsequent iterations skip this entry
                inv_by_index[ent_idx] = synthetic
                if serial:
                    inv_serials.add(serial)

        return inventory_data

    def _build_port_name_map(self, transceivers):
        """Build port_id → ifName mapping for transceiver ports.

        Fetches port data from LibreNMS to resolve port IDs to interface names,
        enabling better bay matching for synthetic transceiver items (e.g.,
        Nokia 1/1/c1 instead of opaque port IDs).
        """
        port_ids = {txr.get("port_id") for txr in transceivers if txr.get("port_id")}
        if not port_ids:
            return {}

        success, ports_data = self.librenms_api.get_ports(self.librenms_id)
        if not success or not isinstance(ports_data, dict):
            return {}

        return {
            p["port_id"]: p["ifName"]
            for p in ports_data.get("ports", [])
            if p.get("port_id") in port_ids and p.get("ifName")
        }

    def _get_sub_components(self, parent_idx, inventory_data):
        """Find descendant items with a model name (real hardware, not empty containers).

        Returns list of (depth, item) tuples.
        """
        # Precompute children_by_parent once to avoid O(n²) linear scans per recursion
        children_by_parent: dict = {}
        for item in inventory_data:
            p = item.get("entPhysicalContainedIn")
            if p is not None:
                children_by_parent.setdefault(p, []).append(item)

        results = []
        self._collect_descendants(parent_idx, children_by_parent, depth=1, results=results, visited={parent_idx})
        return results

    def _collect_descendants(self, parent_idx, children_by_parent, depth, results, visited=None):
        """Recursively collect descendant items that have a model name."""
        if visited is None:
            visited = set()
        for child in children_by_parent.get(parent_idx, []):
            child_idx = child.get("entPhysicalIndex")
            if child_idx is None:
                continue
            if child_idx in visited:
                continue
            visited.add(child_idx)
            model = (child.get("entPhysicalModelName") or "").strip()
            if model and model not in _GENERIC_CONTAINER_MODELS:
                results.append((depth, child))
                # Continue looking for deeper components (e.g., SFPs inside converters)
                self._collect_descendants(child_idx, children_by_parent, depth + 1, results, visited)
            else:
                # Skip generic/empty items, but check their children
                self._collect_descendants(child_idx, children_by_parent, depth, results, visited)

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

    def _match_module_bay(self, item, index_map, module_bays):
        """
        Try to match an inventory item to a NetBox ModuleBay.
        Checks ModuleBayMapping table first (exact then regex), then falls back
        to exact parent name match, then positional matching.
        """
        import re

        parent_name = self._find_parent_container_name(item, index_map)
        item_name = item.get("entPhysicalName", "")
        item_descr = item.get("entPhysicalDescr", "")
        phys_class = item.get("entPhysicalClass", "")

        # Build candidate names: parent, item name, item description
        candidate_names = [n for n in [parent_name, item_name, item_descr] if n]

        # Use preloaded exact mappings (set in _build_context to avoid N+1 queries).
        exact_mappings = getattr(self, "_exact_bay_mappings", None)
        if exact_mappings is None:
            from netbox_librenms_plugin.models import ModuleBayMapping

            exact_mappings = list(ModuleBayMapping.objects.filter(is_regex=False))

        # Check ModuleBayMapping table for each candidate (exact match)
        for name in candidate_names:
            if phys_class:
                mapping = next(
                    (m for m in exact_mappings if m.librenms_name == name and m.librenms_class == phys_class), None
                )
                if not mapping:
                    mapping = next(
                        (m for m in exact_mappings if m.librenms_name == name and m.librenms_class == ""), None
                    )
            else:
                mapping = next((m for m in exact_mappings if m.librenms_name == name and m.librenms_class == ""), None)

            if mapping and mapping.netbox_bay_name in module_bays:
                return module_bays[mapping.netbox_bay_name]

        # Use preloaded regex mappings.
        regex_mappings = getattr(self, "_regex_bay_mappings", None)
        if regex_mappings is None:
            from netbox_librenms_plugin.models import ModuleBayMapping

            regex_mappings = list(ModuleBayMapping.objects.filter(is_regex=True))

        # Regex pattern matching on all candidate names
        for name in candidate_names:
            bay = self._lookup_regex_bay_mapping(re, name, phys_class, module_bays, regex_mappings)
            if bay and self._fpc_slot_matches(name, bay):
                return bay

        # Fallback: exact match on candidate names against bay dict
        for name in candidate_names:
            if name in module_bays:
                return module_bays[name]

        # Positional fallback: determine slot number from container sibling order
        # Handles SFPs inside converters where containers are unnamed
        bay = self._match_bay_by_position(item, index_map, module_bays)
        if bay:
            return bay

        return None

    @staticmethod
    def _fpc_slot_matches(candidate_name, bay):
        """Validate that a regex-matched bay's parent slot position is consistent with
        a positional descriptor like 'Model @ FPC/pic/port'.

        Returns True if the descriptor has no FPC reference, or if the bay's parent
        module slot position matches the FPC number in the descriptor. Prevents
        orphaned top-level items (e.g. QSFP @ 1/1/1 when FPC1 is not installed)
        from incorrectly matching bays belonging to a different FPC's module.
        """
        import re as _re

        match = _re.search(r"@\s+(\d+)/", candidate_name)
        if not match:
            return True
        expected_fpc = match.group(1)
        module = getattr(bay, "module", None)
        if not module:
            return True
        parent_bay = getattr(module, "module_bay", None)
        if not parent_bay:
            return True
        return parent_bay.position == expected_fpc

    @staticmethod
    def _lookup_regex_bay_mapping(re, name, phys_class, module_bays, regex_mappings):
        """Try regex ModuleBayMapping patterns against a name.

        ``regex_mappings`` is a pre-filtered list of is_regex=True ModuleBayMapping
        objects (passed in from the caller to avoid per-item DB queries).

        Returns matched module bay or None.
        """
        # Filter preloaded list by class (exact class match or empty-class fallback)
        if phys_class:
            candidates = [m for m in regex_mappings if m.librenms_class == phys_class or m.librenms_class == ""]
        else:
            candidates = [m for m in regex_mappings if m.librenms_class == ""]

        for mapping in candidates:
            try:
                match = re.fullmatch(mapping.librenms_name, name)
                if match:
                    resolved_bay = match.expand(mapping.netbox_bay_name)
            except re.error:
                continue
            if match:
                if resolved_bay in module_bays:
                    bay = module_bays[resolved_bay]
                    if BaseModuleTableView._fpc_slot_matches(name, bay):
                        return bay
        return None

    @staticmethod
    def _match_bay_by_position(item, index_map, module_bays):
        """Match bay by item's positional order among container siblings.

        When an item is inside a container (no model), walk up to find the
        nearest ancestor with a model, count which container slot the item
        occupies, and match to the bay by number (e.g., SFP 1, SFP 2).
        """
        # Walk up through modelless containers to find the parent with a model.
        # Use a visited set to detect cycles and avoid infinite loops.
        current_idx = item.get("entPhysicalContainedIn", 0)
        container_idx = None
        visited = set()
        while current_idx and current_idx in index_map and current_idx not in visited:
            visited.add(current_idx)
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
            has_nested_name_conflict,
            module_type_is_end_module,
            module_type_uses_module_path,
            module_type_uses_module_token,
            supports_module_path,
        )

        model_name = item.get("entPhysicalModelName", "") or ""
        serial = item.get("entPhysicalSerialNum", "") or ""
        phys_class = item.get("entPhysicalClass", "")
        name = item.get("entPhysicalName", "") or "-"
        description = item.get("entPhysicalDescr", "") or ""

        # Match to NetBox module bay
        matched_bay = self._match_module_bay(item, index_map, module_bays)

        # Match to NetBox module type (direct lookup, then normalization fallback)
        matched_type = module_types.get(model_name) if model_name else None
        if not matched_type and model_name:
            normalized = apply_normalization_rules(
                model_name, "module_type", manufacturer=getattr(self, "_device_manufacturer", None)
            )
            if normalized != model_name:
                matched_type = module_types.get(normalized)

        # Badge flags — purely informational, never block installation
        needs_module_path = matched_type and module_type_uses_module_path(matched_type)
        # {module_path} used but NetBox version does not support it → "Upgrade NetBox" hint
        netbox_upgrade_needed = bool(needs_module_path and not supports_module_path())
        # End module still using old {module} when {module_path} is available → "Upgrade module-type" hint
        suggest_type_upgrade = bool(
            matched_type
            and supports_module_path()
            and module_type_is_end_module(matched_type)
            and module_type_uses_module_token(matched_type)
        )

        # Check for nested module naming conflicts
        name_conflict = matched_type and matched_bay and has_nested_name_conflict(matched_type, matched_bay)

        # Determine status
        status = self._determine_status(matched_bay, matched_type, serial)

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

        if netbox_upgrade_needed:
            row["row_class"] = "table-warning"
            row["module_path_warning"] = (
                "This module type uses {module_path} in its interface templates. "
                "The current NetBox version does not support {module_path} yet — "
                "installation will proceed but interface naming may not work as expected. "
                "Upgrade NetBox to enable full {module_path} support."
            )

        if suggest_type_upgrade:
            row["module_type_upgrade_hint"] = (
                "This module type uses {module} in its interface templates. "
                "Since this NetBox version supports {module_path}, consider updating "
                "the module type's interface templates to use {module_path} for "
                "precise per-slot interface naming."
            )

        if name_conflict:
            row["row_class"] = "table-warning"
            row["name_conflict_warning"] = (
                "This module type uses {module} in its interface template. "
                "Installing multiple siblings will create duplicate interface names. "
                "An interface naming plugin with a rewrite rule for this module type can resolve this."
            )

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
            elif matched_type:
                # Bay exists, type matched, no module installed → can install
                row["can_install"] = True

        if matched_type:
            row["module_type_url"] = matched_type.get_absolute_url()

        return row

    def _determine_status(self, matched_bay, matched_type, serial):
        """Determine the sync status for an inventory item."""
        if matched_bay and matched_type:
            return "Matched"
        if not matched_bay:
            return "No Bay"
        if not matched_type:
            return "No Type"
        return "Unmatched"
