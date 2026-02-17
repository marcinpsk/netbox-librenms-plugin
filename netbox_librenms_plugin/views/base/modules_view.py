import re

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
INVENTORY_CLASSES = {"module", "powerSupply", "fan"}


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

        # Cache the raw inventory data
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
        module_bays = self._get_module_bays(obj)
        module_types = self._get_module_types()

        # Collect top-level items and their sub-components
        top_items = [item for item in inventory_data if item.get("entPhysicalClass") in INVENTORY_CLASSES]

        table_data = []
        for item in top_items:
            row = self._build_row(item, index_map, module_bays, module_types, depth=0)
            table_data.append(row)

            # Find sub-components with a model name (transceivers, converters, etc.)
            sub_items = self._get_sub_components(item["entPhysicalIndex"], inventory_data)
            for depth, sub_item in sub_items:
                sub_row = self._build_row(sub_item, index_map, module_bays, module_types, depth=depth)
                table_data.append(sub_row)

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
        """Get module bays for the device, indexed by name."""
        from dcim.models import ModuleBay

        bays = ModuleBay.objects.filter(device=obj).select_related("installed_module__module_type")
        return {bay.name: bay for bay in bays}

    def _get_module_types(self):
        """Get all module types, indexed by model (part_number)."""
        from dcim.models import ModuleType

        types = ModuleType.objects.all().select_related("manufacturer")
        result = {}
        for mt in types:
            result[mt.model] = mt
            if mt.part_number and mt.part_number != mt.model:
                result[mt.part_number] = mt
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
        Matches by parent container name or item name.
        """
        parent_name = self._find_parent_container_name(item, index_map)
        item_name = item.get("entPhysicalName", "")

        # Try exact match on parent container name first
        if parent_name and parent_name in module_bays:
            return module_bays[parent_name]

        # Try matching by item name (e.g., "Power Supply 1" → "PS1")
        for bay_name, bay in module_bays.items():
            if self._names_match(item_name, bay_name, item.get("entPhysicalClass", "")):
                return bay

        return None

    def _names_match(self, libre_name, bay_name, phys_class):
        """Heuristic matching between LibreNMS inventory name and NetBox bay name."""
        if not libre_name or not bay_name:
            return False

        libre_lower = libre_name.lower().strip()
        bay_lower = bay_name.lower().strip()

        # Exact match
        if libre_lower == bay_lower:
            return True

        # Power supply: "Power Supply 1" ↔ "PS1" or "PSU1"
        if phys_class == "powerSupply":
            ps_match = re.search(r"(\d+)", libre_name)
            bay_match = re.search(r"(\d+)", bay_name)
            if ps_match and bay_match and ps_match.group(1) == bay_match.group(1):
                if any(prefix in bay_lower for prefix in ("ps", "psu", "power")):
                    return True

        # Fan: "FanTray 1" ↔ "Fan 1" or "Fan Bay 1"
        if phys_class == "fan":
            fan_match = re.search(r"(\d+)", libre_name)
            bay_match = re.search(r"(\d+)", bay_name)
            if fan_match and bay_match and fan_match.group(1) == bay_match.group(1):
                if "fan" in bay_lower:
                    return True

        # Slot matching: "Supervisor(slot 1)" or "Linecard(slot 3)" ↔ "Slot 1" / "Slot 3"
        if phys_class == "module":
            slot_match = re.search(r"slot\s*(\d+)", libre_name, re.IGNORECASE)
            bay_match = re.search(r"slot\s*(\d+)", bay_name, re.IGNORECASE)
            if slot_match and bay_match and slot_match.group(1) == bay_match.group(1):
                return True

        # Sub-component port matching: "Converter 3/1" ↔ "X2 Port 1",
        # "TenGigabitEthernet1/5" ↔ "X2 Port 5", "GigabitEthernet3/9" ↔ "X2 Port 9"
        if phys_class in ("other", "port"):
            slot_port = re.search(r"(\d+)/(\d+)", libre_name)
            # Extract trailing number from bay name (e.g., "X2 Port 1" → 1, "SFP 3" → 3)
            bay_num = re.search(r"(\d+)\s*$", bay_name)
            if slot_port and bay_num and slot_port.group(2) == bay_num.group(1):
                if any(kw in bay_lower for kw in ("port", "sfp", "x2", "xfp", "qsfp")):
                    return True

        return False

    def _build_row(self, item, index_map, module_bays, module_types, depth=0):
        """Build a single table row from a LibreNMS inventory item."""
        model_name = item.get("entPhysicalModelName", "") or ""
        serial = item.get("entPhysicalSerialNum", "") or ""
        phys_class = item.get("entPhysicalClass", "")
        name = item.get("entPhysicalName", "") or "-"
        description = item.get("entPhysicalDescr", "") or ""

        # Match to NetBox module bay
        matched_bay = self._match_module_bay(item, index_map, module_bays)

        # Match to NetBox module type
        matched_type = module_types.get(model_name) if model_name else None

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
        }

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
            return redirect(f"{sync_url}?tab=modules")

        module_bay = get_object_or_404(ModuleBay, pk=module_bay_id, device=device)
        module_type = get_object_or_404(ModuleType, pk=module_type_id)

        # Check if bay already has a module installed
        if hasattr(module_bay, "installed_module") and module_bay.installed_module:
            messages.warning(request, f"Module bay '{module_bay.name}' already has a module installed.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return redirect(f"{sync_url}?tab=modules")

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
            messages.success(
                request, f"Installed {module_type.model} in {module_bay.name} (serial: {serial or 'N/A'})."
            )
        except Exception as e:
            messages.error(request, f"Failed to install module: {e}")

        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
        return redirect(f"{sync_url}?tab=modules")
